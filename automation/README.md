# Faceless Video Automation

Turns any timestamped script in [`../content/`](../content/) into a finished
faceless YouTube video — AI voiceover + visuals + burned-in captions + optional
music — in **one command**. No camera, no mic, **₹0 in API costs** by default.

```
content/*.md  ──►  make_video.py  ──►  output/video.mp4
                   (voice + visuals + captions)
```

---

## What it does

| Step | Tool | Cost |
|------|------|------|
| Narration | `edge-tts` (Indian voices, Neerja/Prabhat) | Free, no API key |
| Visuals | Pexels stock **or** auto-generated text slides | Free (Pexels key optional) |
| Captions | auto `.srt`, burned in | Free |
| Assembly | `ffmpeg` | Free |

Default run uses **slides** for visuals, so it works fully offline (after the
voice download). Add a free Pexels key to swap in real stock footage.

---

## Setup (one time, ~5 min)

**1. Install ffmpeg** (the only non-Python dependency):
- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: `choco install ffmpeg`  (or download from ffmpeg.org and add to PATH)

**2. Install Python packages:**
```bash
cd automation
python3 -m venv .venv && source .venv/bin/activate     # optional but recommended
pip install -r requirements.txt
```

That's it. You're ready.

---

## Run it (step by step)

```bash
# from the automation/ folder
python make_video.py ../content/video-01-7-passive-income-ideas-2026.md
```

You'll see:
```
1/5  Parsing script ...        9 sections, ~1250 spoken words
2/5  Narrating (edge-tts) ...  total runtime ~8:30
3/5  Building captions ...
4/5  Building segments ...
5/5  Assembling final video ...
Done -> .../automation/output/video.mp4
```

Open `output/video.mp4`. Done — that's your faceless video.

> First run downloads the narration (needs internet). Re-runs reuse the cached
> audio in `work/audio/`, so tweaking visuals/captions/music is fast.

---

## Make it better (optional flags)

```bash
# Real stock footage instead of slides — get a free key at pexels.com/api
export PEXELS_API_KEY=xxxxxxxxxxxx
python make_video.py ../content/video-01-7-passive-income-ideas-2026.md

# Male Indian voice, slightly slower
python make_video.py ../content/...md --voice en-IN-PrabhatNeural --rate -5%

# Add a background music bed (auto-kept low under the voice)
python make_video.py ../content/...md --music ~/Music/lofi-bed.mp3

# Vertical 9:16 cut for Shorts / Reels
python make_video.py ../content/...md --portrait -o output/short.mp4

# Skip burned-in captions (e.g. you'll add YouTube's own)
python make_video.py ../content/...md --no-captions
```

### Voice options (edge-tts, free)
- `en-IN-NeerjaNeural` — female, Indian English (default)
- `en-IN-PrabhatNeural` — male, Indian English
- `hi-IN-SwaraNeural` / `hi-IN-MadhurNeural` — fuller Hindi pronunciation

List every voice: `edge-tts --list-voices | grep -i -E "en-IN|hi-IN"`

### Free background music
YouTube Studio → Audio Library, or Pixabay Music. Search "lofi" or
"corporate motivational", download an MP3, pass it with `--music`.

---

## How it works (for tinkering)

`make_video.py` is one file, ~5 stages, all editable:
1. **`parse_script`** — pulls spoken text out of the `### [timestamp] HEADING`
   blocks, stripping stage directions (`[VISUAL: ...]`, `*[pause]*`), markdown,
   and emoji. It also reads the `[VISUAL: ...]` cue to pick a stock-search term.
2. **`narrate`** — edge-tts renders one MP3 per section (cached).
3. **`build_srt`** — splits each section into ~7-word caption chunks, timed to
   the real audio length.
4. **`build_segments`** — one video clip per section (stock or slide), zoomed and
   muxed with its narration.
5. **`concat` + `finalize`** — joins segments, burns captions, mixes music.

Want different visuals for a section? Edit the `[VISUAL: ...]` cue in the script
`.md` — the search term follows it automatically.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **Video much longer than the narration / audio drifts out of sync** | Fixed: duration is now measured by decoding (not unreliable MP3 metadata), and the video + audio tracks are built separately then muxed. Re-render with the latest `make_video.py`. |
| **Video plays but no sound** | Fixed: output is now 48 kHz stereo + faststart. Re-render with the latest `make_video.py`. (edge-tts is 24 kHz mono, which some players play silently.) |
| **Video is "only headers"/slides, no footage** | Expected without a Pexels key — it falls back to text slides. Set `PEXELS_API_KEY` (free, see above) for real stock footage. |
| `'ffmpeg' not found` | Install ffmpeg and reopen the terminal (PATH). |
| Narration sounds robotic on Hindi words | Try `--voice hi-IN-SwaraNeural`. |
| Pexels clips look generic | Sharpen the `[VISUAL: ...]` cue in the script to plain English keywords. |
| Want to re-record one section | Delete that `work/audio/NN.mp3` and re-run. |
| Captions too big/small | Edit `FontSize` in `finalize()`. |

---

## Output layout
```
automation/
├── make_video.py
├── requirements.txt
├── work/              # cache: audio/, visuals/, segments/, captions.srt  (gitignored)
└── output/
    └── video.mp4      # ← upload this
```
