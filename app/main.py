import os
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None


BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
STATIC_DIR = APP_DIR / "static"
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="自动中文字幕网页版")


class HealthResponse(BaseModel):
    ok: bool
    whisper_ready: bool
    ffmpeg_ready: bool


def ffmpeg_ready() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["ffprobe", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


def whisper_ready() -> bool:
    if WhisperModel is None:
        return False
    try:
        # 用最小模型做试探，避免 small/medium 导致 Railway 启动失败
        WhisperModel("tiny", device="cpu", compute_type="int8")
        return True
    except Exception:
        return False


def format_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, output_path: Path):
    with output_path.open("w", encoding="utf-8") as f:
        idx = 1
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            f.write(f"{idx}\n")
            f.write(f"{format_ts(seg['start'])} --> {format_ts(seg['end'])}\n")
            f.write(text + "\n\n")
            idx += 1


def extract_audio(video_path: Path, wav_path: Path):
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def mux_softsub(video_path: Path, srt_path: Path, output_path: Path):
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(srt_path),
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=chi",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path):
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    vf = f"subtitles='{srt_escaped}':force_style='FontName=Arial,FontSize=18,Outline=1,Shadow=0,MarginV=28'"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-c:a",
        "copy",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def normalize_text(text: str) -> str:
    return (
        text.replace(",", "，")
        .replace("?", "？")
        .replace("!", "！")
        .replace(":", "：")
        .replace(";", "；")
    )


@app.get("/api/health", response_model=HealthResponse)
def api_health():
    return HealthResponse(
        ok=True,
        whisper_ready=whisper_ready(),
        ffmpeg_ready=ffmpeg_ready(),
    )


@app.post("/api/subtitle")
async def api_subtitle(
    file: UploadFile = File(...),
    source_language: str = Form("auto"),
    mode: str = Form("translate"),
    output_mode: str = Form("srt"),
    model_size: str = Form("tiny"),
):
    if not ffmpeg_ready():
        raise HTTPException(status_code=500, detail="ffmpeg/ffprobe 未安装或不可用")

    if WhisperModel is None:
        raise HTTPException(status_code=500, detail="faster-whisper 未安装或不可用")

    # Railway 上优先强制 tiny，最稳
    safe_model = "tiny"
    if model_size in {"tiny", "base"}:
        safe_model = model_size

    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    file_id = uuid.uuid4().hex
    input_video = UPLOADS_DIR / f"{file_id}{ext}"
    audio_wav = UPLOADS_DIR / f"{file_id}.wav"

    try:
        with input_video.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        extract_audio(input_video, audio_wav)

        lang = None if source_language == "auto" else source_language
        task = "translate" if mode == "translate" else "transcribe"

        model = WhisperModel(
            safe_model,
            device="cpu",
            compute_type="int8",
        )

        segments_iter, info = model.transcribe(
            str(audio_wav),
            beam_size=3,
            vad_filter=True,
            language=lang,
            task=task,
            word_timestamps=False,
        )

        segments = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if task == "translate":
                text = normalize_text(text)
            segments.append(
                {
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "text": text,
                }
            )

        if not segments:
            raise HTTPException(status_code=400, detail="没有识别到可用语音内容")

        stem = Path(file.filename or "subtitle").stem
        srt_name = f"{stem}.{file_id}.zh.srt"
        srt_path = OUTPUTS_DIR / srt_name
        write_srt(segments, srt_path)

        response = {
            "ok": True,
            "detected_language": getattr(info, "language", "unknown"),
            "language_probability": getattr(info, "language_probability", 0.0),
            "srt_filename": srt_name,
            "srt_download_url": f"/api/download/{srt_name}",
            "subtitle_text": srt_path.read_text(encoding="utf-8"),
        }

        if output_mode == "softsub":
            video_name = f"{stem}.{file_id}.softsub.mp4"
            video_path = OUTPUTS_DIR / video_name
            mux_softsub(input_video, srt_path, video_path)
            response["video_filename"] = video_name
            response["video_download_url"] = f"/api/download/{video_name}"

        elif output_mode == "hardsub":
            video_name = f"{stem}.{file_id}.hardsub.mp4"
            video_path = OUTPUTS_DIR / video_name
            burn_subtitles(input_video, srt_path, video_path)
            response["video_filename"] = video_name
            response["video_download_url"] = f"/api/download/{video_name}"

        return response

    except subprocess.CalledProcessError as e:
        err = ""
        try:
            err = e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e)
        except Exception:
            err = str(e)
        raise HTTPException(status_code=500, detail=f"ffmpeg 执行失败: {err}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
    finally:
        try:
            if input_video.exists():
                input_video.unlink()
        except Exception:
            pass
        try:
            if audio_wav.exists():
                audio_wav.unlink()
        except Exception:
            pass


@app.get("/api/download/{filename}")
def api_download(filename: str):
    safe_name = os.path.basename(filename)
    file_path = OUTPUTS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=file_path, filename=safe_name)


# 最后再挂静态目录，避免抢走 /api 路由
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
