"""yt-dlp 방송용 로컬 다운로드 캐시 (미리 받아 두고 즉시 재생)."""
from __future__ import annotations

import shutil
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yt_dlp

from config_store import get_install_dir
from panel_log import get_logger
from youtube_util import (
    YT_DOWNLOAD_FORMAT,
    YT_DOWNLOAD_FORMAT_CANDIDATES,
    YT_DOWNLOAD_PLAYER_CLIENT_ROTATIONS,
    YT_MIN_DOWNLOAD_HEIGHT,
    build_download_ydl_opts,
    fetch_youtube_video_meta,
    info_max_height,
    list_video_heights_from_info,
    ytdlp_extract_info,
)

_log = get_logger()
# Windows: 동시 다운로드 시 .part rename 충돌(WinError 32) 방지 — 영상별 tmp 분리
YTDLP_BATCH_WORKERS = 5 if sys.platform == "win32" else 8
_pool = ThreadPoolExecutor(max_workers=YTDLP_BATCH_WORKERS, thread_name_prefix="ytdlp-dl")
_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_ready_lock = threading.Lock()
_ready: dict[str, dict[str, Any]] = {}
_video_locks_guard = threading.Lock()
_video_locks: dict[str, threading.Lock] = {}

_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mkv", ".m4v"})
_MIN_CACHE_BYTES = 32 * 1024
_cache_dir_logged = False


def _looks_like_video_file(path: Path) -> bool:
    """손상·미완료(.part 잔여) 파일을 캐시로 쓰지 않도록 간단 검사."""
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < _MIN_CACHE_BYTES:
        return False
    try:
        with path.open("rb") as f:
            head = f.read(512)
    except OSError:
        return False
    if len(head) < 12:
        return False
    if head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3":
        return True
    return b"ftyp" in head or b"moov" in head or b"webm" in head


def _invalidate_cache_file(video_id: str, *, reason: str = "") -> None:
    vid = (video_id or "").strip()
    if not vid:
        return
    with _ready_lock:
        _ready.pop(vid, None)
    path = local_video_path(vid)
    if path.is_file():
        try:
            path.unlink()
            note = f" ({reason})" if reason else ""
            _log.warning("ytdlp cache removed id=%s path=%s%s", vid, path, note)
        except OSError as exc:
            _log.warning("ytdlp cache remove failed id=%s: %s", vid, exc)
    _cleanup_video_temp_files(ytdlp_cache_dir(), vid)


class _QuietYtdlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _lock_for_video(video_id: str) -> threading.Lock:
    vid = (video_id or "").strip()
    with _video_locks_guard:
        if vid not in _video_locks:
            _video_locks[vid] = threading.Lock()
        return _video_locks[vid]


def _is_windows_file_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "winerror 32" in text
        or "unable to rename file" in text
        or "다른 프로세스가 파일을 사용" in text
    )


def ytdlp_cache_dir() -> Path:
    global _cache_dir_logged
    path = get_install_dir() / "ytdlp_broadcast"
    path.mkdir(parents=True, exist_ok=True)
    if not _cache_dir_logged:
        _cache_dir_logged = True
        _log.info("ytdlp cache dir=%s", path)
    return path


def local_video_path(video_id: str) -> Path:
    return ytdlp_cache_dir() / f"{video_id}.mp4"


def local_playback_url(video_id: str) -> str:
    return f"/api/youtube/local/{video_id}"


def _enforce_min_download_height() -> bool:
    try:
        from config_store import load_config

        return bool(load_config().get("youtube_enforce_min_height"))
    except Exception:
        return False


def is_download_ready(
    video_id: str,
    *,
    min_height: int = 0,
) -> bool:
    vid = (video_id or "").strip()
    if not vid:
        return False
    path = local_video_path(vid)
    if not path.is_file():
        return False
    if not _looks_like_video_file(path):
        _invalidate_cache_file(vid, reason="invalid or incomplete file")
        return False
    need = min_height if min_height > 0 else (
        YT_MIN_DOWNLOAD_HEIGHT if _enforce_min_download_height() else 0
    )
    if need <= 0:
        with _ready_lock:
            cached = _ready.get(vid)
            if cached:
                try:
                    h = int(cached.get("height") or 0)
                except (TypeError, ValueError):
                    h = 0
                if 0 < h < 720:
                    return False
        return True
    with _ready_lock:
        cached = _ready.get(vid)
        if not cached:
            return False
        try:
            h = int(cached.get("height") or 0)
        except (TypeError, ValueError):
            h = 0
        return h >= need


