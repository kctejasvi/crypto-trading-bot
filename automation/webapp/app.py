#!/usr/bin/env python3
"""
Local web front end for the faceless video generator.

A tiny Flask app that runs entirely on your machine. It lists the scripts in
content/, lets you pick voice/format options, runs automation/make_video.py in
the background while streaming its progress, then previews and serves the MP4.

Run:
    cd automation/webapp
    pip install -r requirements.txt          # Flask (make_video's deps too)
    python app.py                            # -> http://127.0.0.1:5000

Notes:
  * ffmpeg must be on PATH (same requirement as the CLI).
  * For real stock footage instead of slides, set PEXELS_API_KEY in your shell
    before launching:  export PEXELS_API_KEY=xxxx  (the render inherits it).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, abort

# ---- paths (resolved relative to this file, so CWD doesn't matter) ----------
WEBAPP_DIR = Path(__file__).resolve().parent
AUTOMATION_DIR = WEBAPP_DIR.parent
REPO_ROOT = AUTOMATION_DIR.parent
CONTENT_DIR = REPO_ROOT / "content"
MAKE_VIDEO = AUTOMATION_DIR / "make_video.py"
OUTPUT_DIR = AUTOMATION_DIR / "output"
WORK_DIR = AUTOMATION_DIR / "work"

VOICES = [
    ("en-IN-NeerjaNeural", "Neerja — female, Indian English"),
    ("en-IN-PrabhatNeural", "Prabhat — male, Indian English"),
    ("hi-IN-SwaraNeural", "Swara — female, fuller Hindi"),
    ("hi-IN-MadhurNeural", "Madhur — male, fuller Hindi"),
]

app = Flask(__name__)

# In-memory job registry: job_id -> {log: [str], done: bool, ok: bool, output: Path|None}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def list_scripts() -> list[str]:
    if not CONTENT_DIR.is_dir():
        return []
    return sorted(p.name for p in CONTENT_DIR.glob("*.md"))


def run_job(job_id: str, script: str, voice: str, rate: str,
            portrait: bool, captions: bool):
    slug = Path(script).stem
    out_path = OUTPUT_DIR / f"{slug}-{job_id[:8]}.mp4"
    cmd = [sys.executable, str(MAKE_VIDEO), str(CONTENT_DIR / script),
           "--voice", voice, "--rate", rate,
           "--output", str(out_path), "--workdir", str(WORK_DIR / job_id[:8])]
    if portrait:
        cmd.append("--portrait")
    if not captions:
        cmd.append("--no-captions")

    def log(line: str):
        with JOBS_LOCK:
            JOBS[job_id]["log"].append(line.rstrip())

    log(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, cwd=str(AUTOMATION_DIR))
        for line in proc.stdout:               # stream progress live
            log(line)
        proc.wait()
        ok = proc.returncode == 0 and out_path.exists()
        with JOBS_LOCK:
            JOBS[job_id]["ok"] = ok
            JOBS[job_id]["done"] = True
            JOBS[job_id]["output"] = out_path if ok else None
        log("Done." if ok else f"Failed (exit {proc.returncode}).")
    except Exception as e:  # pragma: no cover - defensive
        log(f"Error: {e}")
        with JOBS_LOCK:
            JOBS[job_id]["ok"] = False
            JOBS[job_id]["done"] = True


@app.route("/")
def index():
    return render_template("index.html", scripts=list_scripts(), voices=VOICES)


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True)
    script = data.get("script", "")
    if script not in list_scripts():
        return jsonify({"error": "Unknown script"}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"log": [], "done": False, "ok": False, "output": None}
    t = threading.Thread(target=run_job, args=(
        job_id, script,
        data.get("voice", VOICES[0][0]),
        data.get("rate", "+0%"),
        bool(data.get("portrait")),
        bool(data.get("captions", True)),
    ), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({
            "log": job["log"],
            "done": job["done"],
            "ok": job["ok"],
            "video_url": f"/video/{job_id}" if job["ok"] else None,
        })


@app.route("/video/<job_id>")
def video(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("output"):
        abort(404)
    return send_file(job["output"], mimetype="video/mp4",
                     as_attachment=False, conditional=True)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Faceless video studio -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
