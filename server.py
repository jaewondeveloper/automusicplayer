"""Flask + SocketIO 서버."""
from __future__ import annotations

import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import bcrypt
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from flask_socketio import SocketIO, disconnect, emit
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from werkzeug.utils import secure_filename

import cloudflare_sync
from config_store import (
    ALLOWED_UPLOAD_EXT,
    ASSETS_DIR,
    BUNDLE_DIR,
    DEFAULT_NEXT_ALERT_LOGO,
    UPLOADS_DIR,
    WEBSITE_PORT,
    broadcast_ui_config,
    bundled_assets_dir,
    bundle_dir,
    ensure_dirs,
    is_setup_complete,
    load_config,
    normalize_alert_theme,
    youtube_embed_only,
    youtube_stream_only,
    normalize_next_alert_logo,
    normalize_next_alert_text,
    resolve_alert_logo_url,
    save_config,
)
from network_utils import network_access_urls, panel_urls
from panel_log import get_logger
from panel_window import enqueue_panel_window_command
from playback_recovery import (
    bump_stream_generation,
    current_stream_generation,
    playback_recovery,
    set_prep_running_check,
)
from youtube_download_cache import (
    clear_ytdlp_broadcast_downloads,
    download_youtube_videos_sync,
    ensure_youtube_downloaded,
    get_download_entry,
    is_download_ready,
    local_playback_url,
    schedule_ytdlp_downloads,
    schedule_ytdlp_downloads_for_playlist,
    youtube_ids_from_playlist,
)
from playlist_store import load_playlist, save_playlist
from state import BroadcastState
from youtube_search import search_youtube
from youtube_util import (
    YT_MIN_DOWNLOAD_HEIGHT,
    fetch_youtube_progressive_stream,
    fetch_youtube_stream_info,
    fetch_youtube_video_meta,
    inspect_youtube_playback_mode,
    parse_youtube_video_id,
)

# --- 전역 ---
broadcast_state = BroadcastState()
config_data: dict[str, Any] = {}
login_attempts: dict[str, dict[str, Any]] = {}
broadcast_command_queue: queue.Queue | None = None
_panel_sids: set[str] = set()
_panel_sids_lock = threading.Lock()
_yt_stream_cache: dict[str, dict[str, Any]] = {}
_yt_progressive_cache: dict[str, dict[str, Any]] = {}
_yt_dash_cache: dict[str, dict[str, Any]] = {}
_yt_stream_cache_lock = threading.Lock()
_ytdlp_scan_lock = threading.Lock()
_ytdlp_scan_running = False
_ytdlp_scan_pending = False
YTDLP_SCAN_MAX_WORKERS = 6

_embed_scan_lock = threading.Lock()
_embed_scan_done = threading.Event()
_embed_scan_results: list[dict[str, Any]] = []
_embed_scan_broadcast_ready = False
_embed_scan_pending_payload: dict[str, Any] | None = None
_embed_scan_client_ready = threading.Event()
_prep_token = 0
EMBED_PROBE_SECONDS = 4


class BroadcastPrepAborted(Exception):
    """방송 준비 중 사용자가 종료했거나 새 준비가 시작됨."""

app = Flask(
    __name__,
    static_folder=str(BUNDLE_DIR / "panel" / "static"),
    static_url_path="/static",
    template_folder=str(BUNDLE_DIR / "panel"),
)
csrf = CSRFProtect()
login_manager = LoginManager()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


class AdminUser(UserMixin):
    def __init__(self, username: str):
        self.id = username


@login_manager.user_loader
def load_user(user_id: str) -> AdminUser | None:
    cfg = load_config()
    if user_id == cfg.get("admin_username"):
        return AdminUser(user_id)
    return None


def set_broadcast_queue(q: queue.Queue) -> None:
    global broadcast_command_queue
    broadcast_command_queue = q


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()


def _is_local_request() -> bool:
    ip = _client_ip()
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")


def _register_panel_client() -> None:
    with _panel_sids_lock:
        _panel_sids.add(request.sid)


def _unregister_panel_client() -> None:
    with _panel_sids_lock:
        _panel_sids.discard(request.sid)


def _panel_sids_snapshot() -> list[str]:
    with _panel_sids_lock:
        return list(_panel_sids)


def _disconnect_panel_clients() -> None:
    for sid in _panel_sids_snapshot():
        try:
            socketio.server.disconnect(sid)
        except Exception:
            pass
    with _panel_sids_lock:
        _panel_sids.clear()


def _emit_panel_session_status() -> None:
    """원격 브라우저·패널에 앱 연결 상태 브로드캐스트."""
    panel_online = _has_panel_client()
    socketio.emit(
        "session_status",
        {
            "panel_online": panel_online,
            "broadcast_allowed": panel_online,
        },
    )


def _stop_broadcast_playback() -> None:
    if broadcast_command_queue:
        broadcast_command_queue.put({"action": "close_broadcast"})
    broadcast_state.stop()
    bump_stream_generation()
    _emit_now_playing()
    _emit_playback_status()


def _prep_alive(token: int) -> bool:
    with _ytdlp_scan_lock:
        return token == _prep_token


def _queue_broadcast_window(
    display_index: int,
    *,
    embed_scan: bool = False,
    wait_open: bool = False,
) -> None:
    if broadcast_command_queue:
        broadcast_command_queue.put(
            {
                "action": "open_broadcast",
                "display_index": display_index,
                "embed_scan": embed_scan,
                "wait_open": wait_open,
            }
        )


def _close_broadcast_window() -> None:
    if broadcast_command_queue:
        broadcast_command_queue.put({"action": "close_broadcast"})


def _close_broadcast_window_and_wait(timeout: float = 20.0) -> None:
    if not broadcast_command_queue:
        return
    done = threading.Event()
    broadcast_command_queue.put({"action": "close_broadcast", "done": done})
    done.wait(timeout=timeout)


def _clear_ytdlp_download_failed_flags() -> None:
    pl = broadcast_state.get_playlist_dicts()
    merged: list[dict[str, Any]] = []
    changed = False
    for row in pl:
        item = dict(row)
        if item.pop("ytdlp_download_failed", False):
            changed = True
        merged.append(item)
    if changed:
        broadcast_state.set_playlist(merged)
        _persist_playlist()
        _emit_playlist()


def _mark_ytdlp_download_failures(failed: list[dict[str, str]]) -> None:
    failed_ids = {
        str(row.get("id") or "").strip()
        for row in failed
        if str(row.get("id") or "").strip()
    }
    if not failed_ids:
        return
    for vid in failed_ids:
        _mark_video_ytdlp_download_failed(vid)


def _clear_ytdlp_required_for_playlist() -> None:
    """YouTube 퍼가기 전용 모드 — 플레이리스트 yt-dlp 플래그 해제."""
    merged: list[dict[str, Any]] = []
    changed = False
    for item in broadcast_state.get_playlist_dicts():
        row = dict(item)
        if row.get("type") == "youtube":
            if row.get("ytdlp_required") or not row.get("ytdlp_checked"):
                row["ytdlp_required"] = False
                row["ytdlp_checked"] = True
                row["ytdlp_reason"] = "embed_only"
                changed = True
        merged.append(row)
    if changed:
        broadcast_state.set_playlist(merged)
        _persist_playlist()
        _emit_playlist()


def _mark_video_ytdlp_download_failed(video_id: str) -> None:
    vid = (video_id or "").strip()
    if not vid:
        return
    pl = broadcast_state.get_playlist_dicts()
    merged: list[dict[str, Any]] = []
    changed = False
    for row in pl:
        item = dict(row)
        if str(item.get("id") or "").strip() == vid:
            if not item.get("ytdlp_download_failed"):
                item["ytdlp_download_failed"] = True
                changed = True
        merged.append(item)
    if changed:
        broadcast_state.set_playlist(merged)
        _persist_playlist()
        _emit_playlist()


def _playlist_item_row(item: Any) -> dict[str, Any]:
    if item is None:
        return {}
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return dict(item)


def _is_track_unplayable(item: dict[str, Any] | Any | None) -> bool:
    """다운로드가 실패로 표시된 곡만 자동 건너뜀."""
    row = _playlist_item_row(item) if not isinstance(item, dict) else item
    if not row or row.get("type") != "youtube":
        return False
    return bool(row.get("ytdlp_download_failed"))


def _skip_to_next_track(reason: str = "") -> bool:
    """재생 불가 곡 건너뛰기. False 면 방송 종료."""
    item = broadcast_state.current_item()
    title = (item.title if item else "") or "YouTube"
    idx = broadcast_state.current_index
    if reason:
        get_logger().warning("skip track index=%s title=%s: %s", idx, title, reason)
    else:
        get_logger().warning("skip track index=%s title=%s", idx, title)
    socketio.emit(
        "track_skipped",
        {
            "message": reason or f"재생 불가 — {title}",
            "index": idx,
            "title": title,
        },
        namespace="/broadcast",
    )
    nxt = broadcast_state.advance_next()
    _emit_now_playing()
    _emit_playback_status()
    _emit_playlist()
    if nxt:
        _advance_past_unplayable_tracks()
        resync_broadcast_clients(allow_during_scan=True)
        _notify_now_playing(nxt.title)
        return True
    _finalize_broadcast_ended()
    return False


def _hard_reset_for_broadcast_start() -> None:
    """방송 시작마다 첫 방송과 같이 상태·창·준비 플래그 초기화."""
    global _embed_scan_results, _embed_scan_broadcast_ready, _embed_scan_pending_payload

    _cancel_broadcast_prep()
    _close_broadcast_window_and_wait()
    try:
        from broadcast_window import close_external_youtube

        close_external_youtube()
    except Exception:
        pass
    broadcast_state.stop()
    bump_stream_generation()
    with _embed_scan_lock:
        _embed_scan_results = []
        _embed_scan_broadcast_ready = False
        _embed_scan_pending_payload = None
    _embed_scan_done.clear()
    _embed_scan_client_ready.clear()
    _clear_ytdlp_download_failed_flags()
    try:
        from youtube_download_cache import scan_and_repair_ytdlp_cache

        scan_and_repair_ytdlp_cache()
    except Exception as exc:
        get_logger().warning("ytdlp cache scan failed: %s", exc)
    try:
        playback_recovery.dismiss_error()
    except Exception:
        pass
    socketio.emit("broadcast_prep_reset", {})
    socketio.emit("broadcast_prep_reset", namespace="/broadcast")
    get_logger().info("broadcast start hard reset complete")


def _restart_broadcast_window(
    display_index: int,
    *,
    embed_scan: bool = False,
    timeout: float = 40.0,
) -> bool:
    """종료 화면 등 이전 키오스크를 닫은 뒤 방송 창을 다시 연다."""
    if not broadcast_command_queue:
        return False
    cfg = load_config()
    port = int(cfg.get("port", WEBSITE_PORT))
    done = threading.Event()
    ok: list[bool] = [False]
    broadcast_command_queue.put(
        {
            "action": "restart_broadcast",
            "display_index": display_index,
            "embed_scan": embed_scan,
            "port": port,
            "done": done,
            "ok": ok,
        }
    )
    if not done.wait(timeout=timeout):
        get_logger().error("restart broadcast window timed out (%.0fs)", timeout)
        return False
    if not ok[0]:
        get_logger().error("restart broadcast window failed display=%s", display_index)
    return bool(ok[0])


