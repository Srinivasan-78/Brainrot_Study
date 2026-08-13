#!/usr/bin/env python3
"""audio + word-boundary timing + background loop -> captioned MP4, concatenated."""
import os
import sys
import json
import re
import glob
import random
import subprocess

BACKGROUND_DIR = "assets"
BUILD_DIR = "build"
AUDIO_DIR = f"{BUILD_DIR}/audio"
SEGMENTS_DIR = f"{BUILD_DIR}/segments"
WORDS_PER_CAPTION = 3
FPS = 30


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


def background_groups():
    """Group clips into series by filename prefix.

    part_00..part_18 form one continuous series, partty_00..partty_07 another,
    and bare-numeric files (1.mp4, 2.mp4) are each standalone. Returns
    {group_name: [paths in numeric order]}.
    """
    candidates = glob.glob(f"{BACKGROUND_DIR}/*.mp4") + glob.glob(f"{BACKGROUND_DIR}/*.mov")
    if not candidates:
        print(f"ERROR: no background loop videos found in {BACKGROUND_DIR}/", file=sys.stderr)
        sys.exit(1)

    groups = {}
    for path in candidates:
        stem = os.path.splitext(os.path.basename(path))[0]
        m = re.match(r"^(.*?)[_-]?(\d+)$", stem)
        if m and m.group(1):
            name, order = m.group(1), int(m.group(2))
        else:
            name, order = stem, 0
        groups.setdefault(name, []).append((order, path))

    return {name: [p for _, p in sorted(items)] for name, items in groups.items()}


def pick_series():
    """Choose one series to use for the whole video, honouring BACKGROUND_GROUP."""
    groups = background_groups()
    forced = os.environ.get("BACKGROUND_GROUP")
    if forced:
        if forced not in groups:
            print(
                f"ERROR: BACKGROUND_GROUP='{forced}' not found. Available: {sorted(groups)}",
                file=sys.stderr,
            )
            sys.exit(1)
        name = forced
    else:
        name = random.choice(sorted(groups))

    playlist = groups[name]
    print(f"[assemble] background series '{name}': {len(playlist)} clip(s), played in order")
    return playlist


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


def write_concat_list(paths, list_path):
    with open(list_path, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    return list_path


def make_segment(index, audio_path, boundaries_path, bg_list, out_path, seek=0.0):
    duration = get_duration(audio_path)

    with open(boundaries_path, encoding="utf-8") as f:
        boundaries = json.load(f)

    base_vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if boundaries:
        srt_path = f"{SEGMENTS_DIR}/chunk_{index:02d}.srt"
        build_srt(boundaries, srt_path)
        # FFmpeg renders SRT on a default 384x288 ASS canvas that libass then scales
        # to the video height, so FontSize is in THAT space, not in output pixels.
        # Alignment uses legacy SSA numbering: 2 = bottom-centre.
        style = ",".join([
            "FontName=DejaVu Sans",
            "FontSize=16",
            "Bold=1",
            "PrimaryColour=&H00FFFFFF",
            "OutlineColour=&H00000000",
            "BorderStyle=1",
            "Outline=2",
            "MarginV=60",
            "Shadow=1",
            "Alignment=2",
            "MarginL=30",
            "MarginR=30",
        ])
        vf = f"{base_vf},subtitles={srt_path}:force_style='{style}'"
    else:
        print(
            f"ERROR: chunk {index} has no word-boundary data — captions would be missing. "
            f"Check that voice_gen.py wrote {boundaries_path} with events.",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-stream_loop", "-1",
        "-ss", f"{seek:.3f}",
        "-i", bg_list,
        "-i", audio_path,
        "-vf", vf,
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", str(duration),
        "-r", str(FPS),               # normalise fps so concat -c copy stays valid
        "-c:v", "libx264", "-c:a", "aac",
        "-ar", "24000", "-ac", "1",   # normalise audio params too
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    run(cmd)
    return duration


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

    playlist = pick_series()
    os.makedirs(SEGMENTS_DIR, exist_ok=True)

    # Treat the whole series as ONE continuous input via the concat demuxer, so a
    # segment can run across a clip boundary and the cursor just advances through
    # the series timeline. Safe within a series because the clips share encoding.
    bg_list = write_concat_list(playlist, f"{BUILD_DIR}/bg_list.txt")
    series_duration = sum(get_duration(p) for p in playlist)
    print(f"[assemble] series timeline: {series_duration:.0f}s total")

    seek = 0.0
    segment_paths = []
    for i in range(n_chunks):
        audio_path = f"{AUDIO_DIR}/chunk_{i:02d}.mp3"
        boundaries_path = f"{AUDIO_DIR}/chunk_{i:02d}.json"
        out_path = f"{SEGMENTS_DIR}/chunk_{i:02d}.mp4"
        if not os.path.exists(audio_path):
            print(f"ERROR: missing audio file {audio_path}", file=sys.stderr)
            sys.exit(1)

        print(f"[assemble] segment {i + 1}/{n_chunks}: series @ {seek:.1f}s")
        used = make_segment(i, audio_path, boundaries_path, bg_list, out_path, seek=seek)
        seek = (seek + used) % series_duration
        segment_paths.append(out_path)

    final_path = f"{BUILD_DIR}/output.mp4"
    print("[assemble] concatenating segments...")
    concat_segments(segment_paths, final_path)
    print(f"[assemble] wrote {final_path}")


if __name__ == "__main__":
    main()
