#!/usr/bin/env python3
"""
make_video.py — Faceless YouTube video generator.

Turns a timestamped markdown script (the ones in ../content/) into a finished
MP4: AI voiceover + b-roll/slides + burned-in captions + optional music.

Design goals:
  * FREE by default — uses edge-tts (no API key) for narration and auto-generated
    text slides for visuals. Add a free Pexels key to swap slides for real stock.
  * One command. Re-runnable. Caches narration so re-renders are fast.

Pipeline:
  1. Parse the markdown into ordered sections (heading + spoken text + visual cue).
  2. Narrate each section with edge-tts  -> work/audio/NN.mp3
  3. Measure each clip's duration (ffprobe).
  4. Build captions (.srt) timed proportionally to each section.
  5. Get a visual per section: Pexels stock video (if key) else a generated slide.
  6. ffmpeg: build one segment per section, concat, burn captions, mix music.

Usage:
  pip install -r requirements.txt          # edge-tts, requests, pillow
  # ffmpeg must be on PATH (brew install ffmpeg / apt install ffmpeg / choco install ffmpeg)
  python make_video.py ../content/video-01-7-passive-income-ideas-2026.md

Common flags:
  --voice en-IN-PrabhatNeural     male Indian voice (default: en-IN-NeerjaNeural)
  --rate -5%                      slow narration down a touch
  --music path/to/bed.mp3         background music (kept low, auto-ducked volume)
  --no-captions                   skip burned-in subtitles
  --pexels-key KEY                or set env PEXELS_API_KEY for real stock footage
  --portrait                      9:16 (Shorts) instead of 16:9
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# Constant output frame rate. Every clip is forced to this so the concatenated
# video track has a single, uniform timebase.
FPS = 25

# -----------------------------------------------------------------------------
# Section model + markdown parsing
# -----------------------------------------------------------------------------

@dataclass
class Section:
    index: int
    heading: str            # e.g. "IDEA #1 — DIGITAL PRODUCTS"
    timecode: str           # e.g. "0:45-2:15"
    spoken: str             # clean narration text (no cues, no markdown)
    visual_query: str       # search terms for stock footage
    audio_path: Path | None = None
    duration: float = 0.0   # seconds, filled after narration
    caption_lines: list[str] = field(default_factory=list)


# A "### [0:00–0:15] HOOK" style heading. Handles both - and – dashes.
HEADING_RE = re.compile(r"^#{2,4}\s*\[([^\]]+)\]\s*(.+?)\s*$")
# Any inline or standalone cue: *[VISUAL: ...]*, [pause], *[slow]*, etc.
CUE_RE = re.compile(r"\*?\[[^\]]*\]\*?")
VISUAL_RE = re.compile(r"\[VISUAL:\s*(.+?)\]", re.IGNORECASE)


def _clean_spoken(raw: str) -> str:
    """Strip markdown + stage directions, leaving only words to be spoken."""
    text = CUE_RE.sub(" ", raw)              # remove [..] / *[..]* cues
    text = re.sub(r"\*{1,3}", "", text)       # bold/italic markers
    text = re.sub(r"^>.*$", "", text, flags=re.MULTILINE)  # blockquotes
    text = re.sub(r"^\s*-{3,}\s*$", "", text, flags=re.MULTILINE)  # --- rules
    text = re.sub(r"`+", "", text)            # code ticks
    # Drop emoji / pictographs so TTS doesn't read "fire emoji" etc.
    text = re.sub(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF←-⇿⬀-⯿]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def parse_script(md_path: Path) -> list[Section]:
    """Extract the spoken script: everything inside the THE COMPLETE SCRIPT block."""
    raw = md_path.read_text(encoding="utf-8")

    # Narrow to the script body if the marker exists (avoids summaries/comments).
    start = raw.find("## THE COMPLETE SCRIPT")
    if start != -1:
        # body ends at the first "---\n---" hard divider after the script
        rest = raw[start:]
        end = rest.find("\n---\n---")
        body = rest[:end] if end != -1 else rest
    else:
        body = raw

    sections: list[Section] = []
    cur_heading = cur_tc = None
    cur_visual = "abstract motivational background"
    buf: list[str] = []

    def flush():
        if cur_heading is None:
            return
        spoken = _clean_spoken("\n".join(buf))
        if not spoken:
            return
        sections.append(Section(
            index=len(sections) + 1,
            heading=cur_heading,
            timecode=cur_tc or "",
            spoken=spoken,
            visual_query=cur_visual,
        ))

    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            cur_tc, cur_heading = m.group(1).strip(), m.group(2).strip()
            cur_visual = "abstract motivational background"
            buf = []
            continue
        vm = VISUAL_RE.search(line)
        if vm and cur_heading is not None:
            # Use the first clause of the visual cue (up to ~5 words) as the query.
            clause = re.split(r"[,;—]| - ", vm.group(1))[0].strip()
            clause = re.sub(r"[^\w\s]", "", clause)          # drop punctuation/quotes
            words = [w for w in clause.split() if not w.isdigit()][:5]
            if words:
                cur_visual = " ".join(words)
        buf.append(line)
    flush()
    return sections


# -----------------------------------------------------------------------------
# Narration (edge-tts, free, no key)
# -----------------------------------------------------------------------------

async def _tts_one(text: str, out: Path, voice: str, rate: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    await communicate.save(str(out))


def narrate(sections: list[Section], audio_dir: Path, voice: str, rate: str):
    audio_dir.mkdir(parents=True, exist_ok=True)
    for s in sections:
        out = audio_dir / f"{s.index:02d}.mp3"
        if not out.exists():
            print(f"  [tts] {s.index:02d} {s.heading[:40]}")
            asyncio.run(_tts_one(s.spoken, out, voice, rate))
        s.audio_path = out
        s.duration = probe_duration(out)


# -----------------------------------------------------------------------------
# ffmpeg / ffprobe helpers
# -----------------------------------------------------------------------------

def run(cmd: list[str]):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"Command failed:\n  {' '.join(cmd)}\n{proc.stderr[-800:]}")
    return proc


def probe_duration(path: Path) -> float:
    """Exact media duration by *decoding* the stream.

    edge-tts (and many encoders) write inaccurate duration metadata into MP3
    headers; trusting `format=duration` made video segments run past the
    narration, ballooning the total length. Decoding to null and reading the
    final timestamp is slower but always correct.
    """
    proc = subprocess.run(["ffmpeg", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True)
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", proc.stderr)
    if times:
        h, m, s = times[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    # Fallback to container metadata if decoding produced no timestamp.
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


# -----------------------------------------------------------------------------
# Captions (.srt)
# -----------------------------------------------------------------------------

def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"


def build_srt(sections: list[Section], out: Path):
    """Split each section's text into ~7-word caption chunks, timed proportionally."""
    entries, idx, clock = [], 1, 0.0
    for s in sections:
        words = s.spoken.replace("\n", " ").split()
        if not words or s.duration <= 0:
            clock += s.duration
            continue
        chunks, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) >= 7:
                chunks.append(" ".join(cur)); cur = []
        if cur:
            chunks.append(" ".join(cur))
        per = s.duration / len(chunks)
        for c in chunks:
            start, end = clock, clock + per
            entries.append(f"{idx}\n{_ts(start)} --> {_ts(end)}\n{c}\n")
            idx += 1
            clock = end
    out.write_text("\n".join(entries), encoding="utf-8")


