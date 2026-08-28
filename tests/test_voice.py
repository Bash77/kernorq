"""
Voice interface tests — Kernorq voice layer.

Proves:
  - /voice/status reflects configuration
  - transcription endpoint works with injected provider (and 503 when unconfigured)
  - speech endpoint returns audio with injected provider (and clear errors otherwise)
  - voice failures never break execution APIs
  - API key never appears in frontend source

Uses fake providers via monkeypatching get_default_voice_service.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.voice import (
    GeminiVoiceService,
    NoopVoiceService,
    SpeechResult,
    TranscriptionResult,
    VoiceUnavailableError,
)
from app.api.main import create_app, get_default_voice_service
from app.memory.sqlite_store import SQLiteExecutionStore


class FakeModel:
    def __init__(self, response: dict):
        self._response = response

    def generate(self, objective: str) -> str:
        return json.dumps(self._response)


def _valid_plan():
    return {
        "objective": "Inspect workspace",
        "tasks": [
            {
                "task_id": "inspect_workspace",
                "title": "Inspect workspace",
                "description": "Inspect repo",
                "tool_name": "inspect_project_workspace",
                "tool_input": {"directory_path": "."},
            }
        ],
    }


class FakeVoiceProvider:
    def transcribe(self, audio_base64: str, mime_type: str = "audio/webm") -> TranscriptionResult:
        if not audio_base64 or audio_base64 == "bogus":
            raise VoiceUnavailableError("bad audio")
        return TranscriptionResult(text="Run the test suite and report failures")

    def speak(self, text: str) -> SpeechResult:
        if not text.strip():
            raise VoiceUnavailableError("no text")
        # Minimal WAV header + silence
        wav = base64.b64encode(b"RIFF0000WAVEfmt ").decode()
        return SpeechResult(audio_base64=wav, mime_type="audio/wav")


class FailingVoiceProvider:
    def __init__(self, exc=VoiceUnavailableError("Gemini down")):
        self._exc = exc

    def transcribe(self, audio_base64: str, mime_type: str = "audio/webm"):
        raise self._exc

    def speak(self, text: str):
        raise self._exc


def _temp_store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        Path(tmp.name).unlink()
    except Exception:
        pass
    return tmp.name


def _safe_unlink(p):
    try:
        Path(p).unlink(missing_ok=True)
    except PermissionError:
        import gc, time
        gc.collect(); time.sleep(0.05)
        try: Path(p).unlink(missing_ok=True)
        except Exception: pass
    for suf in ["-wal", "-shm"]:
        try: Path(f"{p}{suf}").unlink(missing_ok=True)
        except Exception: pass


@pytest.fixture()
def client_with_voice(monkeypatch):
    db = _temp_store()
    store = SQLiteExecutionStore(db)
    app = create_app(store=store, model_client=FakeModel(_valid_plan()))
    monkeypatch.setattr("app.api.main.get_default_voice_service", lambda: FakeVoiceProvider())
    yield TestClient(app), store
    try:
        store.close()
    except Exception:
        pass
    _safe_unlink(db)


@pytest.fixture()
def client_no_voice(monkeypatch):
    db = _temp_store()
    store = SQLiteExecutionStore(db)
    app = create_app(store=store, model_client=FakeModel(_valid_plan()))
    monkeypatch.setattr("app.api.main.get_default_voice_service", lambda: NoopVoiceService())
    yield TestClient(app), store
    try:
        store.close()
    except Exception:
        pass
    _safe_unlink(db)


def test_voice_status_available(client_with_voice):
    client, _ = client_with_voice
    resp = client.get("/voice/status")
    assert resp.status_code == 200
    assert resp.json()["available"] is True
    assert resp.json()["provider"] == "gemini"


def test_voice_status_unavailable(client_no_voice):
    client, _ = client_no_voice
    resp = client.get("/voice/status")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_transcribe_success(client_with_voice):
    client, _ = client_with_voice
    resp = client.post("/voice/transcribe", json={"audio_base64": "aGVsbG8=", "mime_type": "audio/webm"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "Run the test suite and report failures"
    assert resp.json()["provider"] == "gemini"


def test_transcribe_unconfigured_returns_503(client_no_voice):
    client, _ = client_no_voice
    resp = client.post("/voice/transcribe", json={"audio_base64": "aGVsbG8="})
    assert resp.status_code == 503
    assert "configuration" in resp.json()["detail"].lower() or "api_key" in resp.json()["detail"].lower()


def test_transcribe_runtime_failure_returns_502(client_with_voice, monkeypatch):
    client, _ = client_with_voice
    monkeypatch.setattr("app.api.main.get_default_voice_service", lambda: FailingVoiceProvider())
    resp = client.post("/voice/transcribe", json={"audio_base64": "aGVsbG8="})
    assert resp.status_code == 502


def test_speak_success(client_with_voice):
    client, _ = client_with_voice
    resp = client.post("/voice/speak", json={"text": "Execution completed successfully."})
    assert resp.status_code == 200
    data = resp.json()
    assert data["audio_base64"]
    assert "audio" in data["mime_type"]


def test_speak_unconfigured_returns_503(client_no_voice):
    client, _ = client_no_voice
    resp = client.post("/voice/speak", json={"text": "hello"})
    assert resp.status_code == 503


def test_voice_failure_does_not_break_execution(client_no_voice):
    """Execution APIs remain fully functional when voice is unavailable."""
    client, _ = client_no_voice
    resp = client.post("/executions", json={"objective": "Inspect workspace"})
    assert resp.status_code == 201
    exec_id = resp.json()["execution_id"]
    assert client.get(f"/executions/{exec_id}").status_code == 200
    # Voice endpoints fail gracefully, not fatally
    assert client.post("/voice/speak", json={"text": "x"}).status_code == 503


def test_api_key_not_in_frontend_source():
    """Security invariant: no long-lived API key in browser code."""
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    for blob in (js, html):
        assert "GOOGLE_API_KEY" not in blob
        assert "GEMINI_API_KEY" not in blob
        assert "api_key=" not in blob.replace("api_key=None", "")


def test_gemini_voice_service_requires_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
    with pytest.raises(Exception, match="not configured|GOOGLE_API_KEY"):
        GeminiVoiceService()


# ---------------------------------------------------------------------------
# Service-layer behavior with mocked SDK (no network)
# ---------------------------------------------------------------------------

import base64 as _b64


class _Inline:
    def __init__(self, data, mime):
        self.data = data
        self.mime_type = mime


class _Part:
    def __init__(self, inline):
        self.inline_data = inline


def _mk_speak_response(pcm=b"\x01\x02" * 4800, mime="audio/L16;rate=24000", with_audio=True):
    part = _Part(_Inline(pcm, mime)) if with_audio else _Part(None)
    cand = type("C", (), {"content": type("C2", (), {"parts": [part]})(), "finish_reason": "STOP"})()
    return type("R", (), {"candidates": [cand]})()


def test_transcribe_uses_no_tools_and_plain_text_config(monkeypatch):
    """Transcription must not pass Kernorq tools or enable automatic function calling."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    from unittest.mock import MagicMock, patch

    fake_resp = type("R", (), {"text": "Inspect my project"})()
    with patch("google.genai.Client") as MockClient:
        inst = MagicMock()
        inst.models.generate_content.return_value = fake_resp
        MockClient.return_value = inst

        svc = GeminiVoiceService()
        out = svc.transcribe(_b64.b64encode(b"x" * 200).decode(), mime_type="audio/wav")
        assert out.text == "Inspect my project"

        call = inst.models.generate_content.call_args
        kwargs = call.kwargs
        # No tools of any kind passed
        cfg = kwargs.get("config")
        assert cfg is not None
        assert getattr(cfg, "tools", None) in (None, [])
        assert getattr(cfg.automatic_function_calling, "disable", True) is True
        assert getattr(cfg.tool_config.function_calling_config, "mode", "NONE") == "NONE"
        # Audio part present with the actual MIME
        contents = kwargs["contents"]
        audio_part = contents[0]
        assert audio_part.inline_data.mime_type == "audio/wav"
        # Model is a real current Flash model, not a speculative one
        assert kwargs["model"].startswith("gemini-")


