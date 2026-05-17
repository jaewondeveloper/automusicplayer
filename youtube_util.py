"""YouTube URL 파싱·메타데이터 (yt-dlp)."""
from __future__ import annotations

import re
from typing import Any

import yt_dlp

_YT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_YT_URL_PATTERNS = (
    re.compile(r"(?:youtube\.com/watch\?(?:[^&]+&)*v=|youtube\.com/watch\?v=)([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([a-zA-Z0-9_-]{11})"),
)


def parse_youtube_video_id(value: str) -> str | None:
    """URL 또는 11자리 영상 ID → video id."""
    s = (value or "").strip()
    if not s:
        return None
    if _YT_ID_RE.fullmatch(s):
        return s
    for pat in _YT_URL_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


def fetch_youtube_video_meta(video_id: str) -> dict[str, Any]:
    """제목·썸네일·길이(초) 조회."""
    if not video_id:
        raise ValueError("video_id 필요")
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
    info = info or {}
    vid = info.get("id") or video_id
    thumb = info.get("thumbnail") or ""
    if not thumb:
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "type": "youtube",
        "id": vid,
        "title": info.get("title") or "제목 없음",
        "thumbnail": thumb,
        "duration": max(0.0, duration),
    }
