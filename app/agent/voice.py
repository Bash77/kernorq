"""
VoiceService — Gemini voice interface layer (server-side).

Responsibilities:
  - speech input transcription  (audio bytes → text objective)
  - spoken output               (text → WAV audio bytes)
  - availability check          (is voice configured?)

Design rules:
  - Dedicated requests: NO Kernorq tools, NO planner, NO executor, NO AFC.
  - Browser sends audio/wav (16 kHz mono PCM16) recorded client-side, so the
    MIME type always matches the actual bytes (Gemini supports WAV natively).
  - TTS response validated step-by-step; PCM sample rate parsed from the
    returned mime_type instead of assumed.
  - Provider is replaceable (VoiceProvider protocol); execution never depends
    on voice success.
"""

from __future__ import annotations

import base64
import logging
import os
import struct
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.gemini_client import GeminiConfigurationError

logger = logging.getLogger("kernorq.voice")


class VoiceUnavailableError(RuntimeError):
    """Raised when a voice operation fails at runtime (quota, billing, network, model)."""


@dataclass
class TranscriptionResult:
    text: str
    provider: str = "gemini"


@dataclass
class SpeechResult:
    audio_base64: str  # complete WAV file, base64-encoded
    mime_type: str = "audio/wav"
    provider: str = "gemini"