def get_download_entry(video_id: str) -> dict[str, Any] | None:
    vid = (video_id or "").strip()
    if not vid:
        return None
    with _ready_lock:
        cached = _ready.get(vid)
        if cached:
            return dict(cached)
    path = local_video_path(vid)
    if not path.is_file():
        return None
    if not _looks_like_video_file(path):
        _invalidate_cache_file(vid, reason="invalid or incomplete file")
        return None
    entry = {
        "video_id": vid,
        "path": path,
        "url": local_playback_url(vid),
        "duration": 0.0,
    }
    with _ready_lock:
        _ready[vid] = entry
    return dict(entry)


def clear_ytdlp_broadcast_downloads() -> None:
    with _ready_lock:
        _ready.clear()
    cache = ytdlp_cache_dir()
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    _log.info("ytdlp broadcast cache cleared")


def scan_and_repair_ytdlp_cache() -> dict[str, int]:
    """캐시 폴더의 깨진 mp4·잔여 temp 파일 정리. (인터넷 공용 캐시 없음 — 로컬만)"""
    cache = ytdlp_cache_dir()
    removed = 0
    repaired = 0
    if not cache.is_dir():
        return {"removed": 0, "repaired": 0}
    for path in cache.glob("*.mp4"):
        vid = path.stem.strip()
        if len(vid) != 11:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
            continue
        if _looks_like_video_file(path):
            repaired += 1
            continue
        _invalidate_cache_file(vid, reason="scan repair")
        removed += 1
    for pattern in ("*.part", "*.ytdl", "*.temp"):
        for path in cache.glob(pattern):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    tmp_root = cache / "_tmp"
    if tmp_root.is_dir():
        shutil.rmtree(tmp_root, ignore_errors=True)
    _log.info("ytdlp cache scan removed=%s ok=%s dir=%s", removed, repaired, cache)
    return {"removed": removed, "repaired": repaired}


def _cleanup_video_temp_files(out_dir: Path, vid: str) -> None:
    for pattern in (f"{vid}.*.part", f"{vid}.*.ytdl", f"{vid}.*.temp"):
        for path in out_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass
    tmp_root = out_dir / "_tmp" / vid
    if tmp_root.is_dir():
        shutil.rmtree(tmp_root, ignore_errors=True)


def _resolve_downloaded_path(out_dir: Path, vid: str, info: dict[str, Any]) -> Path:
    target = local_video_path(vid)
    if target.is_file() and target.stat().st_size > 0:
        return target

    ext = info.get("ext") or "mp4"
    alt = out_dir / f"{vid}.{ext}"
    if alt.is_file() and alt.stat().st_size > 0:
        if ext != "mp4" and not target.is_file():
            alt.rename(target)
        return target if target.is_file() else alt

    tmp_dir = out_dir / "_tmp" / vid
    search_dirs = [out_dir, tmp_dir] if tmp_dir.is_dir() else [out_dir]
    candidates: list[Path] = []
    for base in search_dirs:
        candidates.extend(
            p
            for p in base.glob(f"{vid}.*")
            if p.is_file() and p.suffix.lower() in _VIDEO_EXTS and p.stat().st_size > 0
        )
    if candidates:
        best = max(candidates, key=lambda p: p.stat().st_size)
        if best != target:
            if target.is_file():
                try:
                    target.unlink()
                except OSError:
                    pass
            try:
                best.rename(target)
            except OSError:
                shutil.copy2(best, target)
                try:
                    best.unlink()
                except OSError:
                    pass
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if target.is_file() and target.stat().st_size > 0:
            return target

    raise ValueError("다운로드 파일을 찾을 수 없습니다")


