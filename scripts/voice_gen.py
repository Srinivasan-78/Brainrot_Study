#!/usr/bin/env python3
"""script.json -> numbered TTS audio files + word-boundary timing JSON (edge-tts, no key)."""
import os
import sys
import json
import asyncio
import edge_tts

VOICE = os.environ.get("TTS_VOICE", "en-US-AndrewNeural")


async def synth_chunk(text, audio_path, boundaries_path):
    communicate = edge_tts.Communicate(text, VOICE)
    boundaries = []
    with open(audio_path, "wb") as audio_f:
        async for event in communicate.stream():
            if event["type"] == "audio":
                audio_f.write(event["data"])
            elif event["type"] == "WordBoundary":
                boundaries.append(
                    {
                        "text": event["text"],
                        "offset_s": event["offset"] / 10_000_000,
                        "duration_s": event["duration"] / 10_000_000,
                    }
                )
    with open(boundaries_path, "w", encoding="utf-8") as f:
        json.dump(boundaries, f, indent=2)
    return len(boundaries)


async def main_async():
    with open("build/script.json", encoding="utf-8") as f:
        data = json.load(f)

    chunks = data["chunks"]
    os.makedirs("build/audio", exist_ok=True)

    for i, chunk in enumerate(chunks):
        text = chunk["text"]
        audio_path = f"build/audio/chunk_{i:02d}.mp3"
        boundaries_path = f"build/audio/chunk_{i:02d}.json"
        print(f"[voice_gen] synthesizing chunk {i + 1}/{len(chunks)}...")
        try:
            n_words = await synth_chunk(text, audio_path, boundaries_path)
        except Exception as e:
            print(f"ERROR: TTS failed on chunk {i}: {e}", file=sys.stderr)
            sys.exit(1)
        if n_words == 0:
            print(
                f"ERROR: chunk {i} produced no WordBoundary events — captions cannot be timed. "
                f"Try a different TTS_VOICE or upgrade edge-tts.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"[voice_gen]   {n_words} word boundaries captured")

    print(f"[voice_gen] wrote {len(chunks)} audio files to build/audio/")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