class VoiceProvider(Protocol):
    """Replaceable voice provider interface (future: GeminiLiveVoiceService)."""

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav") -> TranscriptionResult: ...
    def speak(self, text: str) -> SpeechResult: ...


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Wraps raw PCM (little-endian) in a valid RIFF/WAVE container."""
    data_size = len(pcm_bytes)
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels,
        sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    return header + pcm_bytes


def _parse_rate_from_mime(mime: Any) -> int:
    """Extracts sample rate from mime like 'audio/L16;rate=24000'. Falls back to 24000."""
    try:
        s = str(mime or "")
        if "rate=" in s:
            return int(s.split("rate=", 1)[1].split(";", 1)[0].strip())
        if "rate=" in s.lower():
            return int(s.lower().split("rate=", 1)[1].split(";", 1)[0].strip())
    except Exception:
        pass
    return 24000


class GeminiVoiceService:
    """
    Server-side Gemini voice service (request/response pattern; no Live API).

    Models (configurable):
      KERNORQ_TRANSCRIBE_MODEL  default gemini-3.5-flash            (GA, audio input, hackathon-compliant)
      KERNORQ_TTS_MODEL         default gemini-3.1-flash-tts-preview (documented current TTS)
      KERNORQ_VOICE_NAME        default Kore

    Raises GeminiConfigurationError if no credentials configured,
    VoiceUnavailableError on runtime failures (billing, quota, network, bad audio).
    """

    TRANSCRIBE_MODEL_DEFAULT = "gemini-3.5-flash"
    TTS_MODEL_DEFAULT = "gemini-3.1-flash-tts-preview"

    _TRANSCRIBE_PROMPT = (
        "Transcribe the user's speech exactly.\n"
        "Return ONLY the spoken transcript.\n"
        "Do not summarize.\n"
        "Do not answer the user.\n"
        "Do not execute tools.\n"
        "Do not add commentary."
    )

    def __init__(
        self,
        api_key: str | None = None,
        tts_model: str | None = None,
        transcribe_model: str | None = None,
        voice_name: str | None = None,
    ) -> None:
        resolved_key = (
            api_key
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_GENAI_API_KEY")
        )
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT")
        self._auth_mode = "gemini-api" if resolved_key else ("vertex-ai" if project else None)
        if not self._auth_mode:
            raise GeminiConfigurationError(
                "Gemini voice not configured: set GOOGLE_API_KEY/GEMINI_API_KEY "
                "or GOOGLE_CLOUD_PROJECT (+billing enabled)"
            )

        # google-genai caches GOOGLE_GENAI_USE_VERTEXAI at import time; neutralize
        # a stale =true BEFORE import when using the Gemini API key path.
        if resolved_key:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"

        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise GeminiConfigurationError(f"google-genai not installed: {exc}") from exc

        self._types = types
        try:
            if resolved_key:
                # Gemini Developer API path; vertexai=False so a stray
                # GOOGLE_GENAI_USE_VERTEXAI=true cannot redirect an api_key client.
                self._client = genai.Client(api_key=resolved_key, vertexai=False)
                self._auth_mode = "gemini-api"
            else:
                self._client = genai.Client(
                    vertexai=True,
                    project=project,
                    location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
                )
                self._auth_mode = "vertex-ai"
        except Exception as exc:
            raise GeminiConfigurationError(f"Failed to create Gemini client: {exc}") from exc

        self.tts_model = tts_model or os.getenv("KERNORQ_TTS_MODEL", self.TTS_MODEL_DEFAULT)
        self.transcribe_model = transcribe_model or os.getenv(
            "KERNORQ_TRANSCRIBE_MODEL", self.TRANSCRIBE_MODEL_DEFAULT
        )
        self.voice_name = voice_name or os.getenv("KERNORQ_VOICE_NAME", "Kore")

    def _no_tools_config(self) -> Any:
        """Plain-text generation config: AFC disabled, zero tool declarations."""
        t = self._types
        try:
            return t.GenerateContentConfig(
                automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
                tool_config=t.ToolConfig(
                    function_calling_config=t.FunctionCallingConfig(mode="NONE")
                ),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Speech input → text
    # ------------------------------------------------------------------

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav") -> TranscriptionResult:
        """Transcribes recorded audio into an objective string. No tools involved."""
        diag = {
            "operation": "transcribe",
            "provider": "gemini",
            "model": self.transcribe_model,
            "auth_mode": self._auth_mode,
        }
        if not audio_base64 or not audio_base64.strip():
            raise VoiceUnavailableError("No audio data supplied for transcription")
        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as exc:
            raise VoiceUnavailableError(f"Invalid base64 audio payload: {exc}") from exc
        if len(audio_bytes) < 100:
            raise VoiceUnavailableError("Recording too short to contain speech")

        diag["mime"] = mime_type
        diag["bytes"] = len(audio_bytes)
        try:
            t = self._types
            response = self._client.models.generate_content(
                model=self.transcribe_model,
                contents=[
                    t.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    self._TRANSCRIBE_PROMPT,
                ],
                config=self._no_tools_config(),
            )
        except Exception as exc:
            logger.error("voice failed | %s | exception=%s message=%s",
                         " ".join(f"{k}={v}" for k, v in diag.items()),
                         type(exc).__name__, str(exc)[:400])
            raise VoiceUnavailableError(f"Gemini transcription failed: {exc}") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            logger.error("voice empty transcript | %s", " ".join(f"{k}={v}" for k, v in diag.items()))
            raise VoiceUnavailableError("Voice transcription returned no speech.")
        logger.info("voice ok | %s | transcript_chars=%d",
                    " ".join(f"{k}={v}" for k, v in diag.items()), len(text))
        return TranscriptionResult(text=text)

    # ------------------------------------------------------------------
    # Text → spoken output (WAV)
    # ------------------------------------------------------------------

    def speak(self, text: str) -> SpeechResult:
        """Generates spoken audio using Gemini TTS; returns a valid WAV file."""
        diag = {
            "operation": "speak",
            "provider": "gemini",
            "model": self.tts_model,
            "voice": self.voice_name,
            "auth_mode": self._auth_mode,
        }
        if not text or not text.strip():
            raise VoiceUnavailableError("No text supplied for speech synthesis")

        try:
            t = self._types
            response = self._client.models.generate_content(
                model=self.tts_model,
                contents=text[:2000],
                config=t.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
                    speech_config=t.SpeechConfig(
                        voice_config=t.VoiceConfig(
                            prebuilt_voice_config=t.PrebuiltVoiceConfig(
                                voice_name=self.voice_name,
                            )
                        )
                    ),
                ),
            )
        except Exception as exc:
            logger.error("voice failed | %s | exception=%s message=%s",
                         " ".join(f"{k}={v}" for k, v in diag.items()),
                         type(exc).__name__, str(exc)[:400])
            raise VoiceUnavailableError(f"Gemini speech generation failed: {exc}") from exc

        # Robust response validation: response → candidate(s) → content → parts → inline audio
        pcm_data = b""
        audio_mime = ""
        try:
            candidates = list(getattr(response, "candidates", None) or [])
            if candidates:
                content = getattr(candidates[0], "content", None)
                parts = list(getattr(content, "parts", None) or []) if content else []
                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    data = getattr(inline, "data", None) if inline is not None else None
                    if data:
                        pcm_data = bytes(data)
                        audio_mime = str(getattr(inline, "mime_type", "") or "")
                        break
        except Exception as exc:
            logger.error("voice unparsable response | %s | exception=%s", 
                         " ".join(f"{k}={v}" for k, v in diag.items()), type(exc).__name__)
            raise VoiceUnavailableError(f"Gemini TTS response unreadable: {exc}") from exc

        if not pcm_data:
            # Surface block reasons if present (e.g., safety), else generic
            block = ""
            try:
                cand0 = list(getattr(response, "candidates", None) or [])
                if cand0:
                    block = str(getattr(cand0[0], "finish_reason", "") or "")
            except Exception:
                pass
            logger.error("voice no audio part | %s | finish_reason=%s",
                         " ".join(f"{k}={v}" for k, v in diag.items()), block)
            msg = f"Gemini TTS returned no audio data{' (finish_reason=' + block + ')' if block else ''}"
            raise VoiceUnavailableError(msg)

        sample_rate = _parse_rate_from_mime(audio_mime)
        wav_bytes = pcm_to_wav(pcm_data, sample_rate)
        logger.info("voice ok | %s | pcm_bytes=%d rate=%d mime=%s",
                    " ".join(f"{k}={v}" for k, v in diag.items()),
                    len(pcm_data), sample_rate, audio_mime)
        return SpeechResult(
            audio_base64=base64.b64encode(wav_bytes).decode("ascii"),
            mime_type="audio/wav",
        )


class NoopVoiceService:
    """Fallback provider used when credentials are absent — voice unavailable, text works."""

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav") -> TranscriptionResult:
        raise GeminiConfigurationError(
            "Voice input requires GOOGLE_API_KEY/GEMINI_API_KEY configuration"
        )

    def speak(self, text: str) -> SpeechResult:
        raise GeminiConfigurationError(
            "Voice output requires GOOGLE_API_KEY/GEMINI_API_KEY configuration"
        )


def get_default_voice_service() -> GeminiVoiceService | NoopVoiceService:
    """Factory used by the API; returns Noop when credentials are absent."""
    try:
        return GeminiVoiceService()
    except GeminiConfigurationError:
        return NoopVoiceService()


__all__ = [
    "VoiceProvider",
    "VoiceUnavailableError",
    "TranscriptionResult",
    "SpeechResult",
    "GeminiVoiceService",
    "NoopVoiceService",
    "get_default_voice_service",
    "pcm_to_wav",
]
