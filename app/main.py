import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Auto Chinese Subtitle Web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

_MODEL_CACHE = {}


class HealthResponse(BaseModel):
    ok: bool
    whisper_ready: bool
    ffmpeg_ready: bool


def ffmpeg_ready() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["ffprobe", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True, whisper_ready=WhisperModel is not None, ffmpeg_ready=ffmpeg_ready())


def get_model(model_size: str) -> WhisperModel:
    if WhisperModel is None:
        raise HTTPException(status_code=500, detail="faster-whisper 未安装。请先安装依赖。")
    if model_size not in _MODEL_CACHE:
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        _MODEL_CACHE[model_size] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _MODEL_CACHE[model_size]


def format_ts(seconds: float) -> str:
    seconds = max(0, seconds)
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    s = whole % 60
    m = (whole // 60) % 60
    h = whole // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def normalize_cn_punctuation(text: str) -> str:
    return (
        text.replace(",", "，")
        .replace("?", "？")
        .replace("!", "！")
        .replace(":", "：")
        .replace(";", "；")
    )


def clean_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return safe or "video"


def extract_audio(video_path: Path, audio_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write_srt(segments, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        idx = 1
        for seg in segments:
            text = (seg["text"] or "").strip()
            if not text:
                continue
            f.write(f"{idx}\n")
            f.write(f"{format_ts(seg['start'])} --> {format_ts(seg['end'])}\n")
            f.write(text + "\n\n")
            idx += 1


def mux_softsub(video_path: Path, srt_path: Path, out_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(srt_path),
        "-c", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi", str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def burn_subtitles(video_path: Path, srt_path: Path, out_path: Path) -> None:
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    vf = f"subtitles='{srt_escaped}':force_style='FontName=Arial,FontSize=18,Outline=1,Shadow=0,MarginV=28'"
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vf", vf, "-c:a", "copy", str(out_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@app.post("/api/subtitle")
async def subtitle(
    file: UploadFile = File(...),
    source_language: str = Form("auto"),
    mode: str = Form("translate"),
    output_mode: str = Form("srt"),
    model_size: str = Form("small"),
):
    if not ffmpeg_ready():
        raise HTTPException(status_code=500, detail="ffmpeg/ffprobe 未安装或不可用。")

    suffix = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}:
        raise HTTPException(status_code=400, detail="暂不支持该视频格式。")

    task_id = uuid.uuid4().hex[:12]
    safe_name = clean_filename(Path(file.filename or f"video{suffix}").stem)
    upload_path = UPLOAD_DIR / f"{task_id}_{safe_name}{suffix}"

    with upload_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    srt_path = OUTPUT_DIR / f"{task_id}_{safe_name}.zh.srt"
    soft_video_path = OUTPUT_DIR / f"{task_id}_{safe_name}.softsub.mp4"
    hard_video_path = OUTPUT_DIR / f"{task_id}_{safe_name}.hardsub.mp4"

    try:
        model = get_model(model_size)
        with tempfile.TemporaryDirectory() as td:
            audio_path = Path(td) / "audio.wav"
            extract_audio(upload_path, audio_path)

            lang: Optional[str] = None if source_language == "auto" else source_language
            task = "translate" if mode == "translate" else "transcribe"
            segments_iter, info = model.transcribe(
                str(audio_path),
                beam_size=5,
                vad_filter=True,
                language=lang,
                task=task,
                word_timestamps=False,
            )

            segments = []
            for seg in segments_iter:
                text = (seg.text or "").strip()
                if task == "translate":
                    text = normalize_cn_punctuation(text)
                segments.append({"start": float(seg.start), "end": float(seg.end), "text": text})

            if not segments:
                raise HTTPException(status_code=400, detail="没有识别到可用的语音内容。")

            write_srt(segments, srt_path)

            download_url = f"/api/download/{srt_path.name}"
            video_url = None
            if output_mode == "softsub":
                mux_softsub(upload_path, srt_path, soft_video_path)
                video_url = f"/api/download/{soft_video_path.name}"
            elif output_mode == "hardsub":
                burn_subtitles(upload_path, srt_path, hard_video_path)
                video_url = f"/api/download/{hard_video_path.name}"

            return {
                "ok": True,
                "task_id": task_id,
                "detected_language": getattr(info, "language", "unknown"),
                "language_probability": getattr(info, "language_probability", 0.0),
                "srt_text": srt_path.read_text(encoding="utf-8"),
                "srt_download_url": download_url,
                "video_download_url": video_url,
            }
    except subprocess.CalledProcessError as e:
        detail = e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e)
        raise HTTPException(status_code=500, detail=f"ffmpeg 执行失败：{detail}")
    finally:
        try:
            upload_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/download/{filename}")
def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在。")
    media_type = "application/octet-stream"
    if path.suffix == ".srt":
        media_type = "text/plain; charset=utf-8"
    elif path.suffix == ".mp4":
        media_type = "video/mp4"
    return FileResponse(str(path), media_type=media_type, filename=path.name)