# -----------------------------------------------------------------------------
# Visuals: Pexels stock (optional) or generated slide (default)
# -----------------------------------------------------------------------------

def pexels_clip(query: str, dest: Path, key: str, w: int, h: int) -> bool:
    import requests
    orient = "portrait" if h > w else "landscape"
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": 1, "orientation": orient},
            timeout=30,
        )
        r.raise_for_status()
        vids = r.json().get("videos", [])
        if not vids:
            return False
        files = sorted(vids[0]["video_files"],
                       key=lambda f: (f.get("width") or 0), reverse=True)
        url = files[0]["link"]
        with requests.get(url, stream=True, timeout=120) as dl:
            dl.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in dl.iter_content(1 << 16):
                    fh.write(chunk)
        return True
    except Exception as e:
        print(f"  [pexels] '{query}' failed ({e}); using slide.")
        return False


def make_slide(section: Section, dest: Path, w: int, h: int):
    """Generate a clean gradient slide with the heading + timecode as a PNG."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (w, h), (12, 16, 28))
    draw = ImageDraw.Draw(img)
    # simple vertical gradient
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)],
                  fill=(int(12 + 24 * t), int(16 + 30 * t), int(28 + 55 * t)))

    def font(size):
        for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    title = section.heading
    wrapped = textwrap.fill(title, width=22)
    f_title = font(int(h * 0.075))
    f_tc = font(int(h * 0.03))

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=f_title, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.multiline_text(((w - tw) / 2, (h - th) / 2), wrapped,
                        font=f_title, fill=(245, 245, 250), align="center")
    draw.text((w * 0.06, h * 0.08), f"[{section.timecode}]",
              font=f_tc, fill=(120, 180, 255))
    img.save(dest)


def visual_clip_from_image(img: Path, dur: float, out: Path, w: int, h: int):
    """Still image -> SILENT video of exactly `dur` seconds, constant 25 fps."""
    run(["ffmpeg", "-y", "-loop", "1", "-t", f"{dur}", "-i", str(img),
         "-vf", f"scale={w}:{h},format=yuv420p", "-r", f"{FPS}",
         "-fps_mode", "cfr", "-c:v", "libx264", "-an", str(out)])


def visual_clip_from_video(vid: Path, dur: float, out: Path, w: int, h: int):
    """Stock clip -> looped/trimmed to exactly `dur`, SILENT, constant 25 fps."""
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},format=yuv420p")
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-t", f"{dur}", "-i", str(vid),
         "-vf", vf, "-r", f"{FPS}", "-fps_mode", "cfr",
         "-c:v", "libx264", "-an", str(out)])


# -----------------------------------------------------------------------------
# Assembly
#
# The video and audio tracks are built SEPARATELY then muxed once, instead of
# building per-section A+V clips and concatenating them. This guarantees:
#   * each section's silent visual is exactly its narration length (no -t drift
#     from unreliable MP3 metadata -> no ballooning total),
#   * the narration is one continuous AAC encode (concatenating per-section AAC
#     with -c copy inserts encoder-priming gaps that accumulate into A/V drift).
# -----------------------------------------------------------------------------

def build_video_track(sections, work: Path, key: str | None, w: int, h: int) -> Path:
    vis_dir = work / "visuals"; clip_dir = work / "clips"
    vis_dir.mkdir(parents=True, exist_ok=True); clip_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for s in sections:
        clip = clip_dir / f"{s.index:02d}.mp4"
        got_stock = False
        if key:
            stock = vis_dir / f"{s.index:02d}.mp4"
            if stock.exists() or pexels_clip(s.visual_query, stock, key, w, h):
                visual_clip_from_video(stock, s.duration, clip, w, h)
                got_stock = True
        if not got_stock:
            slide = vis_dir / f"{s.index:02d}.png"
            make_slide(s, slide, w, h)
            visual_clip_from_image(slide, s.duration, clip, w, h)
        print(f"  [clip] {s.index:02d} ({s.duration:.1f}s){'  [stock]' if got_stock else ''}")
        clips.append(clip)

    listfile = work / "video_concat.txt"
    listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in clips))
    out = work / "video_track.mp4"
    # Uniform codec/fps/timebase across clips -> stream copy concat is safe.
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(out)])
    return out


def build_audio_track(sections, work: Path) -> Path:
    """Concatenate narration MP3s into ONE continuous 48 kHz stereo AAC track."""
    listfile = work / "audio_concat.txt"
    listfile.write_text("".join(f"file '{s.audio_path.resolve()}'\n" for s in sections))
    out = work / "audio_track.m4a"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", str(out)])
    return out


def finalize(video_track: Path, audio_track: Path,
             srt: Path | None, music: Path | None, out: Path):
    cmd = ["ffmpeg", "-y", "-i", str(video_track), "-i", str(audio_track)]
    filters, maps = [], []
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    if srt:
        style = "FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=1"
        filters.append(f"[0:v]subtitles='{srt.as_posix()}':force_style='{style}'[v]")
        maps += ["-map", "[v]"]
    else:
        maps += ["-map", "0:v"]
    if music:
        filters.append("[2:a]volume=0.12[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]")
        maps += ["-map", "[a]"]
    else:
        maps += ["-map", "1:a"]
    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    # 48 kHz stereo AAC + faststart (moov atom up front) so the voiceover plays
    # in every player and streams cleanly; matches YouTube's recommended spec.
    cmd += maps + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                   "-movflags", "+faststart", "-shortest", str(out)]
    run(cmd)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Faceless video generator")
    ap.add_argument("script", type=Path, help="path to the markdown script")
    ap.add_argument("-o", "--output", type=Path, default=Path("output/video.mp4"))
    ap.add_argument("--voice", default="en-IN-NeerjaNeural")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--music", type=Path)
    ap.add_argument("--pexels-key", default=os.environ.get("PEXELS_API_KEY"))
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--portrait", action="store_true", help="9:16 instead of 16:9")
    ap.add_argument("--workdir", type=Path, default=Path("work"))
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"'{tool}' not found on PATH. Install ffmpeg first.")

    w, h = (1080, 1920) if args.portrait else (1920, 1080)

    print("1/5  Parsing script ...")
    sections = parse_script(args.script)
    if not sections:
        sys.exit("No spoken sections found — check the script format.")
    words = sum(len(s.spoken.split()) for s in sections)
    print(f"     {len(sections)} sections, ~{words} spoken words")

    print("2/5  Narrating (edge-tts) ...")
    narrate(sections, args.workdir / "audio", args.voice, args.rate)
    total = sum(s.duration for s in sections)
    print(f"     total runtime ~{int(total // 60)}:{int(total % 60):02d}")

    srt = None
    if not args.no_captions:
        print("3/5  Building captions ...")
        srt = args.workdir / "captions.srt"
        build_srt(sections, srt)
    else:
        print("3/5  Captions skipped")

    print("4/5  Building video + audio tracks ...")
    video_track = build_video_track(sections, args.workdir, args.pexels_key, w, h)
    audio_track = build_audio_track(sections, args.workdir)

    print("5/5  Assembling final video ...")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    finalize(video_track, audio_track, srt, args.music, args.output)
    print(f"\nDone -> {args.output.resolve()}")


if __name__ == "__main__":
    main()