def _begin_broadcast_prep() -> int:
    """새 방송 준비 세션 시작. 반환 토큰으로 취소 여부 판별."""
    global _prep_token, _ytdlp_scan_running, _embed_scan_broadcast_ready
    with _ytdlp_scan_lock:
        _prep_token += 1
        token = _prep_token
        _ytdlp_scan_running = True
        _embed_scan_broadcast_ready = False
    _embed_scan_done.clear()
    _embed_scan_client_ready.clear()
    return token


def _cancel_broadcast_prep() -> None:
    """준비 스레드 중단·UI 잠금 해제 (방송 종료·재시작 시)."""
    global _prep_token, _embed_scan_pending_payload
    with _ytdlp_scan_lock:
        _prep_token += 1
        _embed_scan_pending_payload = None
        _ytdlp_scan_running = False
    _embed_scan_done.set()
    _embed_scan_client_ready.clear()
    try:
        playback_recovery.dismiss_error()
    except Exception:
        pass
    _emit_ytdlp_scan_progress(
        0, 1, "", False, include_broadcast=True, phase=""
    )
    socketio.emit("broadcast_prep_reset", {})
    socketio.emit("broadcast_prep_reset", namespace="/broadcast")


def _finalize_broadcast_ended(*, close_window: bool = False) -> None:
    """방송 종료 — 종료 화면 표시 (창은 방송 화면 ESC 두 번째에 닫음)."""
    _cancel_broadcast_prep()
    broadcast_state.stop()
    bump_stream_generation()
    _emit_now_playing()
    _emit_playback_status()
    socketio.emit("broadcast_ended", {}, namespace="/broadcast")
    if close_window:
        _close_broadcast_window()


def _has_panel_client() -> bool:
    with _panel_sids_lock:
        return len(_panel_sids) > 0


def _broadcast_playback_allowed() -> bool:
    return current_user.is_authenticated and _has_panel_client()


def _check_login_lock() -> tuple[bool, int]:
    """잠금 여부, 남은 초."""
    ip = _client_ip()
    rec = login_attempts.get(ip, {"count": 0, "lock_until": None})
    lock_until = rec.get("lock_until")
    if lock_until and datetime.utcnow() < lock_until:
        return True, int((lock_until - datetime.utcnow()).total_seconds()) + 1
    return False, 0


def _record_login_failure() -> None:
    ip = _client_ip()
    rec = login_attempts.setdefault(ip, {"count": 0, "lock_until": None})
    rec["count"] = rec.get("count", 0) + 1
    if rec["count"] >= 5:
        rec["lock_until"] = datetime.utcnow() + timedelta(seconds=30)
        rec["count"] = 0


def _clear_login_failures() -> None:
    login_attempts.pop(_client_ip(), None)


def _persist_playlist() -> None:
    """플레이리스트를 디스크에 저장."""
    save_playlist(broadcast_state.get_playlist_dicts())


def _emit_playlist() -> None:
    socketio.emit("playlist_update", {"playlist": broadcast_state.get_playlist_dicts()})


def _emit_now_playing() -> None:
    item = broadcast_state.current_item()
    socketio.emit(
        "now_playing",
        {
            "index": broadcast_state.current_index,
            "title": item.title if item else "",
            "type": item.type if item else "",
        },
    )
def _emit_playback_status() -> None:
    socketio.emit(
        "playback_status",
        {"status": broadcast_state.playback_status},
    )
    socketio.emit(
        "playback_status",
        {"status": broadcast_state.playback_status},
        namespace="/broadcast",
    )


def _emit_broadcast_track() -> None:
    """방송 키오스크 화면에 현재 트랙 반영 (다운로드 실패 곡은 자동 건너뜀)."""
    _advance_past_unplayable_tracks()
    resync_broadcast_clients(allow_during_scan=True)


def _prefetch_playlist_streams(around_index: int | None = None) -> None:
    """yt-dlp 필요 곡만 백그라운드 다운로드 (임베드 가능 곡 제외)."""
    if youtube_stream_only() or youtube_embed_only():
        return
    snap = broadcast_state.snapshot()
    pl = snap.get("playlist") or []
    cur = (
        int(around_index)
        if around_index is not None
        else int(snap.get("current_index", -1))
    )
    schedule_ytdlp_downloads_for_playlist(
        pl,
        only_required=True,
        from_index=max(0, cur),
    )


def _prefetch_all_ytdlp_in_playlist() -> None:
    schedule_ytdlp_downloads_for_playlist(
        broadcast_state.get_playlist_dicts(),
        only_required=True,
        from_index=0,
    )


def _emit_mux_playback(
    video_id: str,
    *,
    title: str,
    duration: float,
    index: int,
) -> None:
    """스트림 재생 (/api/youtube/stream) — 캐시는 prefetch가 담당."""
    socketio.emit(
        "youtube_stream_playback",
        {
            "url": f"/api/youtube/stream/{video_id}",
            "video_id": video_id,
            "title": title,
            "duration": max(0.0, duration),
            "index": index,
            "local": False,
            "mux": False,
        },
        namespace="/broadcast",
    )


def _emit_ytdlp_local_playback(
    video_id: str,
    *,
    title: str,
    duration: float,
    index: int,
) -> None:
    """yt-dlp 로컬 파일 재생 (stream 모드만 실시간 스트림 폴백)."""
    if youtube_stream_only():
        _emit_mux_playback(
            video_id,
            title=title,
            duration=duration,
            index=index,
        )
        return
    entry = get_download_entry(video_id)
    if not entry:
        item = broadcast_state.current_item()
        if _is_track_unplayable(item):
            _skip_to_next_track(f"다운로드 실패 — {title}")
            return
        if item and item.ytdlp_required:
            _mark_video_ytdlp_download_failed(video_id)
            _skip_to_next_track(f"다운로드 실패 — {title}")
            return
        _emit_mux_playback(
            video_id,
            title=title,
            duration=duration,
            index=index,
        )
        return
    dur = float(entry.get("duration") or duration or 0)
    socketio.emit(
        "youtube_stream_playback",
        {
            "url": entry.get("url") or local_playback_url(video_id),
            "video_id": video_id,
            "title": title,
            "duration": dur,
            "index": index,
            "local": True,
        },
        namespace="/broadcast",
    )


def _emit_ytdlp_playback_if_ready(index: int) -> None:
    """yt-dlp 필요 곡만 로컬/스트림 재생 (임베드 곡은 playCurrent → iframe)."""
    if youtube_embed_only():
        return
    if index != broadcast_state.current_index:
        return
    item = broadcast_state.current_item()
    if not item or item.type != "youtube" or not item.ytdlp_required:
        return
    video_id = str(item.id or "").strip()
    if not video_id:
        return
    dur = float(item.duration or 0)
    if _is_track_unplayable(item):
        _skip_to_next_track(f"다운로드 실패 — {item.title or 'YouTube'}")
        return
    if youtube_stream_only():
        _emit_mux_playback(
            video_id,
            title=item.title or "YouTube",
            duration=dur,
            index=index,
        )
        return
    if not is_download_ready(video_id):
        _play_ytdlp_at_index(
            video_id,
            index,
            title=item.title or "YouTube",
            mark_required=False,
        )
        return
    _emit_ytdlp_local_playback(
        video_id,
        title=item.title or "YouTube",
        duration=dur,
        index=index,
    )


def _advance_past_unplayable_tracks() -> None:
    pl = broadcast_state.get_playlist_dicts()
    for _ in range(len(pl) + 1):
        item = broadcast_state.current_item()
        if not item:
            return
        if not _is_track_unplayable(item):
            return
        row = _playlist_item_row(item)
        title = row.get("title") or "YouTube"
        if not _skip_to_next_track(f"다운로드 실패 — {title}"):
            return


def resync_broadcast_clients(*, allow_during_scan: bool = False) -> None:
    """방송 브라우저에 현재 재생 상태를 다시 보냄 (창이 늦게 열릴 때 유실 방지)."""
    if _ytdlp_scan_running and not allow_during_scan:
        return
    _advance_past_unplayable_tracks()
    snap = broadcast_state.snapshot()
    idx = int(snap.get("current_index", -1))
    status = snap.get("playback_status", "stopped")
    get_logger().info("broadcast resync index=%s status=%s", idx, status)
    playback_recovery.notify_track_sync(idx, status)
    socketio.emit("load_track", snap, namespace="/broadcast")
    socketio.emit("playback_status", {"status": status}, namespace="/broadcast")
    if idx >= 0 and status in ("playing", "paused"):
        _prefetch_playlist_streams(idx)
        _emit_ytdlp_playback_if_ready(idx)


def _cache_youtube_progressive(video_id: str) -> dict[str, Any]:
    with _yt_stream_cache_lock:
        cached = _yt_progressive_cache.get(video_id)
        if cached and cached.get("expires", 0) > time.time():
            return cached
    info = fetch_youtube_progressive_stream(video_id)
    entry = {**info, "expires": time.time() + 7200}
    with _yt_stream_cache_lock:
        _yt_progressive_cache[video_id] = entry
    return entry


def _cache_youtube_dash(video_id: str) -> dict[str, Any]:
    with _yt_stream_cache_lock:
        cached = _yt_dash_cache.get(video_id)
        if cached and cached.get("expires", 0) > time.time():
            return cached
    from youtube_dash_mux import extract_dash_av_urls

    info = extract_dash_av_urls(
        video_id,
        min_video_height=YT_MIN_DOWNLOAD_HEIGHT,
    )
    entry = {**info, "expires": time.time() + 7200}
    with _yt_stream_cache_lock:
        _yt_dash_cache[video_id] = entry
    return entry


def _cache_youtube_stream(video_id: str) -> dict[str, Any]:
    with _yt_stream_cache_lock:
        cached = _yt_stream_cache.get(video_id)
        if cached and cached.get("expires", 0) > time.time():
            return cached
    info = fetch_youtube_stream_info(video_id, min_height=0)
    entry = {**info, "expires": time.time() + 7200}
    with _yt_stream_cache_lock:
        _yt_stream_cache[video_id] = entry
    return entry


def _try_emit_hd_stream_playback(
    video_id: str,
    index: int,
    *,
    title: str = "",
    min_height: int = YT_MIN_DOWNLOAD_HEIGHT,
) -> bool:
    """로컬 파일 없을 때 DASH 실시간 mux 재생."""
    if not load_config().get("youtube_allow_stream_fallback", True):
        return False
    try:
        dur = _fetch_youtube_duration(video_id)
        _emit_mux_playback(
            video_id,
            title=title,
            duration=dur,
            index=index,
        )
        get_logger().info("ytdlp → mux stream id=%s index=%s", video_id, index)
        return True
    except Exception as exc:
        get_logger().warning(
            "ytdlp mux fallback failed id=%s: %s", video_id, exc
        )
        return False


def notify_youtube_stream_failed(
    finished_index: int,
    message: str = "",
    *,
    title: str = "",
) -> None:
    try:
        finished_index = int(finished_index)
    except (TypeError, ValueError):
        finished_index = broadcast_state.current_index
    if (
        finished_index >= 0
        and finished_index != broadcast_state.current_index
    ):
        return
    socketio.emit(
        "youtube_stream_failed",
        {
            "message": message,
            "index": finished_index,
            "title": title,
        },
        namespace="/broadcast",
    )
    reason = message or f"재생 실패 — {title or 'YouTube'}"
    _skip_to_next_track(reason)


def notify_youtube_browser_fallback_started(
    video_id: str,
    finished_index: int,
    title: str = "",
) -> None:
    socketio.emit(
        "youtube_browser_fallback_started",
        {
            "id": video_id,
            "index": finished_index,
            "title": title,
        },
        namespace="/broadcast",
    )


