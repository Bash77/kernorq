"""
Kernorq Voice Diagnostic — developer command.

Run:  python -m tests.voice_diagnostic

Prints configuration status and performs live STT/TTS round-trip when
credentials are functional. Never prints the API key.

Exit codes: 0 = all pass, 1 = configuration/runtime failure.
"""

from __future__ import annotations

import base64
import os
import sys


def _mask(value: str | None) -> str:
    return f"configured (…{value[-4:]})" if value else "not set"


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()

    print("Kernorq Voice Diagnostic")
    print("------------------------")

    api_key = (
        os.getenv("GOOGLE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_GENAI_API_KEY")
    )
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    auth_mode = "api-key" if api_key else ("vertex-adc" if project else None)
    print(f"API key: {_mask(api_key)}")
    print(f"Vertex project: {project or 'not set'}")
    try:
        import google.genai as genai_mod

        sdk_version = getattr(genai_mod, "__version__", "unknown")
    except Exception:
        sdk_version = "NOT INSTALLED"
    print(f"SDK: google-genai {sdk_version}")

    from app.agent.voice import GeminiVoiceService, VoiceUnavailableError

    t_model = os.getenv("KERNORQ_TRANSCRIBE_MODEL", GeminiVoiceService.TRANSCRIBE_MODEL_DEFAULT)
    s_model = os.getenv("KERNORQ_TTS_MODEL", GeminiVoiceService.TTS_MODEL_DEFAULT)
    voice = os.getenv("KERNORQ_VOICE_NAME", "Kore")
    print(f"Transcription model: {t_model}")
    print(f"TTS model: {s_model}")
    print(f"Voice: {voice}")
    print()

    failures = 0
    try:
        svc = GeminiVoiceService()
    except Exception as exc:
        print(f"Configuration: FAIL — {exc}")
        print("\nFix: provide a Gemini API key (GOOGLE_API_KEY) or enable billing on the")
        print("Vertex AI project named in GOOGLE_CLOUD_PROJECT.")
        return 1
    print(f"Auth mode: {svc._auth_mode}")

    # TTS first: produces known speech we can feed back into STT
    stt_status = tts_status = "FAIL"
    detail = ""
    try:
        phrase = "Kernorq voice diagnostic. Execution verified."
        sp = svc.speak(phrase)
        wav_len = len(base64.b64decode(sp.audio_base64))
        if sp.mime_type == "audio/wav" and wav_len > 1000:
            tts_status = "PASS"
            print(f"TTS: PASS ({wav_len} bytes WAV)")
        else:
            detail = f"unexpected mime/size: {sp.mime_type}, {wav_len}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"

    try:
        if tts_status == "PASS":
            tr = svc.transcribe(sp.audio_base64, mime_type="audio/wav")
            if tr.text.strip():
                stt_status = "PASS"
                print(f"STT: PASS (transcript: {tr.text!r})")
                print()
                print("STT: PASS")
                print("TTS: PASS")
                return 0
            detail = "empty transcript"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"

    failures += 1
    print(f"STT: {'PASS' if stt_status == 'PASS' else 'FAIL'}")
    print(f"TTS: {'PASS' if tts_status == 'PASS' else 'FAIL'}")
    if detail:
        print(f"\nLast failure: {detail}")
        if "403" in detail or "billing" in detail.lower():
            print("\nDiagnosis: Vertex AI billing is disabled for this project.")
            print("Enable billing in Google Cloud Console, or switch to a Gemini API key:")
            print("  set GOOGLE_API_KEY=<key from https://aistudio.google.com/apikey>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
