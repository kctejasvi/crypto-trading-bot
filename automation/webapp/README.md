# Faceless Video Studio (local web app)

A small Flask front end for the video generator. Runs entirely on your machine —
nothing is uploaded anywhere.

## Run

```bash
cd automation/webapp
pip install -r requirements.txt      # Flask + the render deps
python app.py                        # -> http://127.0.0.1:5000
```

`ffmpeg` must be on PATH (same as the CLI). For real stock footage instead of
slides, set the key before launching:

```bash
export PEXELS_API_KEY=xxxxxxxx
python app.py
```

## What it does

1. Lists every script in `../../content/*.md` in a dropdown.
2. You pick voice, speech rate, captions on/off, and 16:9 vs 9:16.
3. **Generate** runs `../make_video.py` in the background and streams its
   progress (`1/5 … 5/5`) into the page.
4. When it finishes, the MP4 previews inline with a **Download** button.

Outputs land in `../output/`, working files in `../work/` (both gitignored).

## How it's wired

| Route | Purpose |
|-------|---------|
| `GET /` | the page (form + progress area) |
| `POST /generate` | starts a render job, returns a `job_id` |
| `GET /status/<job_id>` | polled once a second for live log + result |
| `GET /video/<job_id>` | serves the finished MP4 (supports seeking) |

Single file: [`app.py`](app.py). Jobs run in background threads; state is kept
in memory (restarting the server clears the job list, not the output files).