def finish_external_youtube_playback(finished_index: int | None = None) -> None:
    """임베드 불가 영상을 외부 브라우저로 재생한 뒤 다음 곡으로 진행."""
    if finished_index is not None:
        try:
            finished_index = int(finished_index)
        except (TypeError, ValueError):
            finished_index = None
    if finished_index is not None and broadcast_state.current_index > finished_index:
        _emit_broadcast_track()
        socketio.emit("external_playback_done", {}, namespace="/broadcast")
        return
    item = broadcast_state.advance_next()
    _emit_now_playing()
    _emit_playback_status()
    _emit_playlist()
    socketio.emit("external_playback_done", {}, namespace="/broadcast")
    if item:
        _emit_broadcast_track()
        _notify_now_playing(item.title)
    else:
        _finalize_broadcast_ended()


def _notify_broadcast(title: str, event: str) -> None:
    socketio.emit(event, {"title": title}, namespace="/broadcast")


def _notify_now_playing(title: str) -> None:
    """수동 이전/다음 — 짧은 안내만 (다음 곡 10초 안내는 막지 않음)."""
    socketio.emit(
        "now_playing_toast",
        {"title": title, "brief": True},
        namespace="/broadcast",
    )


def _fetch_youtube_duration(video_id: str) -> float:
    if not video_id:
        return 0.0
    try:
        return float(fetch_youtube_video_meta(video_id).get("duration") or 0)
    except Exception:
        return 0.0


def _emit_ytdlp_scan_progress(
    done: int,
    total: int,
    status: str,
    running: bool,
    *,
    include_broadcast: bool = False,
    phase: str = "",
    percent: int | None = None,
) -> None:
    if percent is None:
        pct = int((done * 100) / total) if total > 0 else (0 if running else 100)
    else:
        pct = int(percent)
    payload = {
        "done": done,
        "total": total,
        "percent": max(0, min(100, pct)),
        "status": status,
        "running": running,
        "phase": phase,
    }
    socketio.emit("ytdlp_scan_progress", payload)
    if include_broadcast:
        socketio.emit("ytdlp_scan_progress", payload, namespace="/broadcast")


def _mark_playlist_ytdlp_required(video_id: str, reason: str = "embed_blocked_runtime") -> None:
    """방송 중 실제 yt-dlp 폴백이 발생한 영상에 배지를 반영."""
    if not video_id:
        return
    pl = broadcast_state.get_playlist_dicts()
    changed = False
    for item in pl:
        if item.get("type") == "youtube" and str(item.get("id") or "").strip() == video_id:
            item["ytdlp_required"] = True
            item["ytdlp_checked"] = True
            item["ytdlp_reason"] = reason
            changed = True
    if changed:
        broadcast_state.set_playlist(pl)
        _persist_playlist()
        _emit_playlist()


def _scan_one_youtube_item(item: dict[str, Any]) -> dict[str, Any]:
    video_id = str(item.get("id") or "").strip()
    if not video_id:
        return {"checked": False, "required": False, "reason": "missing_id"}
    try:
        inspect_info = inspect_youtube_playback_mode(video_id)
        return {
            "checked": True,
            "required": bool(inspect_info.get("requires_ytdlp")),
            "reason": str(inspect_info.get("reason") or ""),
            "ytdlp_probe_ok": not bool(inspect_info.get("requires_ytdlp")),
        }
    except Exception as exc:
        return {
            "checked": False,
            "required": False,
            "reason": f"scan_error:{exc}",
            "ytdlp_probe_ok": False,
        }


def _scan_playlist_for_ytdlp_required(
    *,
    phase: str,
    include_broadcast: bool = False,
) -> int:
    """퍼가기(iframe) 불가 곡만 ytdlp_required 로 표시."""
    original = broadcast_state.get_playlist_dicts()
    youtube_jobs = [
        dict(row) for row in original if row.get("type") == "youtube"
    ]
    total = len(youtube_jobs)
    verdict_by_video_id: dict[str, dict[str, Any]] = {}

    _emit_ytdlp_scan_progress(
        0,
        max(total, 1),
        f"{phase} · 임베드 재생 검사 0/{total}곡",
        True,
        include_broadcast=include_broadcast,
        phase=phase,
    )

    if youtube_jobs:
        max_workers = max(1, min(YTDLP_SCAN_MAX_WORKERS, len(youtube_jobs)))
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(_scan_one_youtube_item, item): str(item.get("id") or "").strip()
                for item in youtube_jobs
            }
            for future in as_completed(future_map):
                video_id = future_map[future]
                verdict = future.result()
                if video_id:
                    verdict_by_video_id[video_id] = verdict
                done += 1
                _emit_ytdlp_scan_progress(
                    done,
                    total,
                    f"{phase} · 검사 중 ({done}/{total})",
                    True,
                    include_broadcast=include_broadcast,
                    phase=phase,
                )

    latest = broadcast_state.get_playlist_dicts()
    merged: list[dict[str, Any]] = []
    required_count = 0
    for row in latest:
        item = dict(row)
        if item.get("type") != "youtube":
            item["ytdlp_checked"] = True
            item["ytdlp_required"] = False
            item["ytdlp_reason"] = "local"
        else:
            video_id = str(item.get("id") or "").strip()
            verdict = verdict_by_video_id.get(video_id)
            if verdict:
                item["ytdlp_checked"] = bool(verdict.get("checked"))
                item["ytdlp_required"] = bool(verdict.get("required"))
                item["ytdlp_reason"] = str(verdict.get("reason") or "")
                item["ytdlp_probe_ok"] = bool(verdict.get("ytdlp_probe_ok"))
            else:
                item["ytdlp_checked"] = False
                item["ytdlp_required"] = False
                item["ytdlp_reason"] = "pending_scan"
            if youtube_embed_only():
                item["ytdlp_required"] = False
            elif item["ytdlp_required"]:
                required_count += 1
        merged.append(item)

    broadcast_state.set_playlist(merged)
    _persist_playlist()
    _emit_playlist()
    return required_count


def _download_ytdlp_required_in_playlist(
    *,
    phase: str,
    include_broadcast: bool = False,
    prep_token: int | None = None,
) -> dict[str, Any]:
    """yt-dlp 필요 곡 — 방송 시작 전 고화질 다운로드."""
    if youtube_embed_only():
        _emit_ytdlp_scan_progress(
            1,
            1,
            f"{phase} · YouTube 퍼가기 (최고 화질, 다운로드 생략)",
            False,
            include_broadcast=include_broadcast,
            phase=phase,
        )
        return {"ok": [], "failed": [], "total": 0, "skipped": 0}
    if youtube_stream_only():
        ids = youtube_ids_from_playlist(
            broadcast_state.get_playlist_dicts(),
            only_required=True,
        )
        n = len(ids)
        _emit_ytdlp_scan_progress(
            1,
            1,
            f"{phase} · 스트리밍 재생 준비 완료 ({n}곡, 파일 저장 없음)",
            False,
            include_broadcast=include_broadcast,
            phase=phase,
        )
        return {"ok": ids, "failed": [], "total": n, "skipped": n}
    if prep_token is not None and not _prep_alive(prep_token):
        raise BroadcastPrepAborted()
    ids = youtube_ids_from_playlist(
        broadcast_state.get_playlist_dicts(),
        only_required=True,
    )
    total = len(ids)

    download_phase = "방송준비중"

    def on_progress(done: int, total_n: int, status: str, _vid: str | None) -> None:
        if prep_token is not None and not _prep_alive(prep_token):
            raise BroadcastPrepAborted()
        _emit_ytdlp_scan_progress(
            done,
            total_n,
            status,
            True,
            include_broadcast=include_broadcast,
            phase=download_phase,
        )

    if total == 0:
        _emit_ytdlp_scan_progress(
            1,
            1,
            f"{phase} · yt-dlp 필요 곡 없음 (전부 퍼가기 재생)",
            False,
            include_broadcast=include_broadcast,
            phase=phase,
        )
        return {
            "ok": [],
            "failed": [],
            "total": 0,
            "youtube_total": 0,
            "ytdlp_required": 0,
            "scan_failed": 0,
        }

    if prep_token is not None and not _prep_alive(prep_token):
        raise BroadcastPrepAborted()

    ready_n = sum(1 for vid in ids if is_download_ready(vid))
    if ready_n >= total:
        _emit_ytdlp_scan_progress(
            total,
            max(total, 1),
            f"저장된 고화질 영상 {total}곡 사용 (재다운로드 없음)",
            False,
            include_broadcast=include_broadcast,
            phase="방송준비중",
        )
        return {
            "ok": list(ids),
            "failed": [],
            "total": total,
            "skipped": total,
            "youtube_total": total,
            "ytdlp_required": total,
            "scan_failed": 0,
        }

    _emit_ytdlp_scan_progress(
        0,
        total,
        f"yt-dlp 다운로드 0/{total}곡"
        + (f" (이미 {ready_n}곡)" if ready_n else ""),
        True,
        include_broadcast=include_broadcast,
        phase="방송준비중",
    )
    result = download_youtube_videos_sync(ids, on_progress=on_progress)
    if prep_token is not None and not _prep_alive(prep_token):
        raise BroadcastPrepAborted()
    failed = result.get("failed") or []
    ok_n = len(result.get("ok") or [])
    if failed:
        _mark_ytdlp_download_failures(failed)
        msg = f"완료 · yt-dlp {ok_n}곡 / 실패 {len(failed)}곡 (실패 곡은 방송 중 건너뜀)"
    else:
        msg = f"yt-dlp 다운로드 완료 · {ok_n}곡"
    _emit_ytdlp_scan_progress(
        total,
        max(total, 1),
        msg,
        False,
        include_broadcast=include_broadcast,
        phase="방송준비중",
    )
    return {
        **result,
        "youtube_total": total,
        "ytdlp_required": ok_n,
        "scan_failed": len(failed),
    }


