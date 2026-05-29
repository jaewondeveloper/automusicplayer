"""방송 재생 오류 감지·복구 (서버)."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from panel_log import get_logger

_log = get_logger()

_stream_generation = 0
_stream_gen_lock = threading.Lock()

# 트랙 시작 직후·버퍼링 구간 오탐 방지
_TRACK_GRACE_SEC = 22.0


def bump_stream_generation() -> int:
    global _stream_generation
    with _stream_gen_lock:
        _stream_generation += 1
        return _stream_generation


def current_stream_generation() -> int:
    with _stream_gen_lock:
        return _stream_generation


class PlaybackRecoveryManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_progress_at = 0.0
        self._last_progress_current = -1.0
        self._last_heartbeat_at = 0.0
        self._track_started_at = 0.0
        self._last_index = -1
        self._active_error: dict[str, Any] | None = None
        self._recovery_running = False
        self._auto_recovery_started = False
        self._socketio: Any = None
        self._get_cfg: Callable[[], dict[str, Any]] | None = None
        self._broadcast_command_queue: Any = None
        self._get_snapshot_fn: Callable[[], dict[str, Any]] | None = None
        self._get_status_fn: Callable[[], str] | None = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def attach(
        self,
        socketio: Any,
        get_cfg: Callable[[], dict[str, Any]],
        broadcast_command_queue: Any,
        get_snapshot_fn: Callable[[], dict[str, Any]],
        get_status_fn: Callable[[], str],
    ) -> None:
        self._socketio = socketio
        self._get_cfg = get_cfg
        self._broadcast_command_queue = broadcast_command_queue
        self._get_snapshot_fn = get_snapshot_fn
        self._get_status_fn = get_status_fn
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="playback-stall-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def _reset_track_watch(self, index: int) -> None:
        now = time.time()
        with self._lock:
            self._last_index = index
            self._track_started_at = now
            self._last_progress_at = now
            self._last_heartbeat_at = now
            self._last_progress_current = -1.0

    def notify_track_sync(self, index: int, status: str) -> None:
        if status in ("playing", "paused") and index >= 0:
            self._reset_track_watch(index)

    def on_progress(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        try:
            idx = int(data.get("index", -1))
        except (TypeError, ValueError):
            idx = -1
        try:
            cur = float(data.get("current", 0))
        except (TypeError, ValueError):
            cur = 0.0
        now = time.time()
        with self._lock:
            if idx >= 0 and idx != self._last_index:
                self._reset_track_watch(idx)
            if cur > self._last_progress_current + 0.08:
                self._last_progress_at = now
                self._last_progress_current = cur
                self._last_heartbeat_at = now

    def on_heartbeat(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        try:
            idx = int(data.get("index", -1))
        except (TypeError, ValueError):
            idx = -1
        now = time.time()
        with self._lock:
            if idx >= 0 and idx != self._last_index:
                self._reset_track_watch(idx)
            self._last_heartbeat_at = now
            if not self._last_progress_at:
                self._last_progress_at = now

    def _cfg_stall_seconds(self) -> int:
        if not self._get_cfg:
            return 10
        try:
            return max(10, int(self._get_cfg().get("playback_error_stall_seconds", 10)))
        except (TypeError, ValueError):
            return 10

    def _cfg_recover_mode(self) -> str:
        if not self._get_cfg:
            return "manual"
        mode = str(self._get_cfg().get("playback_error_recover_mode", "manual")).lower()
        return "auto" if mode == "auto" else "manual"

    def report_error(
        self,
        code: str,
        message: str,
        *,
        source: str = "broadcast",
        detail: str = "",
    ) -> None:
        if code in ("stream_failed",) and source == "broadcast":
            return
        with self._lock:
            if self._recovery_running:
                return
            if self._active_error and self._active_error.get("code") == code:
                self._active_error["message"] = message
                err = dict(self._active_error)
            else:
                err = {
                    "id": uuid.uuid4().hex[:12],
                    "code": code,
                    "message": message,
                    "detail": detail,
                    "source": source,
                    "at": time.time(),
                }
                self._active_error = err
                self._auto_recovery_started = False
        self._emit_playback_error(err)
        _log.warning("playback error code=%s source=%s msg=%s", code, source, message)

    def dismiss_error(self) -> None:
        with self._lock:
            self._active_error = None
            self._auto_recovery_started = False
        self._emit("playback_error_cleared", {})

    def _emit(self, event: str, data: dict[str, Any], namespace: str | None = None) -> None:
        if not self._socketio:
            return
        if namespace:
            self._socketio.emit(event, data, namespace=namespace)
        else:
            self._socketio.emit(event, data)
            self._socketio.emit(event, data, namespace="/broadcast")

    def _emit_playback_error(self, err: dict[str, Any]) -> None:
        payload = {
            **err,
            "stall_seconds": self._cfg_stall_seconds(),
            "recover_mode": self._cfg_recover_mode(),
        }
        self._emit("playback_error", payload)

    def _emit_recovery_progress(self, percent: int, step: str) -> None:
        self._emit(
            "recovery_progress",
            {"percent": max(0, min(100, percent)), "step": step},
        )

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(3.0):
            try:
                self._check_stall()
                self._maybe_auto_recover()
            except Exception:
                _log.error("playback stall monitor failed", exc_info=True)

    def _playing(self) -> bool:
        if not self._get_status_fn:
            return False
        return self._get_status_fn() == "playing"

    def _check_stall(self) -> None:
        if _prep_running_check():
            return
        if not self._playing():
            with self._lock:
                self._track_started_at = 0.0
            return
        stall = self._cfg_stall_seconds()
        now = time.time()
        with self._lock:
            if self._recovery_running or not self._track_started_at:
                return
            if now - self._track_started_at < _TRACK_GRACE_SEC:
                return
            last_ok = max(self._last_progress_at, self._last_heartbeat_at)
            if not last_ok:
                return
            if now - last_ok < stall:
                return
            if self._active_error and self._active_error.get("code") == "stall":
                return
        self.report_error(
            "stall",
            f"재생 진행이 {stall}초 이상 멈춘 것으로 감지되었습니다.",
            source="server",
        )

    def _maybe_auto_recover(self) -> None:
        if self._cfg_recover_mode() != "auto":
            return
        with self._lock:
            if not self._active_error or self._recovery_running or self._auto_recovery_started:
                return
            if time.time() - self._active_error["at"] < self._cfg_stall_seconds():
                return
            self._auto_recovery_started = True
        self.start_recovery(initiated_by="auto")

    def start_recovery(self, initiated_by: str = "user") -> bool:
        with self._lock:
            if self._recovery_running:
                return False
            self._recovery_running = True
        threading.Thread(
            target=self._run_recovery,
            args=(initiated_by,),
            daemon=True,
            name="playback-recovery",
        ).start()
        return True

    def _run_recovery(self, initiated_by: str) -> None:
        try:
            self._emit_recovery_progress(10, "재생 정리")
            bump_stream_generation()
            if self._broadcast_command_queue:
                try:
                    self._broadcast_command_queue.put({"action": "close_external_youtube"})
                except Exception:
                    pass
            snap = self._get_snapshot_fn() if self._get_snapshot_fn else {}
            time.sleep(0.2)
            self._emit_recovery_progress(55, "방송 화면 복구")
            if self._socketio:
                self._socketio.emit("playback_recover", snap, namespace="/broadcast")
            time.sleep(0.5)
            self._emit_recovery_progress(100, "복구 완료")
            idx = int(snap.get("current_index", -1))
            status = snap.get("playback_status", "stopped")
            if idx >= 0 and status in ("playing", "paused"):
                self.notify_track_sync(idx, status)
            with self._lock:
                self._active_error = None
                self._auto_recovery_started = False
            self._emit("playback_error_cleared", {})
            self._emit("recovery_finished", {"ok": True, "by": initiated_by})
            _log.info("playback recovery finished by=%s", initiated_by)
        except Exception as exc:
            _log.error("playback recovery failed: %s", exc, exc_info=True)
            self.report_error("recovery_failed", f"복구 실패: {exc}", source="server")
        finally:
            with self._lock:
                self._recovery_running = False

    def shutdown(self) -> None:
        self._monitor_stop.set()


_prep_running_check: Callable[[], bool] = lambda: False


def set_prep_running_check(fn: Callable[[], bool]) -> None:
    global _prep_running_check
    _prep_running_check = fn


playback_recovery = PlaybackRecoveryManager()
