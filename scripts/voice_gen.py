#!/usr/bin/env python3
"""script.json -> numbered TTS audio files + word-boundary timing JSON (edge-tts, no key)."""
import os
import sys
import json
import asyncio
import subprocess
import traceback
import edge_tts

# NOTE: the newer conversational voices (Andrew, Ava, Emma, Brian) do NOT emit
# WordBoundary metadata, so captions cannot be timed from them. Classic neural
# voices (Christopher, Guy, Aria, Jenny, Eric) do.
VOICE = os.environ.get("TTS_VOICE", "en-US-ChristopherNeural")
CHUNK_TIMEOUT = int(os.environ.get("TTS_TIMEOUT", "90"))
MAX_ATTEMPTS = 3


def audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def estimate_boundaries(text, duration):
    """Fallback timings when the voice emits no WordBoundary events.

    Distributes the real audio duration across words in proportion to their
    length. Less accurate than real boundaries but keeps captions usable.
    """
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    boundaries = []
    cursor = 0.0
    for word, weight in zip(words, weights):
        span = duration * (weight / total)
        boundaries.append({"text": word, "offset_s": cursor, "duration_s": span})
        cursor += span
    return boundaries


async def synth_chunk(text, audio_path, boundaries_path):
    communicate = edge_tts.Communicate(text, VOICE)
    boundaries = []
    audio_bytes = 0
    with open(audio_path, "wb") as audio_f:
        async for event in communicate.stream():
            if event["type"] == "audio":
                audio_f.write(event["data"])
                audio_bytes += len(event["data"])
            elif event["type"] == "WordBoundary":
                boundaries.append(
                    {
                        "text": event["text"],
                        "offset_s": event["offset"] / 10_000_000,
                        "duration_s": event["duration"] / 10_000_000,
                    }
                )
    if audio_bytes == 0:
        raise RuntimeError("edge-tts returned no audio data")

    estimated = False
    if not boundaries:
        boundaries = estimate_boundaries(text, audio_duration(audio_path))
        estimated = True

    with open(boundaries_path, "w", encoding="utf-8") as f:
        json.dump(boundaries, f, indent=2)
    return len(boundaries), audio_bytes, estimated


async def synth_with_retry(i, text, audio_path, boundaries_path):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(
                synth_chunk(text, audio_path, boundaries_path), timeout=CHUNK_TIMEOUT
            )
        except asyncio.TimeoutError:
            print(
                f"[voice_gen] chunk {i}: timed out after {CHUNK_TIMEOUT}s "
                f"(attempt {attempt}/{MAX_ATTEMPTS})",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            print(
                f"[voice_gen] chunk {i}: {type(e).__name__}: {e} "
                f"(attempt {attempt}/{MAX_ATTEMPTS})",
                file=sys.stderr,
                flush=True,
            )
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(3 * attempt)
    raise RuntimeError(f"chunk {i}: TTS failed after {MAX_ATTEMPTS} attempts")


async def main_async():
    print(f"[voice_gen] starting, edge-tts {getattr(edge_tts, '__version__', 'unknown')}, voice={VOICE}", flush=True)

    with open("build/script.json", encoding="utf-8") as f:
        data = json.load(f)

    chunks = data["chunks"]
    os.makedirs("build/audio", exist_ok=True)
    print(f"[voice_gen] {len(chunks)} chunks to synthesize", flush=True)

    for i, chunk in enumerate(chunks):
        text = chunk["text"]
        audio_path = f"build/audio/chunk_{i:02d}.mp3"
        boundaries_path = f"build/audio/chunk_{i:02d}.json"
        print(f"[voice_gen] chunk {i + 1}/{len(chunks)}: {len(text.split())} words...", flush=True)

        n_words, n_bytes, estimated = await synth_with_retry(
            i, text, audio_path, boundaries_path
        )

        if n_words == 0:
            print(f"ERROR: chunk {i} yielded no timings at all", file=sys.stderr, flush=True)
            sys.exit(1)
        source = "estimated" if estimated else "from voice"
        print(f"[voice_gen]   ok: {n_bytes} bytes, {n_words} word timings ({source})", flush=True)
        if estimated and i == 0:
            print(
                f"[voice_gen] NOTE: voice '{VOICE}' emits no WordBoundary events; "
                f"caption timings are estimated. Use a classic voice "
                f"(en-US-ChristopherNeural, en-US-GuyNeural, en-US-AriaNeural) for exact timing.",
                flush=True,
            )

    print(f"[voice_gen] wrote {len(chunks)} audio files to build/audio/", flush=True)


def main():
    try:
        asyncio.run(main_async())
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
