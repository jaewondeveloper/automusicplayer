"""YouTube DASH — 다운로드 없이 ffmpeg로 영상+음성 실시간 mux (pipe)."""
from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from typing import Any

from panel_log import get_logger
from youtube_util import (
    YT_MAX_DOWNLOAD_HEIGHT,
    YT_TARGET_AUDIO_ABR,
    _audio_bitrate_kbps,
    _resolve_ffmpeg,
    _video_only_format_score,
    build_download_ydl_opts,
    ytdlp_extract_info,
)

_log = get_logger()
_mux_proc_lock = threading.Lock()
_active_mux_pids: set[int] = set()


def _format_http_headers(headers: dict[str, str] | None) -> str:
    if not headers:
        return (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36\r\n"
        )
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def extract_dash_av_urls(
    video_id: str,
    *,
    max_video_height: int = YT_MAX_DOWNLOAD_HEIGHT,
    min_video_height: int = YT_MAX_DOWNLOAD_HEIGHT,
    target_audio_abr: int = 0,
) -> dict[str, Any]:
    """
    yt-dlp 메타만 추출 — video direct URL + audio direct URL (파일 저장 없음).
    """
    vid = (video_id or "").strip()
    if not vid:
        raise ValueError("video_id 필요")
    url = f"https://www.youtube.com/watch?v={vid}"
    probe_opts = build_download_ydl_opts(
        "bestvideo+bestaudio/best",
        "%(id)s",
        download=False,
    )
    meta = ytdlp_extract_info(url, probe_opts, download=False)
    formats = list(meta.get("formats") or [])
    if not formats:
        raise ValueError("스트림 포맷 없음")

    videos = [
        f
        for f in formats
        if f.get("url")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") in (None, "none")
        and int(f.get("height") or 0) <= max_video_height
    ]
    audios = [
        f
        for f in formats
        if f.get("url")
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]
    if not videos:
        videos = [
            f
            for f in formats
            if f.get("url")
            and f.get("vcodec") not in (None, "none")
            and f.get("acodec") in (None, "none")
        ]
    if not videos or not audios:
        raise ValueError("DASH 분리 스트림(video/audio)을 찾지 못했습니다")

    hi = [v for v in videos if int(v.get("height") or 0) >= min_video_height]
    if hi:
        videos = hi
    elif min_video_height > 0:
        _log.warning(
            "dash id=%s: %sp 없음, 사용 가능 최고 %sp",
            vid,
            min_video_height,
            max(int(v.get("height") or 0) for v in videos),
        )

    videos.sort(key=_video_only_format_score)
    best_v = videos[-1]
    if target_audio_abr > 0:
        audios.sort(
            key=lambda f: (
                abs(_audio_bitrate_kbps(f) - float(target_audio_abr)),
                -_audio_bitrate_kbps(f),
            )
        )
        best_a = audios[0]
    else:
        audios.sort(key=lambda f: -_audio_bitrate_kbps(f))
        best_a = audios[0]

    http_headers: dict[str, str] = {}
    raw = meta.get("http_headers")
    if isinstance(raw, dict):
        http_headers = {str(k): str(v) for k, v in raw.items()}

    try:
        duration = float(meta.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    height = int(best_v.get("height") or 0)
    return {
        "video_id": vid,
        "video_url": str(best_v["url"]),
        "audio_url": str(best_a["url"]),
        "http_headers": http_headers,
        "height": height,
        "audio_abr": int(_audio_bitrate_kbps(best_a) or 0),
        "duration": max(0.0, duration),
        "title": str(meta.get("title") or ""),
    }


def build_ffmpeg_mux_command(
    video_url: str,
    audio_url: str,
    http_headers: dict[str, str] | None,
) -> list[str]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg를 찾을 수 없습니다 (PATH 또는 imageio-ffmpeg)")
    hdr = _format_http_headers(http_headers)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-headers",
        hdr,
        "-i",
        video_url,
        "-headers",
        hdr,
        "-i",
        audio_url,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


def build_mpv_command(
    video_url: str,
    audio_url: str,
    http_headers: dict[str, str] | None,
) -> list[str]:
    """mpv — DASH 2입력 동시 재생 (별도 창)."""
    mpv = "mpv"
    hdr = _format_http_headers(http_headers).replace("\r\n", "\\r\\n")
    return [
        mpv,
        f"--demuxer-lavf-o=headers={hdr}",
        f"--audio-file={audio_url}",
        video_url,
    ]


def build_ffmpeg_browser_mux_command(
    video_url: str,
    audio_url: str,
    http_headers: dict[str, str] | None,
) -> list[str]:
    """브라우저 <video>용 — DASH 합성 후 H.264/AAC 조각 MP4."""
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg를 찾을 수 없습니다 (PATH 또는 imageio-ffmpeg)")
    hdr = _format_http_headers(http_headers)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-headers",
        hdr,
        "-i",
        video_url,
        "-headers",
        hdr,
        "-i",
        audio_url,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


def iter_ffmpeg_mux_stream_browser(
    video_url: str,
    audio_url: str,
    http_headers: dict[str, str] | None,
    *,
    chunk_size: int = 256 * 1024,
) -> Iterator[bytes]:
    """ffmpeg → 조각 MP4 (브라우저 재생용, copy mux 실패 시)."""
    cmd = build_ffmpeg_browser_mux_command(video_url, audio_url, http_headers)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with _mux_proc_lock:
        _active_mux_pids.add(proc.pid)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
        code = proc.wait(timeout=5)
        if code != 0:
            err = (proc.stderr.read() if proc.stderr else b"").decode(
                "utf-8", errors="replace"
            )[:500]
            raise RuntimeError(f"ffmpeg browser mux 종료 {code}: {err}")
    finally:
        with _mux_proc_lock:
            _active_mux_pids.discard(proc.pid)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def iter_ffmpeg_mux_stream(
    video_url: str,
    audio_url: str,
    http_headers: dict[str, str] | None,
    *,
    chunk_size: int = 256 * 1024,
) -> Iterator[bytes]:
    """ffmpeg stdout 청크 — 저장 없이 브라우저/플레이어로 전달."""
    cmd = build_ffmpeg_mux_command(video_url, audio_url, http_headers)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with _mux_proc_lock:
        _active_mux_pids.add(proc.pid)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
        code = proc.wait(timeout=5)
        if code != 0:
            err = (proc.stderr.read() if proc.stderr else b"").decode(
                "utf-8", errors="replace"
            )[:500]
            raise RuntimeError(f"ffmpeg mux 종료 코드 {code}: {err}")
    finally:
        with _mux_proc_lock:
            _active_mux_pids.discard(proc.pid)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def stop_all_mux_processes() -> None:
    with _mux_proc_lock:
        pids = list(_active_mux_pids)
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
