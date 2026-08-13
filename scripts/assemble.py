#!/usr/bin/env python3
"""audio + word timings + background series -> captioned vertical MP4s.

Emits one MP4 per ~MAX_VIDEO_SECONDS of content so a long script becomes several
short-form videos rather than one over-long one.
"""
import os
import re
import sys
import json
import glob
import random
import subprocess

BACKGROUND_DIR = "assets"
BUILD_DIR = "build"
AUDIO_DIR = f"{BUILD_DIR}/audio"
SEGMENTS_DIR = f"{BUILD_DIR}/segments"

WORDS_PER_LINE = 3
FPS = 30
CRF = "23"
PRESET = "veryfast"

# Short-form target. A chunk is never split across videos.
# `or` not a default arg: an unset GitHub Actions variable expands to an empty string.
MAX_VIDEO_SECONDS = float(os.environ.get("MAX_VIDEO_SECONDS") or 55)

# Silence padded around each chunk so segments don't cut abruptly.
LEAD_IN = 0.25
TAIL = 0.35

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,78,&H0000E5FF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,320,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


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
    and bare-numeric files (1.mp4, 2.mp4) are each standalone.
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


def ass_timestamp(seconds):
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(boundaries, path, offset=0.0):
    """Word-level karaoke captions: each word fills in as it is spoken."""
    lines = [ASS_HEADER]
    for i in range(0, len(boundaries), WORDS_PER_LINE):
        group = boundaries[i:i + WORDS_PER_LINE]
        start = group[0]["offset_s"] + offset
        end = group[-1]["offset_s"] + group[-1]["duration_s"] + offset
        parts = []
        for w in group:
            k = max(int(round(w["duration_s"] * 100)), 1)
            text = w["text"].replace("{", "").replace("}", "").replace("\n", " ")
            parts.append(f"{{\\k{k}}}{text}")
        lines.append(
            f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,"
            + " ".join(parts)
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_concat_list(paths, list_path):
    with open(list_path, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    return list_path


def rotated_list(playlist, durations, seek, list_path):
    """Build a concat list starting at the clip containing `seek`.

    Input-side -ss is unreliable on the concat demuxer (it silently yields zero or
    too few video frames), so the offset is applied with the trim filter instead.
    Rotating the list first keeps the decode bounded to one clip rather than
    decoding the whole series up to the offset.
    """
    remaining = seek
    start_idx = 0
    for i, path in enumerate(playlist):
        if remaining < durations[path]:
            start_idx = i
            break
        remaining -= durations[path]
    else:
        remaining = 0.0

    rotated = playlist[start_idx:] + playlist[:start_idx]
    write_concat_list(rotated, list_path)
    return list_path, remaining


def make_segment(index, audio_path, boundaries_path, bg_list, out_path, seek=0.0, trim=0.0):
    speech = get_duration(audio_path)
    duration = speech + LEAD_IN + TAIL

    with open(boundaries_path, encoding="utf-8") as f:
        boundaries = json.load(f)
    if not boundaries:
        print(
            f"ERROR: chunk {index} has no word timings — captions would be missing.",
            file=sys.stderr,
        )
        sys.exit(1)

    ass_path = f"{SEGMENTS_DIR}/chunk_{index:02d}.ass"
    build_ass(boundaries, ass_path, offset=LEAD_IN)

    vf = (
        f"trim=start={trim:.3f},setpts=PTS-STARTPTS,"
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,ass={ass_path}"
    )
    af = (
        f"adelay={int(LEAD_IN * 1000)}:all=1,"
        f"apad=pad_dur={TAIL},"
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )

    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-stream_loop", "-1",
        "-i", bg_list,
        "-i", audio_path,
        "-vf", vf,
        "-af", af,
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{duration:.3f}",
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", PRESET, "-crf", CRF,
        "-c:a", "aac", "-ar", "24000", "-ac", "1",
        "-pix_fmt", "yuv420p",
        out_path,
    ])
    return duration


def concat_segments(segment_paths, out_path):
    list_path = f"{BUILD_DIR}/concat_{os.path.basename(out_path)}.txt"
    write_concat_list(segment_paths, list_path)
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy", out_path,
    ])


def plan_videos(durations):
    """Pack chunks into videos of at most MAX_VIDEO_SECONDS. Never splits a chunk."""
    videos, current, total = [], [], 0.0
    for i, d in enumerate(durations):
        if current and total + d > MAX_VIDEO_SECONDS:
            videos.append(current)
            current, total = [], 0.0
        current.append(i)
        total += d
    if current:
        videos.append(current)
    return videos


def main():
    with open(f"{BUILD_DIR}/script.json", encoding="utf-8") as f:
        data = json.load(f)
    n_chunks = len(data["chunks"])

    playlist = pick_series()
    os.makedirs(SEGMENTS_DIR, exist_ok=True)

    clip_durations = {p: get_duration(p) for p in playlist}
    series_duration = sum(clip_durations.values())
    print(f"[assemble] series timeline: {series_duration:.0f}s total")

    seek = 0.0
    segment_paths, segment_durations = [], []
    for i in range(n_chunks):
        audio_path = f"{AUDIO_DIR}/chunk_{i:02d}.mp3"
        boundaries_path = f"{AUDIO_DIR}/chunk_{i:02d}.json"
        out_path = f"{SEGMENTS_DIR}/chunk_{i:02d}.mp4"
        if not os.path.exists(audio_path):
            print(f"ERROR: missing audio file {audio_path}", file=sys.stderr)
            sys.exit(1)

        bg_list, trim = rotated_list(
            playlist, clip_durations, seek, f"{BUILD_DIR}/bg_{i:02d}.txt"
        )
        print(f"[assemble] segment {i + 1}/{n_chunks}: series @ {seek:.1f}s")
        used = make_segment(
            i, audio_path, boundaries_path, bg_list, out_path, seek=seek, trim=trim
        )
        seek = (seek + used) % series_duration
        segment_paths.append(out_path)
        segment_durations.append(used)

    videos = plan_videos(segment_durations)
    print(f"[assemble] packing {n_chunks} chunks into {len(videos)} video(s)")

    outputs = []
    for n, indices in enumerate(videos, 1):
        out_path = f"{BUILD_DIR}/output_{n:02d}.mp4"
        length = sum(segment_durations[i] for i in indices)
        print(f"[assemble] video {n}: chunks {indices[0] + 1}-{indices[-1] + 1}, {length:.0f}s")
        concat_segments([segment_paths[i] for i in indices], out_path)
        outputs.append(out_path)

    print(f"[assemble] wrote {len(outputs)} file(s): {', '.join(outputs)}")


if __name__ == "__main__":
    main()
