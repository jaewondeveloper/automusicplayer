"""
3세대 음방시스템 진입점.
방송 PC: 네이티브 패널(iframe) + 트레이 | 단일 실행
"""
from __future__ import annotations

import multiprocessing
import os
import queue
import sys
import threading
import time

if getattr(sys, "frozen", False):
    multiprocessing.freeze_support()

import cloudflare_sync
from app_icon_util import apply_windows_app_identity, ensure_app_icon_ico
from panel_log import install_crash_logging, panel_log_path, setup_panel_logging
from webview2_runtime import configure_bundled_webview2
from app_meta import APP_NAME
from broadcast_window import close_broadcast_window
from config_store import BUNDLE_DIR, INSTALL_DIR, WEBSITE_PORT, load_config
from network_utils import network_access_urls
from panel_window import enqueue_panel_window_command, run_on_main_thread, run_panel_native, stop_panel_window
from playlist_store import load_playlist, save_playlist
from server import auto_setup_admin, broadcast_state, create_socketio_app, set_broadcast_queue, start_server_thread
from website_server import start_website_server_thread
from single_instance import ensure_single_instance
from tray_icon import start_tray_thread, stop_tray

_command_queue: queue.Queue = queue.Queue()
_shutting_down = False


def _resync_broadcast_after_open(port: int, embed_scan: bool = False) -> None:
    """방송 키오스크가 뜬 뒤 load_track 이벤트가 유실되지 않도록 재동기화."""
    if embed_scan:
        return
    import urllib.error
    import urllib.request

    from server import resync_broadcast_clients

    url = f"http://127.0.0.1:{port}/broadcast/?kiosk=1"
    log = setup_panel_logging()
    for _ in range(40):
        if _shutting_down:
            return
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status < 500:
                    break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.25)
    time.sleep(0.6)
    try:
        resync_broadcast_clients()
        log.info("broadcast resync sent after kiosk open")
    except Exception:
        log.error("broadcast resync failed", exc_info=True)


def _schedule_open_broadcast(
    display_index: int,
    port: int,
    *,
    embed_scan: bool = False,
    wait_open: bool = False,
) -> bool:
    """방송 창은 Win32 메인 스레드에서 연다. wait_open=True 이면 창 뜰 때까지 대기."""
    opened = threading.Event()
    open_ok: list[bool] = [False]

    def _open_on_main_thread() -> None:
        from broadcast_window import get_broadcast_pid, open_broadcast_window
        from win_desktop import focus_process_main_window, minimize_other_windows

        try:
            minimize_other_windows(set())
            ok = open_broadcast_window(display_index, port, embed_scan=embed_scan)
            open_ok[0] = bool(ok)
            if ok:
                threading.Thread(
                    target=_resync_broadcast_after_open,
                    args=(port, embed_scan),
                    daemon=True,
                ).start()
                setup_panel_logging().info(
                    "broadcast window opened display=%s port=%s embed_scan=%s",
                    display_index,
                    port,
                    embed_scan,
                )
                pid = get_broadcast_pid()
                if pid:
                    threading.Thread(
                        target=focus_process_main_window,
                        args=(pid,),
                        daemon=True,
                    ).start()
                else:
                    setup_panel_logging().warning(
                        "broadcast process pid missing after open"
                    )
            else:
                setup_panel_logging().error(
                    "open_broadcast failed display=%s embed_scan=%s",
                    display_index,
                    embed_scan,
                )
        except Exception:
            setup_panel_logging().error("open_broadcast failed", exc_info=True)
        finally:
            opened.set()

    enqueue_panel_window_command("minimize")
    run_on_main_thread(_open_on_main_thread, delay=0.05)
    if wait_open:
        opened.wait(timeout=25.0)
        return open_ok[0]
    return True


