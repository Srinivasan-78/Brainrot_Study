#!/usr/bin/env python3
"""audio + word-boundary timing + background loop -> captioned MP4, concatenated."""
import os
import sys
import json
import glob
import subprocess

BACKGROUND_DIR = "assets"
BUILD_DIR = "build"
AUDIO_DIR = f"{BUILD_DIR}/audio"
SEGMENTS_DIR = f"{BUILD_DIR}/segments"
WORDS_PER_CAPTION = 4


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: command failed: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def get_duration(path):
    out = run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ]
    )
    return float(out.strip())


def pick_background():
    candidates = sorted(glob.glob(f"{BACKGROUND_DIR}/*.mp4") + glob.glob(f"{BACKGROUND_DIR}/*.mov"))
    if not candidates:
        print(f"ERROR: no background loop videos found in {BACKGROUND_DIR}/", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


def srt_timestamp(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(boundaries, srt_path):
    entries = []
    for i in range(0, len(boundaries), WORDS_PER_CAPTION):
        group = boundaries[i:i + WORDS_PER_CAPTION]
        start = group[0]["offset_s"]
        end = group[-1]["offset_s"] + group[-1]["duration_s"]
        text = " ".join(w["text"] for w in group)
        entries.append((start, end, text))

    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, (start, end, text) in enumerate(entries, 1):
            f.write(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n\n")


def make_segment(index, audio_path, boundaries_path, background_path, out_path):
    duration = get_duration(audio_path)

    with open(boundaries_path, encoding="utf-8") as f:
        boundaries = json.load(f)

    base_vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if boundaries:
        srt_path = f"{SEGMENTS_DIR}/chunk_{index:02d}.srt"
        build_srt(boundaries, srt_path)
        style = "FontSize=20,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,Alignment=2"
        vf = f"{base_vf},subtitles={srt_path}:force_style='{style}'"
    else:
        vf = base_vf

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", background_path,
        "-i", audio_path,
        "-vf", vf,
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", str(duration),
        "-c:v", "libx264", "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    run(cmd)


def concat_segments(segment_paths, out_path):
    list_path = f"{BUILD_DIR}/concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", out_path,
        ]
    )


def main():
    with open(f"{BUILD_DIR}/script.json", encoding="utf-8") as f:
        data = json.load(f)
    n_chunks = len(data["chunks"])

    background_path = pick_background()
    os.makedirs(SEGMENTS_DIR, exist_ok=True)

    segment_paths = []
    for i in range(n_chunks):
        audio_path = f"{AUDIO_DIR}/chunk_{i:02d}.mp3"
        boundaries_path = f"{AUDIO_DIR}/chunk_{i:02d}.json"
        out_path = f"{SEGMENTS_DIR}/chunk_{i:02d}.mp4"
        if not os.path.exists(audio_path):
            print(f"ERROR: missing audio file {audio_path}", file=sys.stderr)
            sys.exit(1)
        print(f"[assemble] rendering segment {i + 1}/{n_chunks}...")
        make_segment(i, audio_path, boundaries_path, background_path, out_path)
        segment_paths.append(out_path)

    final_path = f"{BUILD_DIR}/output.mp4"
    print("[assemble] concatenating segments...")
    concat_segments(segment_paths, final_path)
    print(f"[assemble] wrote {final_path}")


if __name__ == "__main__":
    main()