def _apply_embed_scan_results(results: list[dict[str, Any]]) -> int:
    """방송 화면 임베드 검사 결과를 플레이리스트에 반영."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in results:
        vid = str(row.get("id") or "").strip()
        if vid:
            by_id[vid] = row
    pl = broadcast_state.get_playlist_dicts()
    required_count = 0
    merged: list[dict[str, Any]] = []
    for row in pl:
        item = dict(row)
        if item.get("type") != "youtube":
            item["ytdlp_checked"] = True
            item["ytdlp_required"] = False
            item["ytdlp_reason"] = "local"
        else:
            video_id = str(item.get("id") or "").strip()
            verdict = by_id.get(video_id)
            if verdict:
                item["ytdlp_checked"] = True
                item["ytdlp_required"] = bool(verdict.get("required"))
                item["ytdlp_reason"] = str(verdict.get("reason") or "")
            else:
                item["ytdlp_checked"] = True
                item["ytdlp_required"] = False
                item["ytdlp_reason"] = "no_probe"
            if youtube_embed_only():
                item["ytdlp_required"] = False
            elif item["ytdlp_required"]:
                required_count += 1
        merged.append(item)
    broadcast_state.set_playlist(merged)
    _persist_playlist()
    _emit_playlist()
    return required_count


def _prepare_broadcast_youtube(display_index: int, prep_token: int) -> None:
    """방송 시작 전: 방송 화면에서 임베드 검사 → yt-dlp 필요 곡 다운로드."""
    global _embed_scan_broadcast_ready, _embed_scan_results, _embed_scan_pending_payload

    def _abort_if_cancelled() -> None:
        if not _prep_alive(prep_token):
            raise BroadcastPrepAborted()

    phase = "방송 시작 전"
    youtube_jobs = [
        {"id": str(row.get("id") or "").strip(), "title": str(row.get("title") or "")}
        for row in broadcast_state.get_playlist_dicts()
        if row.get("type") == "youtube" and str(row.get("id") or "").strip()
    ]
    total = len(youtube_jobs)
    scan_payload = {
        "videos": youtube_jobs,
        "probe_seconds": EMBED_PROBE_SECONDS,
    }

    _embed_scan_done.clear()
    _embed_scan_results = []
    _embed_scan_client_ready.clear()
    with _embed_scan_lock:
        _embed_scan_broadcast_ready = False
        _embed_scan_pending_payload = scan_payload

    _abort_if_cancelled()
    enqueue_panel_window_command("minimize")
    from youtube_util import refresh_youtube_cookies_file, resolve_youtube_cookiefile

    _emit_ytdlp_scan_progress(
        0,
        max(total, 1),
        "YouTube 쿠키 확인 중…",
        True,
        include_broadcast=True,
        phase=phase,
    )
    if not resolve_youtube_cookiefile():
        if refresh_youtube_cookies_file(close_browsers=True):
            _emit_ytdlp_scan_progress(
                0,
                max(total, 1),
                "YouTube 쿠키 저장 완료",
                True,
                include_broadcast=True,
                phase=phase,
            )
        else:
            get_logger().warning(
                "youtube cookies not available — use manual cookies.txt (see panel settings)"
            )
            _emit_ytdlp_scan_progress(
                0,
                max(total, 1),
                "YouTube 쿠키 없음 — 설정에서 «쿠키 안내» 참고 (다운로드 실패 가능)",
                True,
                include_broadcast=True,
                phase=phase,
            )
    _abort_if_cancelled()
    scan_label = (
        f"방송 화면에서 임베드 재생 검사 준비… (YouTube {total}곡)"
        if total
        else "방송 화면에서 임베드 재생 검사 준비…"
    )
    _emit_ytdlp_scan_progress(
        0,
        max(total, 1),
        scan_label,
        True,
        include_broadcast=True,
        phase=phase,
    )

    if not _restart_broadcast_window(display_index, embed_scan=True, timeout=60.0):
        raise RuntimeError(
            "방송 창을 열 수 없습니다. Edge 또는 Chrome이 설치되어 있는지 확인해 주세요."
        )
    _abort_if_cancelled()

    # 메타데이터 대체 검사 없음 — 방송 화면 임베드 검사만 사용 (최대 약 90초 대기)
    ready_deadline = time.time() + 90.0
    while time.time() < ready_deadline:
        _abort_if_cancelled()
        if _embed_scan_broadcast_ready:
            break
        time.sleep(0.25)
    else:
        get_logger().warning(
            "embed scan broadcast_ready slow; will push embed_scan_start anyway"
        )

    _abort_if_cancelled()
    if not _embed_scan_client_ready.wait(timeout=30.0):
        get_logger().warning("embed scan client_ready timeout; pushing scan start")
    socketio.emit("embed_scan_start", scan_payload, namespace="/broadcast")
    timeout = max(180.0, total * 20.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        _abort_if_cancelled()
        if _embed_scan_done.wait(timeout=0.5):
            break
    else:
        get_logger().error("embed scan timed out after %.0fs", timeout)
        raise TimeoutError("임베드 재생 검사 시간 초과")

    _abort_if_cancelled()
    _apply_embed_scan_results(_embed_scan_results)
    with _embed_scan_lock:
        _embed_scan_pending_payload = None
    _abort_if_cancelled()
    if youtube_embed_only():
        _emit_ytdlp_scan_progress(
            0,
            1,
            "임베드 검사 완료 · YouTube 퍼가기(최고 화질)로 재생합니다",
            True,
            include_broadcast=True,
            phase="방송준비중",
        )
    else:
        _emit_ytdlp_scan_progress(
            0,
            1,
            "고화질 영상 다운로드 시작…",
            True,
            include_broadcast=True,
            phase="방송준비중",
        )
    _download_ytdlp_required_in_playlist(
        phase=phase,
        include_broadcast=True,
        prep_token=prep_token,
    )
    _abort_if_cancelled()
    _emit_ytdlp_scan_progress(
        1,
        1,
        "준비 완료 · 방송 시작",
        True,
        include_broadcast=True,
        phase="방송준비중",
    )
    socketio.emit("embed_scan_done", {}, namespace="/broadcast")


def auto_setup_admin() -> None:
    """앱 시작 시 admin/1234 계정이 없으면 자동 생성 (온보딩 스킵)."""
    cfg = load_config()
    if not is_setup_complete(cfg):
        pw_hash = bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=12))
        cfg["admin_username"] = "admin"
        cfg["password_hash"] = pw_hash.decode("utf-8")
        cfg["onboarding_complete"] = True
        save_config(cfg)


def init_app(cfg: dict[str, Any]) -> None:
    global config_data
    config_data = cfg
    ensure_dirs()
    # exe: 번들 경로가 준비된 뒤 static 폴더를 다시 지정
    bundle_static = bundle_dir() / "panel" / "static"
    if bundle_static.is_dir():
        app.static_folder = str(bundle_static)
    app.config["SECRET_KEY"] = cfg["secret_key"]
    app.config["WTF_CSRF_ENABLED"] = True
    # LAN IP(192.168.x.x)로 접속 시 세션·CSRF 쿠키 허용
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login_page"
    # 저장된 플레이리스트 복원
    saved = load_playlist()
    if saved:
        broadcast_state.set_playlist(saved)


def login_required_socket(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            disconnect()
            return False
        return fn(*args, **kwargs)

    return wrapper


# --- HTTP 라우트 ---


@app.route("/")
def index():
    cfg = load_config()
    if not is_setup_complete(cfg):
        return redirect(url_for("setup_page"))
    if not current_user.is_authenticated:
        return redirect(url_for("login_page"))
    return render_template_string(_read_panel_html())


@app.route("/setup")
def setup_page():
    cfg = load_config()
    if is_setup_complete(cfg):
        return redirect(url_for("login_page"))
    return render_template_string(_read_auth_html("setup"))


@app.route("/login")
def login_page():
    cfg = load_config()
    if not is_setup_complete(cfg):
        return redirect(url_for("setup_page"))
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template_string(_read_auth_html("login"))


@app.route("/broadcast-static/<path:filename>")
def broadcast_static(filename: str):
    """방송 페이지 전용 정적 파일 (panel/static 과 분리)."""
    from flask import send_from_directory

    return send_from_directory(bundle_dir() / "broadcast", filename)


@app.route("/broadcast/")
def broadcast_page():
    try:
        return render_template_string(_read_broadcast_html())
    except FileNotFoundError:
        get_logger().error("broadcast HTML missing bundle_dir=%s", BUNDLE_DIR)
        return "broadcast/index.html not found in app bundle", 500


@csrf.exempt
@app.route("/api/youtube/local/<video_id>")
def api_youtube_local_file(video_id: str):
    """방송 화면 <video>용 — 미리 받아 둔 yt-dlp 로컬 파일."""
    if not _is_local_request():
        return jsonify({"error": "forbidden"}), 403
    from flask import send_file

    vid = parse_youtube_video_id(video_id) or (video_id or "").strip()
    if not vid:
        return jsonify({"error": "invalid video id"}), 400
    if youtube_stream_only():
        return redirect(f"/api/youtube/stream/{vid}")
    entry = get_download_entry(vid)
    if not entry:
        try:
            entry = ensure_youtube_downloaded(vid)
        except Exception as exc:
            get_logger().error("youtube local file missing id=%s: %s", vid, exc)
            return jsonify({"error": str(exc)}), 404
    path = entry.get("path")
    if not path or not Path(path).is_file():
        return jsonify({"error": "not found"}), 404
    ext = Path(path).suffix.lower()
    mime = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".m4v": "video/x-m4v",
    }.get(ext, "video/mp4")
    return send_file(
        path,
        mimetype=mime,
        conditional=True,
        download_name=f"{vid}{ext or '.mp4'}",
    )


@csrf.exempt
@app.route("/api/youtube/stream/<video_id>")
def api_youtube_stream_proxy(video_id: str):
    """방송 화면 <video>용 YouTube 스트림 프록시 (Range 지원)."""
    if not _is_local_request():
        return jsonify({"error": "forbidden"}), 403
    vid = parse_youtube_video_id(video_id) or (video_id or "").strip()
    if not vid:
        return jsonify({"error": "invalid video id"}), 400
    meta: dict[str, Any] | None = None
    try:
        meta = _cache_youtube_progressive(vid)
        height = int(meta.get("height") or 0)
        if height >= YT_MIN_DOWNLOAD_HEIGHT:
            get_logger().info(
                "youtube progressive stream id=%s height=%sp",
                vid,
                height,
            )
        else:
            raise ValueError(f"progressive {height}p < {YT_MIN_DOWNLOAD_HEIGHT}p")
    except Exception as prog_exc:
        get_logger().warning(
            "youtube progressive failed id=%s: %s — trying fast mux",
            vid,
            prog_exc,
        )
        try:
            from youtube_dash_mux import iter_ffmpeg_mux_stream

            dash = _cache_youtube_dash(vid)
            get_logger().info(
                "youtube copy-mux stream id=%s height=%sp",
                vid,
                dash.get("height"),
            )

            def generate_mux():
                from youtube_dash_mux import iter_ffmpeg_mux_stream_browser

                try:
                    yield from iter_ffmpeg_mux_stream(
                        dash["video_url"],
                        dash["audio_url"],
                        dash.get("http_headers"),
                    )
                except Exception as mux_exc:
                    get_logger().warning(
                        "copy mux failed id=%s, transcode fallback: %s",
                        vid,
                        mux_exc,
                    )
                    yield from iter_ffmpeg_mux_stream_browser(
                        dash["video_url"],
                        dash["audio_url"],
                        dash.get("http_headers"),
                    )

            return Response(
                generate_mux(),
                mimetype="video/mp4",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as mux_meta_exc:
            get_logger().error(
                "youtube stream resolve failed id=%s: %s",
                vid,
                mux_meta_exc,
            )
            return jsonify({"error": str(mux_meta_exc)}), 404

    upstream_url = meta["url"]
    headers = dict(meta.get("http_headers") or {})
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    try:
        upstream = urllib.request.urlopen(
            urllib.request.Request(upstream_url, headers=headers),
            timeout=90,
        )
    except urllib.error.HTTPError as exc:
        return jsonify({"error": f"upstream {exc.code}"}), 502
    except urllib.error.URLError as exc:
        return jsonify({"error": str(exc.reason)}), 502

    status = upstream.status
    content_type = upstream.headers.get("Content-Type", "video/mp4")
    content_length = upstream.headers.get("Content-Length")
    content_range = upstream.headers.get("Content-Range")

    def generate():
        try:
            while True:
                chunk = upstream.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    response = Response(generate(), status=status, mimetype=content_type)
    if content_length:
        response.headers["Content-Length"] = content_length
    if content_range:
        response.headers["Content-Range"] = content_range
    response.headers["Accept-Ranges"] = "bytes"
    return response


@csrf.exempt
@app.route("/api/youtube/mux/<video_id>")
def api_youtube_mux_stream(video_id: str):
    """DASH video+audio — ffmpeg 실시간 mux → <video> (파일 저장 없음)."""
    if not _is_local_request():
        return jsonify({"error": "forbidden"}), 403
    from youtube_dash_mux import extract_dash_av_urls, iter_ffmpeg_mux_stream

    vid = parse_youtube_video_id(video_id) or (video_id or "").strip()
    if not vid:
        return jsonify({"error": "invalid video id"}), 400
    try:
        meta = extract_dash_av_urls(vid)
    except Exception as exc:
        get_logger().error("youtube mux meta failed id=%s: %s", vid, exc)
        return jsonify({"error": str(exc)}), 404

    def generate():
        try:
            yield from iter_ffmpeg_mux_stream(
                meta["video_url"],
                meta["audio_url"],
                meta.get("http_headers"),
            )
        except Exception as exc:
            get_logger().error("youtube mux stream id=%s: %s", vid, exc)

    get_logger().info(
        "youtube mux play id=%s height=%sp audio=%sk",
        vid,
        meta.get("height"),
        meta.get("audio_abr"),
    )
    return Response(
        generate(),
        mimetype="video/mp4",
        headers={
            "Cache-Control": "no-store",
            "Accept-Ranges": "none",
        },
    )


@app.route("/api/csrf-token")
def api_csrf():
    return jsonify({"csrf_token": generate_csrf()})


@csrf.exempt
@app.route("/api/panel/window/<action>", methods=["POST", "GET"])
def api_panel_window(action: str):
    """네이티브 패널 창 제어 (localhost 전용, WebView2 API 대신 사용)."""
    if not _is_local_request():
        return jsonify({"error": "forbidden"}), 403
    action = (action or "").strip().lower()
    allowed = {"minimize", "maximize", "restore", "hide", "show"}
    if action not in allowed:
        return jsonify({"error": "bad_action"}), 400
    try:
        enqueue_panel_window_command(action)
        get_logger().info("HTTP panel window cmd: %s", action)
    except Exception:
        get_logger().error("enqueue panel cmd failed: %s", action, exc_info=True)
        return jsonify({"error": "queue_failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/session/status")
def api_session_status():
    """앱(로컬) / 브라우저(원격) 상태 표시용."""
    local = _is_local_request()
    logged_in = current_user.is_authenticated
    panel_online = _has_panel_client()
    return jsonify(
        {
            "authenticated": logged_in,
            "viewer_is_local": local,
            "panel_online": panel_online,
            "broadcast_allowed": logged_in and panel_online,
            "status_label": (
                "서버 켜짐"
                if local
                else ("앱과 연결됨" if panel_online and logged_in else "앱 로그인 대기")
            ),
        }
    )


@app.route("/api/config/public")
def api_public_config():
    from youtube_util import resolve_youtube_cookiefile

    cfg = load_config()
    port = int(cfg.get("port", 8765))
    urls = network_access_urls(port, WEBSITE_PORT)
    return jsonify(
        {
            "port": port,
            "youtube_cookies_ok": resolve_youtube_cookiefile(cfg) is not None,
            "end_broadcast_image": cfg.get("end_broadcast_image", ""),
            "next_alert_logo": resolve_alert_logo_url(cfg.get("next_alert_logo")),
            "next_alert_text": normalize_next_alert_text(cfg.get("next_alert_text")),
            "next_alert_theme": normalize_alert_theme(cfg.get("next_alert_theme")),
            "now_playing_theme": normalize_alert_theme(cfg.get("now_playing_theme")),
            "autostart": cfg.get("autostart", False),
            "setup_complete": is_setup_complete(cfg),
            "panel_local": urls["panel_local"],
            "panel_lan": urls["panel_lan"],
            "panel_primary_lan": urls["panel_primary_lan"],
            "website_port": urls["website_port"],
            "website_local": urls["website_local"],
            "website_lan": urls["website_lan"],
            "website_primary_lan": urls["website_primary_lan"],
            "broadcast_browser": cfg.get("broadcast_browser", "auto"),
            "onboarding_complete": bool(cfg.get("onboarding_complete")),
            "playback_error_stall_seconds": int(
                cfg.get("playback_error_stall_seconds", 10)
            ),
            "playback_error_recover_mode": cfg.get(
                "playback_error_recover_mode", "manual"
            ),
        }
    )


@app.route("/api/network")
def api_network():
    """다른 기기(스마트폰 등)에서 접속할 LAN 주소."""
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    cfg = load_config()
    port = int(cfg.get("port", 8765))
    return jsonify(network_access_urls(port, WEBSITE_PORT))


@app.route("/api/displays")
def api_displays():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    try:
        from screeninfo import get_monitors

        monitors = get_monitors()
        return jsonify(
            {
                "displays": [
                    {
                        "index": i,
                        "name": f"모니터 {i + 1} ({m.width}x{m.height})",
                        "x": m.x,
                        "y": m.y,
                        "width": m.width,
                        "height": m.height,
                    }
                    for i, m in enumerate(monitors)
                ]
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "displays": [{"index": 0, "name": "기본 모니터", "x": 0, "y": 0, "width": 1920, "height": 1080}]})


@app.route("/api/setup", methods=["POST"])
@csrf.exempt
def api_setup():
    cfg = load_config()
    if is_setup_complete(cfg):
        return jsonify(
            {"error": "이미 설정되었습니다.", "redirect": "/login", "already_setup": True}
        ), 400
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 2 or len(password) < 4:
        return jsonify({"error": "아이디(2자+)·비밀번호(4자+) 필요"}), 400

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
    cfg["admin_username"] = username
    cfg["password_hash"] = pw_hash.decode("utf-8")

    try:
        save_config(cfg)
        global config_data
        config_data = cfg
        app.config["SECRET_KEY"] = cfg["secret_key"]
        login_user(AdminUser(username), remember=True)
    except Exception as exc:
        rollback = load_config()
        rollback["admin_username"] = ""
        rollback["password_hash"] = ""
        save_config(rollback)
        config_data = rollback
        return jsonify({"error": f"설정 저장 실패: {exc}"}), 500

    resp = jsonify({"ok": True, "redirect": "/"})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/login", methods=["POST"])
@csrf.exempt
def api_login():
    locked, remain = _check_login_lock()
    if locked:
        return jsonify({"error": f"30초 후 다시 시도 ({remain}초)"}), 429
    cfg = load_config()
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").encode("utf-8")
    stored = cfg.get("password_hash", "").encode("utf-8")
    if username != cfg.get("admin_username") or not bcrypt.checkpw(password, stored):
        _record_login_failure()
        return jsonify({"error": "아이디 또는 비밀번호가 올바르지 않습니다"}), 401
    _clear_login_failures()
    login_user(AdminUser(username), remember=True)
    _emit_panel_session_status()
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/api/password", methods=["POST"])
def api_change_password():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    current_pw = (data.get("current_password") or "").encode("utf-8")
    new_pw = data.get("new_password") or ""
    cfg = load_config()
    if not bcrypt.checkpw(current_pw, cfg["password_hash"].encode("utf-8")):
        return jsonify({"error": "현재 비밀번호가 올바르지 않습니다"}), 400
    if len(new_pw) < 4:
        return jsonify({"error": "새 비밀번호는 4자 이상"}), 400
    cfg["password_hash"] = bcrypt.hashpw(
        new_pw.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/settings/reset-account", methods=["POST"])
def api_reset_account():
    """DB 계정 초기화 — 로컬 계정을 admin/1234로 리셋하고 재로그인."""
    cfg = load_config()
    pw_hash = bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=12))
    cfg["admin_username"] = "admin"
    cfg["password_hash"] = pw_hash.decode("utf-8")
    cfg["onboarding_complete"] = True
    save_config(cfg)
    global config_data
    config_data = cfg
    _disconnect_panel_clients()
    logout_user()
    _emit_panel_session_status()
    return jsonify({"ok": True, "redirect": "/login"})


@app.route("/api/youtube/from-url", methods=["POST"])
def api_youtube_from_url():
    """YouTube URL/ID → 메타데이터 (플레이리스트 추가용)."""
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    raw = (data.get("url") or data.get("id") or "").strip()
    if not raw:
        return jsonify({"error": "YouTube 링크를 입력해 주세요"}), 400
    video_id = parse_youtube_video_id(raw)
    if not video_id:
        return jsonify({"error": "YouTube 링크를 인식할 수 없습니다"}), 400
    try:
        meta = fetch_youtube_video_meta(video_id)
    except Exception as exc:
        get_logger().warning("youtube meta failed id=%s: %s", video_id, exc)
        return jsonify({"error": f"영상 정보를 가져올 수 없습니다: {exc}"}), 502
    return jsonify(meta)


@app.route("/api/search", methods=["POST"])
def api_search():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "검색어 필요"}), 400
    sid = request.sid if hasattr(request, "sid") else None

    def progress(pct: int, status: str) -> None:
        socketio.emit("search_progress", {"progress": pct, "status": status})

    try:
        results = search_youtube(query, max_results=10, progress_callback=progress)
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _local_upload_paths(original: str) -> tuple[Path, str]:
    """저장 파일명(ASCII 안전)과 플레이리스트 표시용 제목(한글 유지)."""
    raw = (original or "").replace("\\", "/").split("/")[-1].strip()
    if not raw:
        raise ValueError("파일명 없음")
    ext = Path(raw).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise ValueError(f"허용되지 않는 확장자: {ext}")
    display_title = Path(raw).name
    token = secrets.token_hex(6)
    stem = secure_filename(Path(raw).stem) or "file"
    disk_name = f"{stem}_{token}{ext}"
    dest = UPLOADS_DIR / disk_name
    counter = 1
    while dest.exists():
        disk_name = f"{stem}_{token}_{counter}{ext}"
        dest = UPLOADS_DIR / disk_name
        counter += 1
    return dest, display_title


@app.route("/api/upload/local", methods=["POST"])
def api_upload_local():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "파일 없음"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "파일명 없음"}), 400
    try:
        dest, display_title = _local_upload_paths(f.filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    f.save(dest)
    rel = f"uploads/{dest.name}"
    return jsonify(
        {
            "type": "local",
            "id": dest.name,
            "title": display_title,
            "path": rel,
        }
    )


@app.route("/api/settings/end-image", methods=["POST"])
def api_end_image():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "파일 없음"}), 400
    f = request.files["file"]
    ext = Path(f.filename or "").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return jsonify({"error": "이미지 파일만 가능"}), 400
    safe = secure_filename(f.filename) or "end_image.png"
    dest = ASSETS_DIR / safe
    f.save(dest)
    cfg = load_config()
    cfg["end_broadcast_image"] = f"assets/{dest.name}"
    save_config(cfg)
    _emit_broadcast_ui_config()
    return jsonify({"ok": True, "path": cfg["end_broadcast_image"]})


@app.route("/api/settings/end-image", methods=["DELETE"])
def api_end_image_clear():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    cfg = load_config()
    cfg["end_broadcast_image"] = ""
    save_config(cfg)
    _emit_broadcast_ui_config()
    return jsonify({"ok": True})


@app.route("/api/settings/next-alert-branding", methods=["GET"])
def api_next_alert_branding_get():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    cfg = load_config()
    return jsonify(
        {
            "next_alert_logo": resolve_alert_logo_url(cfg.get("next_alert_logo")),
            "next_alert_logo_stored": normalize_next_alert_logo(cfg.get("next_alert_logo")),
            "next_alert_text": normalize_next_alert_text(cfg.get("next_alert_text")),
            "next_alert_theme": normalize_alert_theme(cfg.get("next_alert_theme")),
            "now_playing_theme": normalize_alert_theme(cfg.get("now_playing_theme")),
        }
    )


@app.route("/api/settings/next-alert-logo", methods=["POST"])
def api_next_alert_logo_upload():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "파일 없음"}), 400
    f = request.files["file"]
    ext = Path(f.filename or "").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
        return jsonify({"error": "이미지 파일만 가능"}), 400
    safe = secure_filename(f.filename) or "next_alert_logo.png"
    dest = ASSETS_DIR / safe
    f.save(dest)
    cfg = load_config()
    cfg["next_alert_logo"] = f"assets/{dest.name}"
    save_config(cfg)
    _emit_broadcast_ui_config()
    return jsonify({"ok": True, "path": resolve_alert_logo_url(cfg["next_alert_logo"])})


@app.route("/api/settings/next-alert-logo", methods=["DELETE"])
def api_next_alert_logo_clear():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    cfg = load_config()
    cfg["next_alert_logo"] = DEFAULT_NEXT_ALERT_LOGO
    save_config(cfg)
    _emit_broadcast_ui_config()
    return jsonify({"ok": True, "path": resolve_alert_logo_url(DEFAULT_NEXT_ALERT_LOGO)})


@app.route("/api/settings/alert-themes", methods=["POST"])
def api_alert_themes_save():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    cfg["next_alert_theme"] = normalize_alert_theme(data.get("next_alert_theme"))
    cfg["now_playing_theme"] = normalize_alert_theme(data.get("now_playing_theme"))
    save_config(cfg)
    _emit_broadcast_ui_config()
    return jsonify(
        {
            "ok": True,
            "next_alert_theme": cfg["next_alert_theme"],
            "now_playing_theme": cfg["now_playing_theme"],
        }
    )


@app.route("/api/settings/next-alert-text", methods=["POST"])
def api_next_alert_text_save():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    cfg["next_alert_text"] = normalize_next_alert_text(data.get("text"))
    save_config(cfg)
    _emit_broadcast_ui_config()
    return jsonify({"ok": True, "text": cfg["next_alert_text"]})


def _emit_broadcast_ui_config() -> None:
    cfg = load_config()
    payload = broadcast_ui_config(cfg)
    socketio.emit("config", payload, namespace="/broadcast")


@app.route("/api/settings/broadcast-browser", methods=["GET", "POST"])
def api_broadcast_browser():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    from broadcast_window import list_available_browsers

    if request.method == "GET":
        cfg = load_config()
        return jsonify(
            {
                "broadcast_browser": cfg.get("broadcast_browser", "auto"),
                "available": list_available_browsers(),
            }
        )
    data = request.get_json(silent=True) or {}
    choice = (data.get("broadcast_browser") or "auto").lower().strip()
    if choice not in ("auto", "edge", "chrome"):
        return jsonify({"error": "auto, edge, chrome 중 선택"}), 400
    cfg = load_config()
    cfg["broadcast_browser"] = choice
    save_config(cfg)
    return jsonify(
        {
            "ok": True,
            "broadcast_browser": choice,
            "available": list_available_browsers(),
        }
    )


@app.route("/api/settings/onboarding", methods=["GET", "POST"])
def api_onboarding():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    cfg = load_config()
    if request.method == "GET":
        return jsonify({"complete": bool(cfg.get("onboarding_complete"))})
    data = request.get_json(silent=True) or {}
    if not data.get("agree_terms"):
        return jsonify({"error": "약관에 동의해 주세요"}), 400
    cfg["onboarding_complete"] = True
    save_config(cfg)
    return jsonify({"ok": True, "complete": True})


@app.route("/api/settings/youtube-playback", methods=["GET", "POST"])
def api_youtube_playback_settings():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    from config_store import normalize_youtube_iframe_quality

    cfg = load_config()
    if request.method == "GET":
        return jsonify(
            {
                "youtube_embed_only": youtube_embed_only(cfg),
                "youtube_iframe_quality": normalize_youtube_iframe_quality(
                    cfg.get("youtube_iframe_quality")
                ),
            }
        )
    data = request.get_json(silent=True) or {}
    if "youtube_embed_only" in data:
        cfg["youtube_embed_only"] = bool(data.get("youtube_embed_only"))
    if "youtube_iframe_quality" in data:
        cfg["youtube_iframe_quality"] = normalize_youtube_iframe_quality(
            data.get("youtube_iframe_quality")
        )
    save_config(cfg)
    global config_data
    config_data = cfg
    if youtube_embed_only(cfg):
        _clear_ytdlp_required_for_playlist()
    _emit_broadcast_ui_config()
    return jsonify(
        {
            "ok": True,
            "youtube_embed_only": youtube_embed_only(cfg),
            "youtube_iframe_quality": normalize_youtube_iframe_quality(
                cfg.get("youtube_iframe_quality")
            ),
        }
    )


@app.route("/api/settings/playback-recovery", methods=["GET", "POST"])
def api_playback_recovery_settings():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    cfg = load_config()
    if request.method == "GET":
        return jsonify(
            {
                "playback_error_stall_seconds": int(
                    cfg.get("playback_error_stall_seconds", 10)
                ),
                "playback_error_recover_mode": cfg.get(
                    "playback_error_recover_mode", "manual"
                ),
            }
        )
    data = request.get_json(silent=True) or {}
    if "playback_error_stall_seconds" in data:
        try:
            cfg["playback_error_stall_seconds"] = max(
                5, min(120, int(data["playback_error_stall_seconds"]))
            )
        except (TypeError, ValueError):
            return jsonify({"error": "stall_seconds invalid"}), 400
    if "playback_error_recover_mode" in data:
        mode = str(data["playback_error_recover_mode"]).lower()
        cfg["playback_error_recover_mode"] = "auto" if mode == "auto" else "manual"
    save_config(cfg)
    global config_data
    config_data = cfg
    return jsonify(
        {
            "ok": True,
            "playback_error_stall_seconds": cfg.get("playback_error_stall_seconds", 10),
            "playback_error_recover_mode": cfg.get(
                "playback_error_recover_mode", "manual"
            ),
        }
    )


@app.route("/api/recovery/start", methods=["POST"])
def api_recovery_start():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    if playback_recovery.start_recovery(initiated_by="api"):
        return jsonify({"ok": True})
    return jsonify({"error": "recovery already running"}), 409


@app.route("/api/recovery/dismiss", methods=["POST"])
def api_recovery_dismiss():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    playback_recovery.dismiss_error()
    return jsonify({"ok": True})


@app.route("/api/settings/autostart", methods=["POST"])
def api_autostart():
    if not current_user.is_authenticated:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    cfg = load_config()
    cfg["autostart"] = enabled
    save_config(cfg)
    try:
        from startup import set_autostart

        set_autostart(enabled)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/uploads/<path:filename>")
def uploads_file(filename):
    from flask import send_from_directory

    safe = secure_filename(Path(filename).name)
    return send_from_directory(UPLOADS_DIR, safe)


@app.route("/assets/<path:filename>")
def assets_file(filename):
    from flask import abort, send_from_directory

    norm = filename.replace("\\", "/")
    if norm.startswith("bundled/"):
        name = secure_filename(Path(norm).name)
        base = bundled_assets_dir()
        if not (base / name).is_file():
            abort(404)
        return send_from_directory(base, name)
    if "/" in norm:
        abort(404)
    safe = secure_filename(Path(norm).name)
    return send_from_directory(ASSETS_DIR, safe)


# --- Cloudflare Sync (browser-direct approach) ---

@csrf.exempt
@app.route("/api/local/state")
def local_state():
    """브라우저 JS가 로컬 플레이리스트·설정을 Worker로 푸시하기 위해 읽는 엔드포인트."""
    cfg = load_config()
    playlist = broadcast_state.get_playlist_dicts()
    settings = {
        "end_broadcast_image": cfg.get("end_broadcast_image", ""),
        "next_alert_logo": normalize_next_alert_logo(cfg.get("next_alert_logo")),
        "next_alert_text": normalize_next_alert_text(cfg.get("next_alert_text")),
        "next_alert_theme": normalize_alert_theme(cfg.get("next_alert_theme")),
        "now_playing_theme": normalize_alert_theme(cfg.get("now_playing_theme")),
        "autostart": str(cfg.get("autostart", False)).lower(),
        "broadcast_browser": cfg.get("broadcast_browser", "auto"),
        "port": str(cfg.get("port", 8765)),
    }
    return jsonify({"playlist": playlist, "settings": settings})


@csrf.exempt
@app.route("/api/local/apply", methods=["POST"])
def local_apply():
    """브라우저 JS가 Worker에서 당겨온 플레이리스트·설정을 앱에 적용."""
    data = request.get_json(force=True, silent=True) or {}
    pl = data.get("playlist") or []
    settings = data.get("settings") or {}

    # Apply playlist
    broadcast_state.set_playlist(pl)
    save_playlist(pl)

    # Apply settings
    cfg = load_config()
    changed = False
    for key in (
        "end_broadcast_image",
        "next_alert_logo",
        "next_alert_text",
        "next_alert_theme",
        "now_playing_theme",
        "autostart",
        "broadcast_browser",
        "port",
    ):
        if key in settings:
            val = settings[key]
            if key == "autostart":
                val = val in ("true", True)
            elif key == "port":
                val = int(val) if str(val).isdigit() else cfg.get("port", 8765)
            elif key == "next_alert_text":
                val = normalize_next_alert_text(val)
            elif key == "next_alert_logo":
                val = normalize_next_alert_logo(val)
            elif key in ("next_alert_theme", "now_playing_theme"):
                val = normalize_alert_theme(val)
            cfg[key] = val
            changed = True
    if changed:
        save_config(cfg)

    # 패널 UI 업데이트 — playlist_update 이벤트로 플레이리스트 갱신
    _emit_playlist()
    _emit_now_playing()
    return jsonify({"ok": True, "songs": len(pl)})


@csrf.exempt
@app.route("/api/youtube/cookies/status")
def api_youtube_cookies_status():
    if not current_user.is_authenticated:
        return jsonify({"error": "login required"}), 401
    from youtube_util import youtube_cookie_setup_guide, youtube_cookies_status

    status = youtube_cookies_status()
    status["guide"] = youtube_cookie_setup_guide()
    return jsonify(status)


@csrf.exempt
@app.route("/api/youtube/cookies/import", methods=["POST"])
def api_youtube_cookies_import():
    if not current_user.is_authenticated:
        return jsonify({"error": "login required"}), 401
    from youtube_util import (
        cookiefile_has_youtube_entries,
        import_youtube_cookies_file,
        youtube_cookies_status,
    )

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "파일을 선택해 주세요."}), 400
    import tempfile

    suffix = Path(upload.filename).suffix or ".txt"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            upload.save(tmp.name)
            tmp_path = tmp.name
        src = Path(tmp_path)
        if not cookiefile_has_youtube_entries(src):
            return jsonify(
                {
                    "error": "YouTube 쿠키가 포함된 txt 파일이 아닙니다.\n"
                    "(Netscape 형식, youtube.com 항목 필요)"
                }
            ), 400
        if not import_youtube_cookies_file(tmp_path):
            return jsonify({"error": "쿠키 파일을 저장하지 못했습니다."}), 500
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
    status = youtube_cookies_status()
    return jsonify({"ok": bool(status.get("ok")), **status})


@csrf.exempt
@app.route("/api/youtube/cookies/refresh", methods=["POST"])
def api_youtube_cookies_refresh():
    if not current_user.is_authenticated:
        return jsonify({"error": "login required"}), 401
    from youtube_util import (
        import_youtube_cookies_file,
        refresh_youtube_cookies_file,
        youtube_cookies_status,
    )

    data = request.get_json(force=True, silent=True) or {}
    close_browsers = bool(data.get("close_browsers", True))
    import_path = str(data.get("path") or "").strip()
    if import_path:
        import_youtube_cookies_file(import_path)
    ok = refresh_youtube_cookies_file(close_browsers=close_browsers)
    status = youtube_cookies_status()
    return jsonify({"ok": ok and status.get("ok"), **status})


@csrf.exempt
@app.route("/api/cf/status")
def cf_status():
    cfg = load_config()
    return jsonify({
        "configured": True,   # Worker URL은 하드코딩 — 항상 True
        "worker_url": cloudflare_sync.WORKER_URL,
        "auto_pull_on_start": cfg.get("cf_auto_pull_on_start", True),
    })


@csrf.exempt
@app.route("/api/cf/config", methods=["GET", "POST"])
def cf_config():
    """자동 동기화 토글만 저장 (Worker URL/계정은 하드코딩)."""
    cfg = load_config()
    if request.method == "GET":
        return jsonify({
            "cf_auto_pull_on_start": cfg.get("cf_auto_pull_on_start", True),
        })
    body = request.get_json(force=True, silent=True) or {}
    if "cf_auto_pull_on_start" in body:
        cfg["cf_auto_pull_on_start"] = bool(body["cf_auto_pull_on_start"])
    save_config(cfg)
    return jsonify({"ok": True})


@csrf.exempt
@app.route("/api/cf/push", methods=["POST"])
def cf_push():
    """앱 동기화: push local playlist + settings → Cloudflare D1."""
    cfg = load_config()
    if not cloudflare_sync.is_configured(cfg):
        return jsonify({"error": "Cloudflare Worker URL이 설정되지 않았습니다"}), 400
    playlist = broadcast_state.get_playlist_dicts()
    settings = {
        "end_broadcast_image": cfg.get("end_broadcast_image", ""),
        "next_alert_logo": normalize_next_alert_logo(cfg.get("next_alert_logo")),
        "next_alert_text": normalize_next_alert_text(cfg.get("next_alert_text")),
        "next_alert_theme": normalize_alert_theme(cfg.get("next_alert_theme")),
        "now_playing_theme": normalize_alert_theme(cfg.get("now_playing_theme")),
        "autostart": str(cfg.get("autostart", False)).lower(),
        "broadcast_browser": cfg.get("broadcast_browser", "auto"),
        "port": str(cfg.get("port", 8765)),
    }
    ok = cloudflare_sync.push(cfg, playlist, settings)
    if ok:
        return jsonify({"ok": True, "songs": len(playlist)})
    return jsonify({"error": "동기화 실패 — Worker URL 및 인증 정보를 확인해주세요"}), 502


@csrf.exempt
@app.route("/api/cf/pull", methods=["POST"])
def cf_pull():
    """데이터베이스 동기화: pull playlist + settings from Cloudflare D1 → app."""
    cfg = load_config()
    if not cloudflare_sync.is_configured(cfg):
        return jsonify({"error": "Cloudflare Worker URL이 설정되지 않았습니다"}), 400
    pl, settings = cloudflare_sync.pull(cfg)
    if pl is None:
        return jsonify({"error": "DB에서 데이터를 가져오지 못했습니다"}), 502

    for i, raw in enumerate(pl):
        if (raw.get("type") or "youtube") == "youtube":
            vid = parse_youtube_video_id(
                str(raw.get("song_id") or raw.get("id") or "")
            )
            if not vid:
                get_logger().warning(
                    "cf pull: invalid youtube id at index=%s title=%r raw_id=%r",
                    i,
                    raw.get("title"),
                    raw.get("id"),
                )

    # Apply playlist
    broadcast_state.set_playlist(pl)
    save_playlist(pl)

    # Apply settings
    if settings:
        changed = False
        for key in (
            "end_broadcast_image",
            "next_alert_logo",
            "next_alert_text",
            "next_alert_theme",
            "now_playing_theme",
            "autostart",
            "broadcast_browser",
            "port",
        ):
            if key in settings:
                val = settings[key]
                if key == "autostart":
                    val = val == "true" or val is True
                if key == "port":
                    val = int(val) if str(val).isdigit() else cfg.get("port", 8765)
                if key == "next_alert_text":
                    val = normalize_next_alert_text(val)
                if key == "next_alert_logo":
                    val = normalize_next_alert_logo(val)
                if key in ("next_alert_theme", "now_playing_theme"):
                    val = normalize_alert_theme(val)
                cfg[key] = val
                changed = True
        if changed:
            save_config(cfg)
            _emit_broadcast_ui_config()

    # 패널 UI 업데이트
    _emit_playlist()
    _emit_now_playing()

    return jsonify({
        "ok": True,
        "songs": len(pl),
        "settings_applied": bool(settings),
    })


# --- SocketIO ---


@socketio.on("connect")
def on_panel_connect():
    """컨트롤 패널: 로그인 세션 필수 (localhost 포함)."""
    if not current_user.is_authenticated:
        return False
    _register_panel_client()
    emit(
        "session_status",
        {
            "authenticated": True,
            "panel_online": True,
            "broadcast_allowed": True,
            "viewer_is_local": _is_local_request(),
        },
    )


@socketio.on("disconnect")
def on_panel_disconnect():
    _unregister_panel_client()
    _emit_panel_session_status()


@socketio.on("connect", namespace="/broadcast")
def on_broadcast_connect():
    snap = broadcast_state.snapshot()
    get_logger().info(
        "broadcast client connected sid=%s index=%s status=%s scan=%s",
        request.sid,
        snap.get("current_index"),
        snap.get("playback_status"),
        _ytdlp_scan_running,
    )
    cfg = load_config()
    emit("config", broadcast_ui_config(cfg))
    if _ytdlp_scan_running:
        return
    emit("state_sync", snap)
    idx = int(snap.get("current_index", -1))
    status = snap.get("playback_status", "stopped")
    if idx >= 0 and status in ("playing", "paused"):
        playback_recovery.notify_track_sync(idx, status)
        emit("load_track", snap)
        emit("playback_status", {"status": status})
        _prefetch_playlist_streams(idx)
        _emit_ytdlp_playback_if_ready(idx)


def _require_auth_event(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(data=None):
        if not current_user.is_authenticated:
            emit("control_denied", {"message": "로그인이 필요합니다."})
            return False
        return fn(data if data is not None else {})

    return wrapper


def _deny_broadcast_control(message: str) -> bool:
    emit("control_denied", {"message": message})
    return False


@socketio.on("add_song")
@_require_auth_event
def on_add_song(data):
    try:
        duration = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    vid = data.get("id", "") or ""
    if data.get("type", "youtube") == "youtube" and duration <= 0 and vid:
        duration = _fetch_youtube_duration(vid)
    item = {
        "type": data.get("type", "youtube"),
        "id": vid,
        "title": data.get("title", "제목 없음"),
        "thumbnail": data.get("thumbnail", ""),
        "path": data.get("path", ""),
        "duration": max(0, duration),
    }
    idx = broadcast_state.add_song(item)
    _persist_playlist()
    _emit_playlist()
    cur = broadcast_state.current_index
    if cur >= 0 and idx > cur:
        _notify_broadcast(item["title"], "song_added_notify")
    return {"ok": True}


@socketio.on("reorder")
@_require_auth_event
def on_reorder(data):
    broadcast_state.reorder(int(data.get("from_idx", 0)), int(data.get("to_idx", 0)))
    _persist_playlist()
    _emit_playlist()
    _emit_now_playing()


@socketio.on("remove_song")
@_require_auth_event
def on_remove_song(data):
    broadcast_state.remove_at(int(data.get("index", -1)))
    _persist_playlist()
    _emit_playlist()
    _emit_now_playing()


@socketio.on("control")
@_require_auth_event
def on_control(data):
    action = data.get("action")
    display_index = int(data.get("display_index", 0))
    playback_actions = {"start", "play", "pause", "next", "prev", "stop"}

    if action == "seek":
        if not _broadcast_playback_allowed():
            return _deny_broadcast_control("앱에 로그인되어 있어야 방송을 제어할 수 있습니다.")
        try:
            seconds = max(0.0, float(data.get("seconds", 0)))
        except (TypeError, ValueError):
            seconds = 0.0
        socketio.emit(
            "playback_seek",
            {
                "seconds": seconds,
                "index": broadcast_state.current_index,
            },
            namespace="/broadcast",
        )
        return

    if action in playback_actions and not _broadcast_playback_allowed():
        if not _has_panel_client():
            return _deny_broadcast_control(
                "앱에 로그인되어 있지 않습니다. 방송 PC 앱에서 로그인해 주세요."
            )
        return _deny_broadcast_control("앱에 로그인되어 있어야 방송을 제어할 수 있습니다.")

    if action == "start":
        if not broadcast_state.get_playlist_dicts():
            return _deny_broadcast_control("플레이리스트에 곡을 추가해 주세요.")
        _hard_reset_for_broadcast_start()

        def start_after_prep() -> None:
            global _embed_scan_pending_payload
            prep_token = _begin_broadcast_prep()
            try:
                _prepare_broadcast_youtube(display_index, prep_token)
                if not _prep_alive(prep_token):
                    return
                with _ytdlp_scan_lock:
                    _ytdlp_scan_running = False
                    _embed_scan_pending_payload = None
                    _embed_scan_broadcast_ready = False
                item = broadcast_state.start_playback()
                if not item:
                    _emit_ytdlp_scan_progress(
                        0,
                        1,
                        "재생할 곡이 없습니다.",
                        False,
                        phase="",
                    )
                    return
                _emit_now_playing()
                _emit_playback_status()
                _emit_playlist()
                socketio.emit("broadcast_playback_start", {}, namespace="/broadcast")
                _advance_past_unplayable_tracks()
                _emit_broadcast_track()

                def _delayed_broadcast_resync() -> None:
                    for delay in (0.9, 2.2):
                        time.sleep(delay)
                        if broadcast_state.playback_status not in ("playing", "paused"):
                            return
                        resync_broadcast_clients(allow_during_scan=True)

                threading.Thread(
                    target=_delayed_broadcast_resync, daemon=True
                ).start()
                _emit_ytdlp_scan_progress(
                    1,
                    1,
                    "방송을 시작합니다.",
                    False,
                    include_broadcast=True,
                    phase="",
                )
            except BroadcastPrepAborted:
                get_logger().info("broadcast prep aborted (token=%s)", prep_token)
            except Exception as exc:
                get_logger().error("broadcast start prep failed: %s", exc, exc_info=True)
                _emit_ytdlp_scan_progress(
                    0,
                    1,
                    f"준비 실패 — {exc}",
                    False,
                    include_broadcast=True,
                    phase="방송 시작 전",
                )
            finally:
                with _ytdlp_scan_lock:
                    _ytdlp_scan_running = False
                    _embed_scan_pending_payload = None

        threading.Thread(target=start_after_prep, daemon=True).start()
        return
    elif action == "play":
        if broadcast_state.current_index < 0:
            broadcast_state.start_playback()
        else:
            broadcast_state.resume()
        _emit_playback_status()
        _emit_broadcast_track()
    elif action == "pause":
        broadcast_state.pause()
        _emit_playback_status()
    elif action == "next":
        item = broadcast_state.advance_next()
        _emit_now_playing()
        _emit_playback_status()
        if item:
            _emit_broadcast_track()
            _notify_now_playing(item.title)
        else:
            _finalize_broadcast_ended()
    elif action == "prev":
        item = broadcast_state.advance_previous()
        _emit_now_playing()
        _emit_playback_status()
        if item:
            _emit_broadcast_track()
            _notify_now_playing(item.title)
    elif action == "stop":
        _finalize_broadcast_ended()


@socketio.on("request_stop", namespace="/broadcast")
def on_request_stop(_data=None):
    """방송 화면 ESC — 종료 화면 표시 (창은 두 번째 ESC로 닫기)."""
    _finalize_broadcast_ended(close_window=False)


@socketio.on("request_close_broadcast", namespace="/broadcast")
def on_request_close_broadcast(_data=None):
    """방송 종료 화면에서 두 번째 ESC — 키오스크 창 닫기."""
    _close_broadcast_window()


def _emit_ytdlp_download_error(
    message: str,
    *,
    video_id: str,
    title: str,
    index: int,
) -> None:
    payload = {
        "message": message,
        "video_id": video_id,
        "title": title or "YouTube",
        "index": index,
    }
    socketio.emit("ytdlp_download_error", payload, namespace="/broadcast")
    socketio.emit("ytdlp_download_error", payload)


def _play_ytdlp_at_index(
    video_id: str,
    index: int,
    *,
    title: str = "",
    reason: str = "",
    mark_required: bool = True,
) -> None:
    """yt-dlp 곡 재생 — stream 모드는 DASH mux만 (다운로드 없음)."""
    video_id = (video_id or "").strip()
    if not video_id:
        return
    if index < 0 or index != broadcast_state.current_index:
        get_logger().info(
            "ytdlp playback ignored stale index=%s current=%s id=%s",
            index,
            broadcast_state.current_index,
            video_id,
        )
        return
    if broadcast_state.playback_status not in ("playing", "paused"):
        return
    if mark_required and reason:
        _mark_playlist_ytdlp_required(video_id, reason)

    item = broadcast_state.current_item()
    display_title = title or (item.title if item else "") or "YouTube"

    dur = float(item.duration or 0) if item else 0.0
    if dur <= 0:
        try:
            dur = _fetch_youtube_duration(video_id)
        except Exception:
            dur = 0.0

    if youtube_stream_only():
        _emit_mux_playback(
            video_id,
            title=display_title,
            duration=dur,
            index=index,
        )
        return

    if _is_track_unplayable(item):
        _skip_to_next_track(f"다운로드 실패 — {display_title}")
        return

    if is_download_ready(video_id):
        _emit_ytdlp_local_playback(
            video_id,
            title=display_title,
            duration=dur,
            index=index,
        )
        return

    try:
        ensure_youtube_downloaded(video_id)
    except Exception as exc:
        get_logger().warning(
            "ytdlp download failed at playback id=%s: %s", video_id, exc
        )

    if is_download_ready(video_id):
        _emit_ytdlp_local_playback(
            video_id,
            title=display_title,
            duration=dur,
            index=index,
        )
        return

    _mark_video_ytdlp_download_failed(video_id)
    _skip_to_next_track(f"다운로드 실패 — {display_title}")


def _runtime_ytdlp_playback(
    video_id: str,
    index: int,
    *,
    title: str,
    reason: str = "embed_blocked_runtime",
) -> None:
    """방송 중 임베드 실패 → yt-dlp 로컬 재생."""
    _play_ytdlp_at_index(
        video_id,
        index,
        title=title,
        reason=reason,
        mark_required=True,
    )


@socketio.on("embed_scan_client_ready", namespace="/broadcast")
def on_embed_scan_client_ready(_data=None):
    """방송 페이지 JS 로드 완료 — 임베드 검사 시작."""
    global _embed_scan_broadcast_ready
    with _embed_scan_lock:
        _embed_scan_broadcast_ready = True
        pending = _embed_scan_pending_payload
    _embed_scan_client_ready.set()
    get_logger().info("embed scan client ready sid=%s", request.sid)
    if pending and _ytdlp_scan_running:
        emit("embed_scan_start", pending)


@socketio.on("embed_scan_progress", namespace="/broadcast")
def on_embed_scan_progress(data=None):
    payload = data if isinstance(data, dict) else {}
    with _embed_scan_lock:
        pending = _embed_scan_pending_payload is not None
    if not _ytdlp_scan_running and not pending:
        return
    done = int(payload.get("done", 0))
    total = int(payload.get("total", 1))
    pct_raw = payload.get("percent")
    pct = int(pct_raw) if pct_raw is not None else None
    _emit_ytdlp_scan_progress(
        done,
        max(total, 1),
        str(payload.get("status") or ""),
        True,
        include_broadcast=True,
        phase="방송 시작 전",
        percent=pct,
    )


@socketio.on("embed_scan_complete", namespace="/broadcast")
def on_embed_scan_complete(data=None):
    global _embed_scan_results, _embed_scan_pending_payload
    with _embed_scan_lock:
        pending = _embed_scan_pending_payload is not None
    if not _ytdlp_scan_running and not pending:
        return
    payload = data if isinstance(data, dict) else {}
    _embed_scan_results = list(payload.get("results") or [])
    with _embed_scan_lock:
        _embed_scan_pending_payload = None
    _embed_scan_done.set()


@socketio.on("youtube_embed_blocked", namespace="/broadcast")
def on_youtube_embed_blocked(data=None):
    """퍼가기 불가 — yt-dlp 로컬 재생 (다음 곡 건너뛰지 않음)."""
    payload = data if isinstance(data, dict) else {}
    video_id = (payload.get("id") or "").strip()
    if not video_id:
        return
    try:
        finished_index = int(payload.get("index", broadcast_state.current_index))
    except (TypeError, ValueError):
        finished_index = broadcast_state.current_index

    item = broadcast_state.current_item()
    title = (payload.get("title") or (item.title if item else "")) or "YouTube"
    _runtime_ytdlp_playback(
        video_id,
        finished_index,
        title=title,
        reason="embed_blocked_runtime",
    )


@socketio.on("request_ytdlp_playback", namespace="/broadcast")
def on_request_ytdlp_playback(data=None):
    """플레이리스트에 표시된 yt-dlp 곡 — 로컬 파일 재생 요청."""
    payload = data if isinstance(data, dict) else {}
    video_id = (payload.get("id") or "").strip()
    if not video_id:
        return
    try:
        index = int(payload.get("index", broadcast_state.current_index))
    except (TypeError, ValueError):
        index = broadcast_state.current_index
    title = (payload.get("title") or "").strip() or "YouTube"
    _play_ytdlp_at_index(
        video_id,
        index,
        title=title,
        reason="",
        mark_required=False,
    )


@socketio.on("song_finished", namespace="/broadcast")
def on_song_finished(data=None):
    """방송 화면에서 곡 종료 시 — 다음 곡 또는 방송 종료 화면."""
    payload = data if isinstance(data, dict) else {}
    finished_idx = payload.get("index")
    if finished_idx is not None:
        try:
            finished_idx = int(finished_idx)
        except (TypeError, ValueError):
            finished_idx = None
    if finished_idx is not None and broadcast_state.current_index > finished_idx:
        _emit_broadcast_track()
        return

    item = broadcast_state.advance_next()
    _emit_now_playing()
    _emit_playback_status()
    _emit_playlist()
    if item:
        _emit_broadcast_track()
        _notify_now_playing(item.title)
    else:
        _finalize_broadcast_ended()


@socketio.on("request_sync", namespace="/broadcast")
def on_broadcast_request_sync(_data=None):
    """방송 화면 — 다음 곡 로드 실패 시 상태 재동기화."""
    if _ytdlp_scan_running:
        return
    resync_broadcast_clients(allow_during_scan=True)
    if broadcast_state.playback_status == "ended":
        socketio.emit("broadcast_ended", {}, namespace="/broadcast")


@socketio.on("playback_heartbeat", namespace="/broadcast")
def on_playback_heartbeat(data=None):
    playback_recovery.on_heartbeat(data if isinstance(data, dict) else None)


@socketio.on("playback_progress", namespace="/broadcast")
def on_playback_progress_from_broadcast(data):
    """방송 키오스크 창(비로그인) → 패널 진행률 바."""
    playback_recovery.on_progress(data if isinstance(data, dict) else None)
    socketio.emit("playback_progress", data)


@socketio.on("playback_progress")
def on_playback_progress(data):
    playback_recovery.on_progress(data if isinstance(data, dict) else None)
    socketio.emit("playback_progress", data)


@socketio.on("playback_error_report", namespace="/broadcast")
def on_playback_error_report(data=None):
    payload = data if isinstance(data, dict) else {}
    code = str(payload.get("code") or "playback")
    message = str(payload.get("message") or "방송 재생 오류")
    playback_recovery.report_error(
        code,
        message,
        source="broadcast",
        detail=str(payload.get("detail") or ""),
    )


@socketio.on("playback_error_report")
@login_required_socket
def on_panel_playback_error_report(data=None):
    payload = data if isinstance(data, dict) else {}
    playback_recovery.report_error(
        str(payload.get("code") or "panel"),
        str(payload.get("message") or "재생 오류"),
        source="panel",
        detail=str(payload.get("detail") or ""),
    )


@socketio.on("recovery_request")
@login_required_socket
def on_recovery_request(_data=None):
    playback_recovery.start_recovery(initiated_by="panel")


@socketio.on("recovery_dismiss")
@login_required_socket
def on_recovery_dismiss(_data=None):
    playback_recovery.dismiss_error()


@socketio.on("recovery_request", namespace="/broadcast")
def on_broadcast_recovery_request(_data=None):
    playback_recovery.start_recovery(initiated_by="broadcast")


@socketio.on("recovery_dismiss", namespace="/broadcast")
def on_broadcast_recovery_dismiss(_data=None):
    playback_recovery.dismiss_error()


@socketio.on("youtube_playing", namespace="/broadcast")
def on_youtube_playing(_data=None):
    """iframe 재생 성공 — 대기 중 yt-dlp 스트림 취소."""
    bump_stream_generation()


@socketio.on("get_state")
@_require_auth_event
def on_get_state(_data):
    emit("state_sync", broadcast_state.snapshot())
    _emit_playlist()
    _emit_now_playing()
    _emit_playback_status()


def _read_panel_html() -> str:
    return (BUNDLE_DIR / "panel" / "index.html").read_text(encoding="utf-8")


def _read_auth_html(mode: str) -> str:
    html = (BUNDLE_DIR / "panel" / "auth.html").read_text(encoding="utf-8")
    return html.replace("{{MODE}}", mode)


def _read_broadcast_html() -> str:
    """dev·exe 동일 — broadcast/index.html 그대로 (socket.io는 /broadcast-static/)."""
    path = bundle_dir() / "broadcast" / "index.html"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path.read_text(encoding="utf-8")


def create_socketio_app(cfg: dict[str, Any]) -> tuple[Flask, SocketIO]:
    init_app(cfg)
    set_prep_running_check(lambda: _ytdlp_scan_running)
    playback_recovery.attach(
        socketio,
        load_config,
        broadcast_command_queue,
        broadcast_state.snapshot,
        lambda: broadcast_state.playback_status,
    )
    return app, socketio


def run_server(port: int) -> None:
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )


def start_server_thread(port: int) -> threading.Thread:
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    return t