def _watch_external_youtube(finished_index: int, duration: float) -> None:
    from broadcast_window import (
        external_youtube_running,
        get_broadcast_pid,
        close_external_youtube,
    )
    from server import finish_external_youtube_playback, notify_youtube_stream_failed
    from win_desktop import focus_process_main_window

    if not external_youtube_running():
        notify_youtube_stream_failed(finished_index, "external browser failed")
        return

    wait_sec = max(45.0, float(duration or 0) + 20.0)
    if wait_sec > 7200:
        wait_sec = 7200.0
    deadline = time.time() + wait_sec
    while external_youtube_running() and time.time() < deadline:
        time.sleep(1.0)

    close_external_youtube()
    finish_external_youtube_playback(finished_index)
    broadcast_pid = get_broadcast_pid()
    if broadcast_pid:
        focus_process_main_window(broadcast_pid)


def _schedule_external_youtube(
    video_id: str,
    display_index: int,
    duration: float,
    finished_index: int,
) -> None:
    def _open_on_main_thread() -> None:
        from broadcast_window import (
            get_external_youtube_pid,
            open_external_youtube_video,
        )
        from server import notify_youtube_stream_failed
        from win_desktop import focus_process_main_window

        open_external_youtube_video(video_id, display_index)
        ext_pid = get_external_youtube_pid()
        if not ext_pid:
            setup_panel_logging().error(
                "external youtube open failed video_id=%s", video_id
            )
            notify_youtube_stream_failed(
                finished_index, "external browser failed"
            )
            return
        focus_process_main_window(ext_pid)
        threading.Thread(
            target=_watch_external_youtube,
            args=(finished_index, duration),
            daemon=True,
        ).start()

    run_on_main_thread(_open_on_main_thread)


def _process_commands(port: int) -> None:
    while not _shutting_down:
        try:
            cmd = _command_queue.get(timeout=0.15)
        except queue.Empty:
            continue
        action = cmd.get("action")
        try:
            if action == "open_broadcast":
                display_index = int(cmd.get("display_index", 0))
                embed_scan = bool(cmd.get("embed_scan"))
                wait_open = bool(cmd.get("wait_open"))
                _schedule_open_broadcast(
                    display_index,
                    port,
                    embed_scan=embed_scan,
                    wait_open=wait_open,
                )
            elif action == "close_broadcast":
                done = cmd.get("done")

                def _close_on_main() -> None:
                    close_broadcast_window()
                    if done is not None:
                        done.set()

                run_on_main_thread(_close_on_main)
            elif action == "restart_broadcast":
                display_index = int(cmd.get("display_index", 0))
                embed_scan = bool(cmd.get("embed_scan"))
                port = int(cmd.get("port", WEBSITE_PORT))
                done: threading.Event = cmd["done"]
                ok: list[bool] = cmd["ok"]
                log = setup_panel_logging()

                try:
                    from broadcast_window import (
                        get_broadcast_pid,
                        open_broadcast_window,
                    )
                    from win_desktop import focus_process_main_window, minimize_other_windows

                    from youtube_util import refresh_youtube_cookies_file

                    enqueue_panel_window_command("minimize")
                    close_broadcast_window()
                    time.sleep(0.85)
                    if not refresh_youtube_cookies_file(close_browsers=True):
                        log.warning(
                            "youtube cookies export failed — "
                            "close all Edge/Chrome windows, log in to YouTube, retry"
                        )
                    minimize_other_windows(set())
                    ok[0] = bool(
                        open_broadcast_window(
                            display_index, port, embed_scan=embed_scan
                        )
                    )
                    if ok[0]:
                        log.info(
                            "broadcast window restarted display=%s embed_scan=%s",
                            display_index,
                            embed_scan,
                        )
                        threading.Thread(
                            target=_resync_broadcast_after_open,
                            args=(port, embed_scan),
                            daemon=True,
                        ).start()
                        pid = get_broadcast_pid()
                        if pid:

                            def _focus_broadcast() -> None:
                                focus_process_main_window(pid)

                            run_on_main_thread(_focus_broadcast, delay=0.1)
                    else:
                        log.error(
                            "restart_broadcast open failed display=%s",
                            display_index,
                        )
                except Exception:
                    log.error("restart_broadcast failed", exc_info=True)
                    ok[0] = False
                finally:
                    done.set()
            elif action == "open_external_youtube":
                video_id = str(cmd.get("video_id") or "")
                display_index = int(cmd.get("display_index", 0))
                duration = float(cmd.get("duration") or 0)
                finished_index = int(cmd.get("finished_index", -1))
                _schedule_external_youtube(
                    video_id, display_index, duration, finished_index
                )
            elif action == "close_external_youtube":
                from broadcast_window import close_external_youtube

                close_external_youtube()
        except Exception:
            setup_panel_logging().error("command %s failed", action, exc_info=True)


