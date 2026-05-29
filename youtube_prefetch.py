"""YouTube yt-dlp 스트림 URL 선로딩 (퍼가기 금지·폴백용)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from panel_log import get_logger

_log = get_logger()
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="yt-prefetch")
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def schedule_youtube_prefetch(
    video_ids: list[str],
    cache_fn: Callable[[str], None],
) -> None:
    """백그라운드에서 스트림 메타를 미리 캐시."""
    for raw in video_ids:
        vid = (raw or "").strip()
        if not vid or len(vid) != 11:
            continue
        with _inflight_lock:
            if vid in _inflight:
                continue
            _inflight.add(vid)
        _pool.submit(_prefetch_one, vid, cache_fn)


def _prefetch_one(video_id: str, cache_fn: Callable[[str], None]) -> None:
    try:
        cache_fn(video_id)
        _log.debug("youtube stream prefetched id=%s", video_id)
    except Exception as exc:
        _log.debug("youtube prefetch skipped id=%s: %s", video_id, exc)
    finally:
        with _inflight_lock:
            _inflight.discard(video_id)