def _download_video_once(video_id: str) -> dict[str, Any]:
    vid = (video_id or "").strip()
    out_dir = ytdlp_cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / "_tmp" / vid
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_video_temp_files(out_dir, vid)

    url = f"https://www.youtube.com/watch?v={vid}"
    outtmpl = str(tmp_dir / "%(id)s.%(ext)s")

    last_error: Exception | None = None

    def _try_download(format_selector: str, clients: tuple[str, ...], *, quiet: bool) -> dict[str, Any]:
        opts = build_download_ydl_opts(
            format_selector,
            outtmpl,
            player_clients=clients,
            download=True,
        )
        if quiet:
            opts["logger"] = _QuietYtdlpLogger()
        info = ytdlp_extract_info(url, opts, download=True)
        path = _resolve_downloaded_path(out_dir, vid, info)
        try:
            duration = float(info.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            try:
                duration = float(fetch_youtube_video_meta(vid).get("duration") or 0)
            except Exception:
                duration = 0.0
        height = info_max_height(info)
        if height > 0 and height < YT_MIN_DOWNLOAD_HEIGHT:
            if _enforce_min_download_height():
                raise ValueError(
                    f"다운로드 화질 {height}p < 최소 {YT_MIN_DOWNLOAD_HEIGHT}p"
                )
            _log.warning(
                "ytdlp id=%s height=%sp (권장 %sp+, 사용 가능 최고 화질로 저장)",
                vid,
                height,
                YT_MIN_DOWNLOAD_HEIGHT,
            )
        entry = {
            "video_id": vid,
            "path": path,
            "url": local_playback_url(vid),
            "duration": max(0.0, duration),
            "height": height,
            "cached_at": time.time(),
        }
        with _ready_lock:
            _ready[vid] = entry
        av_detail = ""
        req = info.get("requested_formats") or []
        if len(req) >= 2:
            vh = info_max_height(info)
            abr = 0
            for part in req:
                if part.get("acodec") not in (None, "none"):
                    try:
                        abr = max(abr, int(part.get("abr") or part.get("tbr") or 0))
                    except (TypeError, ValueError):
                        pass
            av_detail = f" merged v{vh or height}p+a{abr}k" if abr else f" merged v{vh or height}p"
        elif "+" not in str(format_selector):
            av_detail = " single-file"
        _log.info(
            "ytdlp downloaded id=%s height=%sp selector=%s%s clients=%s",
            vid,
            height or "?",
            format_selector,
            av_detail,
            ",".join(clients),
        )
        return entry

    def _cleanup_attempt() -> None:
        _cleanup_video_temp_files(out_dir, vid)
        target = out_dir / f"{vid}.mp4"
        if target.is_file():
            try:
                target.unlink()
            except OSError:
                pass

    best_entry: dict[str, Any] | None = None
    best_height = 0
    rotations = YT_DOWNLOAD_PLAYER_CLIENT_ROTATIONS
    formats = YT_DOWNLOAD_FORMAT_CANDIDATES
    total_attempts = len(rotations) * len(formats)
    attempt_no = 0

    for clients in rotations:
        for fmt in formats:
            attempt_no += 1
            quiet = attempt_no < total_attempts
            try:
                entry = _try_download(fmt, clients, quiet=quiet)
                height = int(entry.get("height") or 0)
                if height > best_height:
                    best_entry = entry
                    best_height = height
                if height >= YT_MIN_DOWNLOAD_HEIGHT:
                    return entry
            except Exception as exc:
                last_error = exc
                _cleanup_attempt()

    if best_entry is not None:
        _log.info(
            "ytdlp saved best available id=%s height=%sp",
            vid,
            best_height,
        )
        return best_entry

    heights_hint = ""
    try:
        probe_opts = build_download_ydl_opts(
            YT_DOWNLOAD_FORMAT_CANDIDATES[0],
            outtmpl,
            player_clients=YT_DOWNLOAD_PLAYER_CLIENT_ROTATIONS[0],
            download=False,
        )
        probe_opts["logger"] = _QuietYtdlpLogger()
        meta = ytdlp_extract_info(url, probe_opts, download=False)
        heights = list_video_heights_from_info(meta)
        if heights:
            heights_hint = f" (사용 가능 화질: {', '.join(f'{h}p' for h in heights)})"
    except Exception:
        pass

    if last_error:
        msg = str(last_error)
        if heights_hint and heights_hint not in msg:
            raise ValueError(f"{msg}{heights_hint}") from last_error
        raise last_error
    raise ValueError("다운로드에 실패했습니다")


def _download_video(video_id: str) -> dict[str, Any]:
    vid = (video_id or "").strip()
    if not vid:
        raise ValueError("video_id 필요")

    existing = get_download_entry(vid)
    if existing:
        return existing

    lock = _lock_for_video(vid)
    with lock:
        existing = get_download_entry(vid)
        if existing:
            return existing

        last_error: Exception | None = None
        for attempt in range(5):
            try:
                return _download_video_once(vid)
            except Exception as exc:
                last_error = exc
                if _is_windows_file_lock_error(exc) and attempt < 4:
                    wait = 1.5 + attempt * 1.2
                    _log.warning(
                        "ytdlp rename retry id=%s attempt=%s wait=%.1fs",
                        vid,
                        attempt + 1,
                        wait,
                    )
                    time.sleep(wait)
                    _cleanup_video_temp_files(ytdlp_cache_dir(), vid)
                    continue
                raise
        if last_error:
            raise last_error
        raise ValueError("다운로드에 실패했습니다")


def download_youtube_videos_sync(
    video_ids: list[str],
    *,
    max_workers: int | None = None,
    on_progress: Callable[[int, int, str, str | None], None] | None = None,
) -> dict[str, Any]:
    unique: list[str] = []
    for raw in video_ids:
        vid = (raw or "").strip()
        if vid and len(vid) == 11 and vid not in unique:
            unique.append(vid)

    total = len(unique)
    if total == 0:
        if on_progress:
            on_progress(0, 0, "다운로드할 YouTube 곡 없음", None)
        return {"ok": [], "failed": [], "total": 0, "skipped": 0}

    ok: list[str] = []
    failed: list[dict[str, str]] = []
    skipped = 0
    done = 0

    def work(vid: str) -> tuple[str, str | None, bool]:
        if is_download_ready(vid):
            return vid, None, True
        try:
            _download_video(vid)
            return vid, None, False
        except Exception as exc:
            return vid, str(exc), False

    workers = max_workers if max_workers is not None else YTDLP_BATCH_WORKERS
    workers = max(1, min(int(workers), total, YTDLP_BATCH_WORKERS))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, vid): vid for vid in unique}
        for future in as_completed(futures):
            vid, err, was_ready = future.result()
            done += 1
            if was_ready:
                skipped += 1
                ok.append(vid)
                status = f"이미 받음 ({done}/{total})"
            elif err:
                failed.append({"id": vid, "error": err})
                status = f"실패 ({done}/{total}) · {vid}"
                _log.warning("ytdlp download failed id=%s: %s", vid, err)
            else:
                ok.append(vid)
                status = f"다운로드 완료 ({done}/{total})"
            if on_progress:
                on_progress(done, total, status, vid)

    return {"ok": ok, "failed": failed, "total": total, "skipped": skipped}