def _command_worker(port: int) -> None:
    while not _shutting_down:
        try:
            _process_commands(port)
        except Exception:
            setup_panel_logging().error("command worker error", exc_info=True)
        time.sleep(0.05)


def _shutdown() -> None:
    global _shutting_down
    _shutting_down = True
    stop_panel_window()
    close_broadcast_window()
    stop_tray()
    os._exit(0)


def main() -> None:
    from config_store import ensure_dirs

    ensure_dirs()
    install_crash_logging()
    log = setup_panel_logging()
    log.info("main() start log=%s", panel_log_path())
    if getattr(sys, "frozen", False):
        from config_store import bundle_dir

        bd = bundle_dir()
        log.info("frozen bundle_dir=%s install_dir=%s", bd, INSTALL_DIR)
        for rel in (
            "panel/index.html",
            "broadcast/index.html",
            "website/index.html",
            "panel/static/js/socket.io.min.js",
            "broadcast/js/socket.io.min.js",
        ):
            p = bd / rel
            log.info("bundle %s exists=%s", rel, p.is_file())
    apply_windows_app_identity()
    ensure_app_icon_ico()
    configure_bundled_webview2()

    # Auto-create admin/1234 account — skip onboarding entirely
    auto_setup_admin()

    cfg = load_config()
    port = int(cfg.get("port", 8765))

    try:
        from youtube_util import import_youtube_cookies_file, resolve_youtube_cookiefile

        ck = resolve_youtube_cookiefile(cfg)
        if ck:
            setup_panel_logging().info("youtube cookies file=%s", ck)
        elif import_youtube_cookies_file():
            setup_panel_logging().info(
                "youtube cookies imported to %s",
                resolve_youtube_cookiefile(cfg),
            )
    except Exception as exc:
        setup_panel_logging().warning("youtube cookies init: %s", exc)

    set_broadcast_queue(_command_queue)
    create_socketio_app(cfg)

    start_server_thread(port)
    start_website_server_thread(WEBSITE_PORT)
    time.sleep(1.0)

    threading.Thread(target=_command_worker, args=(port,), daemon=True).start()

    # Auto-pull playlist & settings from Cloudflare D1 on startup
    if cfg.get("cf_auto_pull_on_start", True) and cloudflare_sync.is_configured(cfg):
        def _on_pull(pl, settings):
            broadcast_state.set_playlist(pl)
            save_playlist(pl)
            setup_panel_logging().info("CF auto-pull: %d songs loaded from D1", len(pl))
        cloudflare_sync.pull_background(cfg, _on_pull)

    urls = network_access_urls(port, WEBSITE_PORT)
    panel_addr = urls["panel_primary_lan"] or urls["panel_local"]
    website_addr = urls["website_primary_lan"] or urls["website_local"]
    if not getattr(sys, "frozen", False):
        print("=" * 50)
        print(f"{APP_NAME} 실행 중 (단일 실행)")
        print(f"  패널(WebView2): {panel_addr}")
        for lan in urls["panel_lan"]:
            print(f"  패널(LAN): {lan}")
        print(f"  관리자 웹: {website_addr}")
        for lan in urls["website_lan"]:
            print(f"  웹(LAN): {lan}")
        print("=" * 50)

    start_tray_thread(port, _shutdown, native_panel=True)

    # 패널 메시지 루프는 메인 스레드에서 블로킹 (종료 시 stop_panel_window → WM_QUIT)
    if not run_panel_native(port):
        sys.exit(1)


if __name__ == "__main__":
    ensure_single_instance()
    try:
        main()
    except KeyboardInterrupt:
        _shutdown()
