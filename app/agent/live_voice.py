"""
Gemini Live voice bridge — real-time bidirectional voice sessions.

Wraps client.aio.live.connect (google-genai ≥2.19) for server-to-server
streaming. The API key never leaves this process.

Model: gemini-3.1-flash-live-preview (documented current Live model).
Input : raw PCM16 mono 16 kHz little-endian chunks.
Output: raw PCM16 mono 24 kHz chunks + input/output transcriptions.

Conversation stays here; genuine objectives are surfaced as
{"type":"execute","objective":...} so the EXISTING deterministic pipeline
(planner → validator → executor → verifier → recovery) remains the only
execution path.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

from app.agent.gemini_client import GeminiConfigurationError

logger = logging.getLogger("kernorq.live")

LIVE_MODEL_DEFAULT = "gemini-3.1-flash-live-preview"

SYSTEM_INSTRUCTION = (
    "You are Kernorq, an autonomous execution agent's voice interface. "
    "You handle conversation: greetings, status questions, and general questions "
    "answered briefly (1-3 sentences, spoken style). "
    "You CANNOT execute tasks yourself. "
    "If the user asks you to DO something to their project (run tests, inspect, "
    "fix, analyze files, create artifacts), reply with ONLY one line starting "
    "with EXECUTE:: followed by the objective phrased as an instruction. "
    "Example: user says 'run the tests in my project' -> you output exactly: "
    "EXECUTE:: Run the tests in my project. "
    "Never invent results for project actions."
)


class LiveVoiceSession:
    """One bidirectional Live API session bridged to a browser websocket."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        resolved = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not resolved:
            raise GeminiConfigurationError("Live voice requires GOOGLE_API_KEY/GEMINI_API_KEY")
        self.model = model or os.getenv("KERNORQ_LIVE_MODEL", LIVE_MODEL_DEFAULT)
        self._resolved_key = resolved
        self._session = None
        self._receiver_task: asyncio.Task | None = None
        self.closed = False

    async def start(self, emit) -> None:
        """Connects and begins pumping server messages to `emit(dict)`."""
        # The Gemini-API key path must have NO Vertex context at all: the ADK
        # import chain dotenv-loads GOOGLE_CLOUD_PROJECT/USE_VERTEXAI=true into
        # this process, and a lingering project alongside an api_key client makes
        # the Live server abort (APIError 1000/1007). Remove them BEFORE import.
        if self._resolved_key:
            os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
            os.environ.pop("GCLOUD_PROJECT", None)
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._resolved_key, vertexai=False)
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_INSTRUCTION,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        # live.connect returns an async context manager — enter it manually so
        # the session lifetime is controlled by close(), not a with-block.
        cm = client.aio.live.connect(model=self.model, config=config)
        self._session = await type(cm).__aenter__(cm)
        self._emit = emit
        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        try:
            # receive() is an async generator in google-genai 2.x
            async for message in self._session.receive():
                # Input transcription (what the user said)
                content = getattr(message, "server_content", None)
                input_tr = getattr(content, "input_transcription", None) if content else None
                if input_tr and getattr(input_tr, "text", ""):
                    await self._emit({"type": "transcript", "role": "user",
                                      "text": input_tr.text})
                output_tr = getattr(content, "output_transcription", None) if content else None
                if output_tr and getattr(output_tr, "text", ""):
                    await self._emit({"type": "transcript", "role": "assistant",
                                      "text": output_tr.text})
                # Audio chunks (native audio output)
                if content and getattr(content, "model_turn", None):
                    for part in content.model_turn.parts or []:
                        inline = getattr(part, "inline_data", None)
                        data = getattr(inline, "data", None) if inline else None
                        if data:
                            await self._emit({
                                "type": "audio",
                                "mime_type": getattr(inline, "mime_type", "") or "audio/pcm;rate=24000",
                                "audio_base64": base64.b64encode(bytes(data)).decode("ascii"),
                            })
                if content and getattr(content, "interrupted", False):
                    await self._emit({"type": "interrupted"})
                if content and getattr(content, "turn_complete", False):
                    await self._emit({"type": "turn_complete"})
                # Session lifecycle
                go_away = getattr(message, "go_away", None)
                if go_away is not None:
                    await self._emit({"type": "go_away",
                                      "time_left": str(getattr(go_away, "time_left", ""))})
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # connection-level failure
            logger.error("live session error | exception=%s message=%s",
                         type(exc).__name__, str(exc)[:300])
            try:
                await self._emit({"type": "error", "detail": f"{type(exc).__name__}: {exc}"[:400]})
            except Exception:
                pass

    async def send_audio(self, pcm16_16k: bytes) -> None:
        """Streams one chunk of raw PCM16 @16kHz."""
        from google.genai import types

        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16_16k, mime_type="audio/pcm;rate=16000")
        )

    async def commit_turn(self) -> None:
        """Signals end-of-speech for lower latency than waiting for server VAD alone."""
        # Live API auto-detects activity; committing simply marks the boundary so the
        # model may begin responding sooner.
        try:
            await self._session.send_realtime_input(audio_stream_end=True)
        except Exception:
            pass  # auto-VAD still active; harmless

    async def close(self) -> None:
        self.closed = True
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except Exception:
                pass
        session = self._session
        if session is not None:
            self._session = None
            try:
                await session.close()
            except Exception:
                pass


__all__ = ["LiveVoiceSession", "LIVE_MODEL_DEFAULT"]