def test_transcribe_empty_transcript_raises_clear_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    from unittest.mock import MagicMock, patch

    with patch("google.genai.Client") as MockClient:
        inst = MagicMock()
        inst.models.generate_content.return_value = type("R", (), {"text": ""})()
        MockClient.return_value = inst
        svc = GeminiVoiceService()
        with pytest.raises(VoiceUnavailableError, match="returned no speech"):
            svc.transcribe(_b64.b64encode(b"x" * 200).decode())


def test_transcribe_too_short_audio_rejected(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    from unittest.mock import MagicMock, patch
    with patch("google.genai.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = GeminiVoiceService()
        with pytest.raises(VoiceUnavailableError, match="too short"):
            svc.transcribe(_b64.b64encode(b"tiny").decode())


def test_transcribe_gemini_exception_translated(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    from unittest.mock import MagicMock, patch

    class Fake403(Exception):
        pass

    with patch("google.genai.Client") as MockClient:
        inst = MagicMock()
        inst.models.generate_content.side_effect = Fake403(
            "403 PERMISSION_DENIED. billing not enabled"
        )
        MockClient.return_value = inst
        svc = GeminiVoiceService()
        with pytest.raises(VoiceUnavailableError, match="transcription failed"):
            svc.transcribe(_b64.b64encode(b"x" * 200).decode())


def test_speak_validates_response_chain(monkeypatch):
    """Missing candidate/content/audio part must produce a meaningful error."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    from unittest.mock import MagicMock, patch

    scenarios = [
        type("R", (), {"candidates": []})(),  # no candidates
        type("R", (), {"candidates": [type("C", (), {"content": None, "finish_reason": "SAFETY"})()]})(),
    ]
    for resp in scenarios:
        with patch("google.genai.Client") as MockClient:
            inst = MagicMock()
            inst.models.generate_content.return_value = resp
            MockClient.return_value = inst
            svc = GeminiVoiceService()
            with pytest.raises(VoiceUnavailableError, match="no audio data"):
                svc.speak("hello")


def test_speak_returns_valid_wav_with_parsed_rate(monkeypatch):
    """PCM wrapped in WAV; sample rate parsed from returned mime, not assumed."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    import struct
    from unittest.mock import MagicMock, patch

    pcm = b"\x00\x01" * 2400  # 4800 bytes
    with patch("google.genai.Client") as MockClient:
        inst = MagicMock()
        inst.models.generate_content.return_value = _mk_speak_response(pcm=pcm, mime="audio/L16;rate=16000")
        MockClient.return_value = inst
        svc = GeminiVoiceService()
        result = svc.speak("All done")
        raw = base64.b64decode(result.audio_base64)
        # WAV header checks
        assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
        riff_size = struct.unpack("<I", raw[4:8])[0]
        assert riff_size == len(raw) - 8
        rate = struct.unpack("<I", raw[24:28])[0]
        assert rate == 16000  # parsed from mime, not hardcoded 24000
        data_size = struct.unpack("<I", raw[40:44])[0]
        assert data_size == len(raw) - 44
        assert result.mime_type == "audio/wav"


def test_pcm_to_wav_header_valid():
    import struct
    pcm = b"\x10\x00" * 100
    wav = __import__("app.agent.voice", fromlist=["pcm_to_wav"]).pcm_to_wav(pcm, 24000)
    assert wav[:4] == b"RIFF"
    assert struct.unpack("<I", wav[4:8])[0] == 36 + len(pcm)
    assert struct.unpack("<H", wav[22:24])[0] == 1  # mono
    assert struct.unpack("<I", wav[24:28])[0] == 24000
    assert wav[36:40] == b"data"


def test_default_models_are_current():
    assert GeminiVoiceService.TRANSCRIBE_MODEL_DEFAULT == "gemini-3.5-flash"
    assert GeminiVoiceService.TTS_MODEL_DEFAULT == "gemini-3.1-flash-tts-preview"


def test_api_key_path_forces_gemini_api_not_vertex(monkeypatch):
    """GOOGLE_API_KEY must select the Gemini Developer API even when Vertex env flags exist."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
    from unittest.mock import MagicMock, patch

    with patch("google.genai.Client") as MockClient:
        MockClient.return_value = MagicMock()
        GeminiVoiceService()
        kwargs = MockClient.call_args.kwargs
        assert kwargs.get("api_key") == "fake-key"
        assert kwargs.get("vertexai") is False  # never redirected to Vertex


def test_planner_client_api_key_path_forces_gemini_api(monkeypatch):
    """Same invariant for the planning client."""
    import os as _os
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    from unittest.mock import MagicMock, patch
    from app.agent.gemini_client import GeminiModelClient
    from app.tools.registry import create_default_tool_registry

    with patch("google.genai.Client") as MockClient:
        inst = MagicMock()
        inst.models.generate_content.return_value = type(
            "R", (), {"text": json.dumps({"objective": "x", "tasks": []})}
        )()
        MockClient.return_value = inst
        client = GeminiModelClient(create_default_tool_registry())
        assert client.auth_mode == "gemini-api"
        kwargs = MockClient.call_args.kwargs
        assert kwargs.get("vertexai") is False


def test_app_module_imports_cleanly():
    """Regression for earlier NameError: CreateExecutionResponse must be defined at import."""
    import importlib
    mod = importlib.import_module("app.api.main")
    assert hasattr(mod, "CreateExecutionRequest")
    assert hasattr(mod, "CreateExecutionResponse")
    assert hasattr(mod, "TranscribeRequest")
    assert hasattr(mod, "SpeakRequest")
    assert callable(mod.create_app)


# ---------------------------------------------------------------------------
# Optional live smoke test — only runs with KERNORQ_VOICE_LIVE_TEST=1 + credentials
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.getenv("KERNORQ_VOICE_LIVE_TEST") != "1", reason="Live voice test disabled")
def test_live_gemini_voice_smoke():
    """Real end-to-end: tiny WAV → transcript non-empty; phrase → TTS audio non-empty."""
    import os as _os
    from dotenv import load_dotenv
    load_dotenv()
    svc = GeminiVoiceService()  # raises if unconfigured — acceptable for opted-in live test
    # Tiny spoken-ish WAV: 16kHz, 0.5s of low-amplitude tone won't transcribe to speech;
    # instead reuse TTS output as transcription input (known speech content).
    sp = svc.speak("Inspect my project and report its structure")
    assert len(base64.b64decode(sp.audio_base64)) > 1000
    tr = svc.transcribe(sp.audio_base64, mime_type="audio/wav")
    assert tr.text.strip()
    print(f"LIVE STT transcript: {tr.text!r}")