def ensure_youtube_downloaded(video_id: str) -> dict[str, Any]:
    vid = (video_id or "").strip()
    if is_download_ready(vid):
        entry = get_download_entry(vid)
        if entry:
            return entry
    with _ready_lock:
        _ready.pop(vid, None)
    target = local_video_path(vid)
    if target.is_file():
        try:
            target.unlink()
        except OSError:
            pass
    return _download_video(vid)


def schedule_ytdlp_downloads(video_ids: list[str]) -> None:
    for raw in video_ids:
        vid = (raw or "").strip()
        if not vid or len(vid) != 11:
            continue
        if is_download_ready(vid):
            continue
        lock = _lock_for_video(vid)
        if not lock.acquire(blocking=False):
            continue
        lock.release()
        with _inflight_lock:
            if vid in _inflight:
                continue
            _inflight.add(vid)
        _pool.submit(_download_one, vid)


def schedule_ytdlp_downloads_for_playlist(
    playlist: list[dict[str, Any]],
    *,
    only_required: bool = True,
    from_index: int = 0,
) -> None:
    ids = youtube_ids_from_playlist(
        playlist,
        only_required=only_required,
        from_index=from_index,
    )
    if ids:
        schedule_ytdlp_downloads(ids)


def youtube_ids_from_playlist(
    playlist: list[dict[str, Any]],
    *,
    only_required: bool = False,
    from_index: int = 0,
) -> list[str]:
    ids: list[str] = []
    start = max(0, int(from_index))
    for row in playlist[start:]:
        if not isinstance(row, dict) or row.get("type") != "youtube":
            continue
        if only_required and not row.get("ytdlp_required"):
            continue
        vid = str(row.get("id") or "").strip()
        if vid and len(vid) == 11 and vid not in ids:
            ids.append(vid)
    return ids


def _download_one(video_id: str) -> None:
    try:
        _download_video(video_id)
        _log.info("ytdlp prefetched id=%s", video_id)
    except Exception as exc:
        _log.warning("ytdlp prefetch failed id=%s: %s", video_id, exc)
    finally:
        with _inflight_lock:
            _inflight.discard(video_id)
