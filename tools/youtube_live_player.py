#!/usr/bin/env python3
"""
YouTube — 다운로드 없이 최고 화질 실시간 재생 (yt-dlp + ffmpeg/mpv).

사용:
  python tools/youtube_live_player.py VIDEO_ID_OR_URL
  python tools/youtube_live_player.py VIDEO_ID --mpv
  python tools/youtube_live_player.py VIDEO_ID --max-height 2160
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from youtube_dash_mux import (  # noqa: E402
    build_ffmpeg_mux_command,
    build_mpv_command,
    extract_dash_av_urls,
    iter_ffmpeg_mux_stream,
)
from youtube_util import parse_youtube_video_id  # noqa: E402


def play_with_mpv(meta: dict) -> int:
    cmd = build_mpv_command(
        meta["video_url"],
        meta["audio_url"],
        meta.get("http_headers"),
    )
    if not shutil.which("mpv"):
        print("mpv가 PATH에 없습니다. https://mpv.io 설치 후 재시도하세요.", file=sys.stderr)
        return 1
    print("mpv 실행:", " ".join(cmd[:3]), "...")
    return subprocess.call(cmd)


def play_with_ffplay_pipe(meta: dict) -> int:
    """ffmpeg pipe → ffplay (데모)."""
    ffplay = shutil.which("ffplay")
    if not ffplay:
        print("ffplay 없음 — mpv 또는 --save-pipe out.mp4 사용", file=sys.stderr)
        return 1
    ffmpeg_cmd = build_ffmpeg_mux_command(
        meta["video_url"],
        meta["audio_url"],
        meta.get("http_headers"),
    )
    ffplay_cmd = [ffplay, "-autoexit", "-window_title", meta.get("title") or "YouTube", "-"]
    p1 = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return subprocess.call(ffplay_cmd, stdin=p1.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube DASH live mux player")
    parser.add_argument("url", help="YouTube URL or video id")
    parser.add_argument("--mpv", action="store_true", help="mpv로 재생 (권장)")
    parser.add_argument(
        "--max-height",
        type=int,
        default=1080,
        help="영상 최대 높이 (0=제한 없음)",
    )
    parser.add_argument(
        "--audio-kbps",
        type=int,
        default=128,
        help="목표 음성 비트레이트 kbps",
    )
    args = parser.parse_args()
    vid = parse_youtube_video_id(args.url) or args.url.strip()
    if not vid:
        print("video id를 알 수 없습니다.", file=sys.stderr)
        return 1

    print(f"메타 추출 중… id={vid}")
    meta = extract_dash_av_urls(
        vid,
        max_video_height=args.max_height or 99999,
        target_audio_abr=args.audio_kbps,
    )
    print(
        f"  제목: {meta.get('title')}\n"
        f"  영상: {meta.get('height')}p\n"
        f"  음성: ~{meta.get('audio_abr')}kbps\n"
        f"  길이: {meta.get('duration'):.0f}s"
    )

    if args.mpv:
        return play_with_mpv(meta)

    return play_with_ffplay_pipe(meta)


if __name__ == "__main__":
    raise SystemExit(main())
