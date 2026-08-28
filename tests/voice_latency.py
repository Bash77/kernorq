"""
Live latency measurement: end-of-speech → first audible response byte.

Usage (requires GOOGLE_API_KEY/GEMINI_API_KEY in env):
    python -m tests.voice_latency [path/to/speech.wav] [num_runs]

Uses the same LiveVoiceSession bridge as production. Speech input is either a
WAV file argument or TTS-generated speech ("What can you do, Kernorq?").

Reported:
  - model latency:  last audio-chunk sent → first audio chunk received
  - total EoS→audio incl. VAD/turn handling overhead
"""

from __future__ import annotations

import asyncio
import base64
import os
import statistics
import sys
import time
import wave


def load_wav_16k_mono(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    if rate != 16000:
        # naive decimation for measurement purposes
        factor = rate // 16000
        pcm = b"".join(pcm[i:i + 2] for i in range(0, len(pcm) - 1, 2 * factor))
    return pcm


async def measure_one(pcm16: bytes, chunk_ms: int = 100) -> dict:
    from app.agent.live_voice import LiveVoiceSession

    session = LiveVoiceSession()
    result = {"first_audio": None, "first_transcript": None,
              "assistant_text": "", "chunks_sent": 0}
    last_send_time = None

    async def emit(msg: dict):
        nonlocal last_send_time
        now = time.perf_counter()
        if msg["type"] == "transcript" and msg["role"] == "user":
            if result["first_transcript"] is None:
                result["first_transcript"] = now
        elif msg["type"] == "audio":
            if result["first_audio"] is None and last_send_time is not None:
                result["first_audio"] = now - last_send_time
            # capture assistant text if EXECUTE-style marker appears
        elif msg["type"] == "transcript" and msg["role"] == "assistant":
            result["assistant_text"] += msg.get("text", "")

    await session.start(emit)

    # Stream speech in chunks like the browser would
    chunk_bytes = int(16000 * 2 * chunk_ms / 1000)
    start = time.perf_counter()
    for i in range(0, len(pcm16), chunk_bytes):
        await session.send_audio(pcm16[i:i + chunk_bytes])
        result["chunks_sent"] += 1
        await asyncio.sleep(chunk_ms / 1000)
    last_send_time = time.perf_counter()  # end-of-speech
    await session.commit_turn()

    # Wait up to 15 s for first audio
    deadline = time.perf_counter() + 15
    while time.perf_counter() < deadline and result["first_audio"] is None:
        await asyncio.sleep(0.02)
    elapsed_total = time.perf_counter() - start
    await session.close()

    result["total_eos_to_audio_s"] = (
        round(result["first_audio"], 3) if result["first_audio"] else None)
    result["session_duration_s"] = round(elapsed_total, 2)
    return result


def make_tts_wav(text: str) -> bytes:
    """Generates speech via existing Gemini TTS to use as test input."""
    from app.agent.voice import GeminiVoiceService

    svc = GeminiVoiceService()
    sp = svc.speak(text)
    return base64.b64decode(sp.audio_base64)


def wav_to_pcm16k(wav_bytes: bytes) -> bytes:
    import io
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    if rate != 16000:
        factor = max(1, rate // 16000)
        pcm = b"".join(pcm[i:i + 2] for i in range(0, len(pcm) - 1, 2 * factor))
    return pcm


async def main_async() -> int:
    from dotenv import load_dotenv
    load_dotenv()

    print("Kernorq Live Voice Latency Measurement")
    print("--------------------------------------")
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        print("No API key configured — cannot measure live.")
        return 1
    from app.agent.live_voice import LIVE_MODEL_DEFAULT
    print(f"Model: {LIVE_MODEL_DEFAULT}")
    print(f"Auth: gemini-api (key configured)")
    print()

    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    phrase = "Hey Kernorq, what can you do?"

    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        pcm = load_wav_16k_mono(sys.argv[1])
        print(f"Input: {sys.argv[1]}")
    else:
        wav = make_tts_wav(phrase)
        pcm = wav_to_pcm16k(wav)
        print(f"Input: TTS-generated speech: {phrase!r}")

    latencies = []
    for run in range(runs):
        try:
            r = await measure_one(pcm)
        except Exception as exc:
            print(f"Run {run + 1}: FAILED — {type(exc).__name__}: {str(exc)[:200]}")
            continue
        lat = r["total_eos_to_audio_s"]
        latencies.append(lat)
        print(f"Run {run + 1}: EoS → first audio = {lat}s "
              f"(chunks={r['chunks_sent']}, assistant said: {r['assistant_text'][:60]!r})")

    if not latencies:
        print("\nAll runs failed.")
        return 1

    print()
    print(f"Runs: {len(latencies)}")
    print(f"EoS → first audible response:")
    print(f"  min  : {min(latencies)}s")
    print(f"  median: {statistics.median(latencies)}s")
    print(f"  max  : {max(latencies)}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
