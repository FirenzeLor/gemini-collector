"""
Gemini 媒体工具：视频预览生成、纯媒体辅助函数。
"""

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlunparse, urlencode

PROTECTED_MEDIA_HOSTS = {
    "lh3.google.com",
    "lh3.googleusercontent.com",
    "contribution.usercontent.google.com",
}


def _video_preview_name(media_id):
    stem = Path(media_id).stem
    return f"{stem}_preview.jpg"


def _generate_video_preview(video_path, preview_path):
    """从视频首帧生成固定尺寸预览图（匹配前端预览卡片 160x110）。"""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return False

    preview_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=160:110:force_original_aspect_ratio=increase,crop=160:110",
        "-q:v",
        "4",
        str(preview_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0 and preview_path.exists() and preview_path.stat().st_size > 0
    except Exception:
        return False


def _ensure_video_previews_from_turns(parsed_turns, media_dir):
    """遍历 turn 中的视频附件，确保存在对应首帧预览图。"""
    media_dir = Path(media_dir)
    seen = set()
    stats = {"preview_generated": 0, "preview_failed": 0}

    for parsed in parsed_turns or []:
        if not isinstance(parsed, dict):
            continue
        for role in ("user", "assistant"):
            role_obj = parsed.get(role)
            if not isinstance(role_obj, dict):
                continue
            for f in role_obj.get("files", []) or []:
                if not isinstance(f, dict) or f.get("type") != "video":
                    continue
                media_id = f.get("media_id")
                if not media_id:
                    continue

                preview_id = f.get("preview_media_id") or _video_preview_name(media_id)
                f["preview_media_id"] = preview_id

                key = (media_id, preview_id)
                if key in seen:
                    continue
                seen.add(key)

                video_path = media_dir / media_id
                preview_path = media_dir / preview_id

                if preview_path.exists() and preview_path.stat().st_size > 0:
                    continue
                if not video_path.exists():
                    stats["preview_failed"] += 1
                    continue

                if _generate_video_preview(video_path, preview_path):
                    stats["preview_generated"] += 1
                else:
                    stats["preview_failed"] += 1

    return stats


def _infer_media_type(media_hint):
    if not isinstance(media_hint, str) or not media_hint:
        return "file"
    ext = Path(media_hint).suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "heic", "heif", "svg"}:
        return "image"
    if ext in {"mp4", "mov", "webm", "mkv", "m4v", "avi", "3gp"}:
        return "video"
    if ext in {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "wma", "aiff"}:
        return "audio"
    return "file"


def _media_log_fields(url_text, media_type=None, media_hint=None):
    host = "-"
    if isinstance(url_text, str) and url_text:
        try:
            host = (urlparse(url_text).hostname or "-").lower()
        except Exception:
            host = "-"

    kind = media_type if media_type in {"image", "video", "file"} else None
    if kind is None:
        kind = _infer_media_type(media_hint)
    return {"media": kind, "domain": host}


def _append_authuser(url, authuser):
    if authuser is None:
        return url
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["authuser"] = str(authuser)
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        urlencode(q),
        parsed.fragment,
    ))
