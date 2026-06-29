import contextlib
import io
import os
import queue
import random
import shutil
import threading
import traceback
import uuid
from pathlib import Path, PurePosixPath

from flask import Flask, jsonify, render_template, request, send_file

from main import download_bgm_if_url, ensure_dir, list_media_files, render_video


BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))

VIDEO_EXTS = [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"]
AUDIO_EXTS = [".mp3", ".wav", ".m4a", ".aac", ".flac"]

DEFAULT_SCRIPT = (
    "这双鞋软底软面，上脚很舒服。鞋底防滑，日常走路更安心。"
    "整双鞋非常轻便，增高细节自然不夸张。鞋面还能防水防污，"
    "多色配色也很好搭。"
)

BASE_CONFIG = {
    "project_root": str(BASE_DIR),
    "material_root": "",
    "bgm_root": "",
    "output_root": "",
    "temp_root": "",
    "output_filename": "final.mp4",
    "video_size": [1080, 1920],
    "fps": 24,
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "tts_rate": "+0%",
    "tts_volume": "+0%",
    "lead_time": 1.0,
    "max_overlay_duration": 3.0,
    "min_gap": 1.5,
    "min_video_duration": 1.0,
    "fade_duration": 0.25,
    "bgm_volume": 0.2,
    "voice_volume": 1.0,
    "core_selling_points": {
        "软底软面防滑": ["软底", "软面", "防滑"],
        "轻便": ["轻便"],
        "增高细节": ["增高", "细节"],
        "防水防污": ["防水", "防污"],
        "多色展示": ["多色", "颜色", "配色"],
    },
    "core_material_aliases": {
        "软底软面防滑": ["软底软面防滑", "软底软面", "防滑"],
        "轻便": ["轻便"],
        "增高细节": ["增高细节", "增高", "细节"],
        "防水防污": ["防水防污"],
        "多色展示": ["多色展示"],
    },
    "background_folders": ["开箱向上动作", "防尘袋", "穿鞋", "上脚动作", "手持", "开箱", "箱上动作"],
    "video_exts": VIDEO_EXTS,
    "audio_exts": AUDIO_EXTS,
}


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()


class JobLogger:
    def __init__(self, job_id):
        self.job_id = job_id
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                append_log(self.job_id, line.strip())

    def flush(self):
        if self.buffer.strip():
            append_log(self.job_id, self.buffer.strip())
        self.buffer = ""


def append_log(job_id, message):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["logs"].append(str(message))
        job["logs"] = job["logs"][-300:]


def update_job(job_id, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def safe_relative_path(name):
    normalized = name.replace("\\", "/")
    parts = []
    for part in PurePosixPath(normalized).parts:
        if part in ("", ".", ".."):
            continue
        cleaned = part.strip()
        if cleaned:
            parts.append(cleaned)
    if not parts:
        parts = [f"upload_{uuid.uuid4().hex}"]
    return Path(*parts)


def strip_common_top_folder(material_root):
    """
    Browser folder uploads often send paths like A鞋/轻便/1.mp4.
    If every uploaded file is under one common top folder, use that folder as material_root.
    """
    children = [p for p in material_root.iterdir() if p.exists()]
    dirs = [p for p in children if p.is_dir()]
    files = [p for p in children if p.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return material_root


def save_uploads(files, target_dir, allowed_exts):
    ensure_dir(target_dir)
    saved = []
    for storage in files:
        filename = storage.filename or ""
        suffix = Path(filename).suffix.lower()
        if suffix not in allowed_exts:
            continue
        rel_path = safe_relative_path(filename)
        output_path = target_dir / rel_path
        ensure_dir(output_path.parent)
        storage.save(output_path)
        saved.append(output_path)
    return saved


def build_job_config(job_dir, material_root, bgm_root):
    config = dict(BASE_CONFIG)
    config["material_root"] = str(material_root)
    config["bgm_root"] = str(bgm_root)
    config["output_root"] = str(job_dir / "output")
    config["temp_root"] = str(job_dir / "temp")
    config["output_filename"] = "final.mp4"
    config["video_size"] = tuple(config["video_size"])
    return config


def choose_bgm(config, bgm_dir):
    files = list_media_files(bgm_dir, config["audio_exts"])
    return random.choice(files) if files else None


def run_render_job(job_id, script_text, material_root, bgm_root):
    job_dir = JOBS_DIR / job_id
    output_path = job_dir / "output" / "final.mp4"
    config = build_job_config(job_dir, material_root, bgm_root)

    try:
        update_job(job_id, status="running")
        append_log(job_id, "开始生成视频")
        append_log(job_id, f"素材目录：{material_root}")

        bgm_path = choose_bgm(config, bgm_root)
        append_log(job_id, f"BGM：{bgm_path if bgm_path else '未上传，将仅使用口播人声'}")

        logger = JobLogger(job_id)
        with contextlib.redirect_stdout(logger):
            result = render_video(config, script_text, bgm_path)
        logger.flush()

        if not Path(result).exists():
            raise RuntimeError("视频导出结束，但没有找到输出文件。")

        update_job(job_id, status="done", output=str(output_path))
        append_log(job_id, "生成完成，可以下载成片。")
    except Exception:
        append_log(job_id, traceback.format_exc())
        update_job(job_id, status="failed")


@app.route("/")
def index():
    return render_template("index.html", default_script=DEFAULT_SCRIPT, max_upload_mb=MAX_UPLOAD_MB)


@app.post("/api/jobs")
def create_job():
    script_text = request.form.get("script_text", "").strip() or DEFAULT_SCRIPT
    material_files = request.files.getlist("materials")
    bgm_files = request.files.getlist("bgm")

    if not material_files:
        return jsonify({"error": "请先选择商品素材文件夹。"}), 400

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    material_root = job_dir / "material"
    bgm_root = job_dir / "bgm"

    ensure_dir(job_dir)
    saved_materials = save_uploads(material_files, material_root, VIDEO_EXTS)
    save_uploads(bgm_files, bgm_root, AUDIO_EXTS)

    if not saved_materials:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "素材文件夹里没有找到可用视频。"}), 400

    material_root = strip_common_top_folder(material_root)

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "logs": ["任务已创建，等待开始处理。"],
            "output": "",
        }

    thread = threading.Thread(
        target=run_render_job,
        args=(job_id, script_text, material_root, bgm_root),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在。"}), 404
        return jsonify({
            "status": job["status"],
            "logs": job["logs"],
            "download_url": f"/api/jobs/{job_id}/download" if job["status"] == "done" else "",
        })


@app.get("/api/jobs/<job_id>/download")
def download_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "done":
            return jsonify({"error": "视频还没有生成完成。"}), 404
        output = job.get("output")

    return send_file(output, as_attachment=True, download_name="final.mp4")


if __name__ == "__main__":
    ensure_dir(JOBS_DIR)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
