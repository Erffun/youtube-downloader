from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import subprocess
import re
import os

app = FastAPI()

API_SECRET = os.environ.get("API_SECRET")

if not API_SECRET:
    raise RuntimeError("API_SECRET environment variable is required")

class InspectRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str

def verify_api_key(authorization: str | None):
    expected = f"Bearer {API_SECRET}"

    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

def get_video_info(url: str):
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def estimate_size(fmt):
    return fmt.get("filesize") or fmt.get("filesize_approx")


def sanitize_filename(filename: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
    filename = filename.strip(" .")

    if not filename:
        filename = "youtube-video"

    return filename[:200]


def select_audio(formats):
    """
    Select the best audio-only format.

    Prefer:
    1. m4a
    2. highest abr
    """

    audio_formats = [
        f
        for f in formats
        if f.get("vcodec") == "none"
        and f.get("acodec") != "none"
        and f.get("url")
    ]

    if not audio_formats:
        return None

    m4a = [
        f
        for f in audio_formats
        if f.get("ext") == "m4a"
    ]

    candidates = m4a or audio_formats

    return max(
        candidates,
        key=lambda f: (
            f.get("abr") or 0,
            f.get("tbr") or 0,
        ),
    )


def select_video(formats, height):
    """
    Select the best video-only format for the requested height.

    Prefer:
    - exact height
    - mp4
    - h264
    - higher bitrate
    """

    candidates = [
        f
        for f in formats
        if f.get("vcodec") != "none"
        and f.get("acodec") == "none"
        and f.get("height") == height
        and f.get("url")
    ]

    if not candidates:
        return None

    def score(f):
        ext_score = 1 if f.get("ext") == "mp4" else 0

        codec = f.get("vcodec") or ""
        h264_score = 1 if codec.startswith("avc") else 0

        return (
            ext_score,
            h264_score,
            f.get("tbr") or 0,
            f.get("fps") or 0,
        )

    return max(candidates, key=score)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/inspect")
def inspect(
    req: InspectRequest,
    authorization: str | None = Header(default=None),
):
    verify_api_key(authorization)
    try:
        info = get_video_info(req.url)
        formats = info.get("formats", [])

        audio = select_audio(formats)

        if not audio:
            raise HTTPException(
                status_code=400,
                detail="No audio format found",
            )

        audio_size = estimate_size(audio)

        # Available video resolutions
        heights = sorted(
            {
                f.get("height")
                for f in formats
                if f.get("vcodec") != "none"
                and f.get("acodec") == "none"
                and f.get("height")
            }
        )

        result_formats = []

        for height in heights:
            video = select_video(formats, height)

            if not video:
                continue

            video_size = estimate_size(video)

            estimated_size = None

            if video_size is not None and audio_size is not None:
                estimated_size = video_size + audio_size

            result_formats.append({
                "id": f"{height}p",
                "quality": f"{height}p",
                "height": height,
                "ext": "mp4",
                "hasAudio": True,
                "estimatedSize": estimated_size,
            })

        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "formats": result_formats,
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )


@app.post("/download")
def download(
    req: DownloadRequest,
    authorization: str | None = Header(default=None),
):
    verify_api_key(authorization)
    try:
        info = get_video_info(req.url)
        formats = info.get("formats", [])

        # Example:
        # "720p" -> 720
        match = re.fullmatch(r"(\d+)p", req.format_id)

        if not match:
            raise HTTPException(
                status_code=400,
                detail="Invalid format_id. Expected values like 720p",
            )

        height = int(match.group(1))

        video = select_video(formats, height)

        if not video:
            raise HTTPException(
                status_code=400,
                detail=f"No video format found for {height}p",
            )

        audio = select_audio(formats)

        if not audio:
            raise HTTPException(
                status_code=400,
                detail="No audio format found",
            )

        video_url = video.get("url")
        audio_url = audio.get("url")

        if not video_url:
            raise HTTPException(
                status_code=400,
                detail="Selected video has no direct URL",
            )

        if not audio_url:
            raise HTTPException(
                status_code=400,
                detail="Selected audio has no direct URL",
            )

        command = [
            "ffmpeg",

            "-hide_banner",
            "-loglevel", "error",

            "-i", video_url,
            "-i", audio_url,

            "-map", "0:v:0",
            "-map", "1:a:0",

            # Video is already encoded by YouTube.
            # Copy it to avoid expensive re-encoding.
            "-c:v", "copy",

            # AAC gives us a broadly compatible MP4.
            "-c:a", "aac",
            "-b:a", "128k",

            # Required for streaming MP4 through stdout.
            "-movflags", "frag_keyframe+empty_moov",

            "-f", "mp4",

            "pipe:1",
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        def stream():
            try:
                while True:
                    chunk = process.stdout.read(1024 * 1024)

                    if not chunk:
                        break

                    yield chunk

                return_code = process.wait()

                if return_code != 0:
                    error = process.stderr.read().decode(
                        errors="replace"
                    )

                    print(f"FFmpeg failed: {error}")

            except GeneratorExit:
                # Client disconnected.
                if process.poll() is None:
                    process.kill()

                raise

            finally:
                if process.poll() is None:
                    process.kill()

        filename = sanitize_filename(
            info.get("title") or "youtube-video"
        )

        filename = f"{filename}-{height}p.mp4"

        return StreamingResponse(
            stream(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
            },
        )

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )