"""
방송 화면 창 열기/닫기 (Edge / Chrome 키오스크 전체화면).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from app_meta import APP_NAME
from config_store import load_config
from panel_log import get_logger

_browser_proc: subprocess.Popen | None = None
_external_yt_proc: subprocess.Popen | None = None
_broadcast_lock = threading.RLock()
_KIOSK_PROFILE_KIND = "kiosk-broadcast"
_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
    else 0
)

_CHROMIUM_AUTOPLAY_FLAGS = [
    "--autoplay-policy=no-user-gesture-required",
    (
        "--disable-features=PreloadMediaEngagementData,"
        "MediaEngagementBypassAutoplayPolicies,CalculateNativeWinOcclusion"
    ),
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
]

# Win 키·다른 창 포커스 시 키오스크가 멈추거나 UI가 굳는 현상 완화
_KIOSK_FOCUS_STABILITY_FLAGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

_CHROME_EXTRA = [
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--noerrdialogs",
    "--disable-restore-session-state",
]

_SESSION_RESTORE_FLAGS = [
    "--disable-session-crashed-bubble",
    "--disable-restore-session-state",
    "--hide-crash-restore-bubble",
]


def _get_monitors():
    try:
        from screeninfo import get_monitors

        return get_monitors()
    except Exception:
        class _M:
            x, y, width, height = 0, 0, 1920, 1080

        return [_M()]


def _profile_dir(browser_id: str, kind: str = "kiosk") -> str:
    base = Path(tempfile.gettempdir()) / f"eumbang-{browser_id}-{kind}"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=_SUBPROCESS_FLAGS,
            )
        except Exception:
            pass
        return
    try:
        os.kill(pid, 15)
    except Exception:
        pass


def _win_pids_with_cmdline_containing(needle: str) -> list[int]:
    if sys.platform != "win32" or not needle:
        return []
    script = (
        f"$needle = {needle!r};"
        "Get-CimInstance Win32_Process | Where-Object {"
        "  ($_.Name -eq 'msedge.exe' -or $_.Name -eq 'chrome.exe') -and"
        "  $_.CommandLine -and ($_.CommandLine.IndexOf($needle) -ge 0)"
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=_SUBPROCESS_FLAGS,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _broadcast_kiosk_profile_markers() -> tuple[str, ...]:
    return (
        f"eumbang-edge-{_KIOSK_PROFILE_KIND}",
        f"eumbang-chrome-{_KIOSK_PROFILE_KIND}",
        "eumbang-edge-kiosk-",
        "eumbang-chrome-kiosk-",
    )


def list_broadcast_kiosk_pids() -> list[int]:
    """메인 방송 키오스크(Edge/Chrome) PID 목록 — external-yt 제외."""
    if sys.platform != "win32":
        global _browser_proc
        if _browser_proc is not None and _browser_proc.poll() is None:
            return [int(_browser_proc.pid)]
        return []
    seen: set[int] = set()
    pids: list[int] = []
    for marker in _broadcast_kiosk_profile_markers():
        for pid in _win_pids_with_cmdline_containing(marker):
            if pid in seen:
                continue
            seen.add(pid)
            pids.append(pid)
    return pids


def kill_stale_broadcast_kiosks(*, keep_pid: int | None = None) -> int:
    """추적 밖에 남은 방송 키오스크 프로세스를 모두 종료."""
    killed = 0
    for pid in list_broadcast_kiosk_pids():
        if keep_pid and pid == keep_pid:
            continue
        _kill_process_tree(pid)
        killed += 1
    return killed


def _any_broadcast_kiosk_running() -> bool:
    if sys.platform != "win32":
        return is_broadcast_window_open()
    return bool(list_broadcast_kiosk_pids())


def wait_until_broadcast_closed(timeout: float = 6.0) -> bool:
    """방송 키오스크가 모두 닫힐 때까지 대기."""
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        if not _any_broadcast_kiosk_running():
            return True
        time.sleep(0.12)
    kill_stale_broadcast_kiosks()
    time.sleep(0.25)
    return not _any_broadcast_kiosk_running()


def _edge_paths() -> list[Path]:
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pfx86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    return [
        pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        pfx86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]


def _chrome_paths() -> list[str]:
    found: list[str] = []
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pfx86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    for p in (
        pf / "Google" / "Chrome" / "Application" / "chrome.exe",
        pfx86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ):
        if p.is_file():
            found.append(str(p))
    for name in ("chrome", "google-chrome"):
        w = shutil.which(name)
        if w and w not in found:
            found.append(w)
    return found


def find_edge_for_kiosk() -> str | None:
    """방송 키오스크용 — 설치된 Microsoft Edge만 (WebView2 런타임 exe 제외)."""
    for p in _edge_paths():
        if p.is_file():
            return str(p)
    return None


def find_edge() -> str | None:
    exe = find_edge_for_kiosk()
    if exe:
        return exe
    try:
        from webview2_runtime import find_bundled_browser_exe

        bundled = find_bundled_browser_exe()
        if bundled:
            return bundled
    except Exception:
        pass
    return None


def find_chrome() -> str | None:
    paths = _chrome_paths()
    return paths[0] if paths else None


def list_available_browsers() -> dict[str, bool]:
    return {"edge": find_edge() is not None, "chrome": find_chrome() is not None}


def resolve_browser_exe(preference: str | None = None) -> tuple[str, str] | None:
    """방송 키오스크용 — 설치된 Edge/Chrome만 (WebView2 런타임 exe 제외)."""
    pref = (preference or "auto").lower().strip()
    edge = find_edge_for_kiosk()
    chrome = find_chrome()
    if pref == "edge":
        if edge:
            return edge, "edge"
        if chrome:
            return chrome, "chrome"
        return None
    if pref == "chrome":
        if chrome:
            return chrome, "chrome"
        if edge:
            return edge, "edge"
        return None
    if edge:
        return edge, "edge"
    if chrome:
        return chrome, "chrome"
    return None


def _show_broadcast_error(msg: str) -> None:
    get_logger().error("broadcast: %s", msg.replace("\n", " | "))
    print(f"[{APP_NAME}] {msg}", file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
        except Exception:
            pass


def _is_bundled_edge(exe: str) -> bool:
    try:
        from webview2_runtime import find_bundled_browser_exe

        bundled = find_bundled_browser_exe()
        return bool(bundled and Path(exe).resolve() == Path(bundled).resolve())
    except Exception:
        return False


def _build_browser_args(
    exe: str, browser_id: str, url: str, m, *, profile_kind: str = "kiosk"
) -> list[str]:
    """
    Chrome: --app 과 --kiosk 를 같이 쓰면 전체화면이 깨지므로 URL을 직접 넘김.
    """
    common = [
        f"--user-data-dir={_profile_dir(browser_id, profile_kind)}",
        "--no-first-run",
        "--no-default-browser-check",
        *_SESSION_RESTORE_FLAGS,
        *_CHROMIUM_AUTOPLAY_FLAGS,
        *_KIOSK_FOCUS_STABILITY_FLAGS,
        f"--window-position={m.x},{m.y}",
        f"--window-size={m.width},{m.height}",
    ]

    if browser_id == "chrome":
        return [
            exe,
            *common,
            *_CHROME_EXTRA,
            "--kiosk",
            "--start-fullscreen",
            url,
        ]

    # Edge 또는 번들 msedgewebview2.exe
    edge_args = [
        exe,
        *common,
        "--kiosk",
        "--start-fullscreen",
        url,
    ]
    if not _is_bundled_edge(exe):
        edge_args.insert(-1, "--edge-kiosk-type=fullscreen")
    return edge_args


def _launch_kiosk_browser(
    url: str,
    display_index: int,
    *,
    profile_kind: str = "kiosk",
) -> subprocess.Popen | None:
    cfg = load_config()
    pref = cfg.get("broadcast_browser", "auto")
    resolved = resolve_browser_exe(pref)

    if not resolved:
        _show_broadcast_error(
            "방송 브라우저를 찾을 수 없습니다.\n\n"
            "exe 안에 WebView2가 포함되지 않았거나\n"
            "첫 실행 시 런타임 풀기에 실패했습니다.\n"
            "build.bat 으로 다시 빌드한 exe를 사용하세요."
        )
        return None

    exe, browser_id = resolved
    monitors = _get_monitors()
    idx = min(max(0, display_index), len(monitors) - 1)
    m = monitors[idx]
    args = _build_browser_args(exe, browser_id, url, m, profile_kind=profile_kind)

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        name = "Microsoft Edge" if browser_id == "edge" else "Google Chrome"
        get_logger().info(
            "kiosk started browser=%s pid=%s display=%s profile=%s url=%s",
            name,
            proc.pid,
            idx,
            profile_kind,
            url,
        )
        return proc
    except OSError as exc:
        _show_broadcast_error(f"방송 창 실행 실패:\n{exc}")
        return None


def open_broadcast_window(
    display_index: int,
    port: int,
    *,
    embed_scan: bool = False,
) -> bool:
    """방송 키오스크 열기. 기존 창은 모두 닫은 뒤 하나만 연다."""
    with _broadcast_lock:
        close_broadcast_window()
        if not wait_until_broadcast_closed(timeout=8.0):
            get_logger().warning(
                "broadcast kiosk still running before open — force killing stale processes"
            )
            kill_stale_broadcast_kiosks()
            wait_until_broadcast_closed(timeout=4.0)

        query = "kiosk=1"
        if embed_scan:
            query += "&embed_scan=1"
        url = f"http://127.0.0.1:{port}/broadcast/?{query}"
        proc = _launch_kiosk_browser(
            url, display_index, profile_kind=_KIOSK_PROFILE_KIND
        )
        if not proc:
            return False

        global _browser_proc
        _browser_proc = proc
        time.sleep(0.35)
        stale = list_broadcast_kiosk_pids()
        keep = int(proc.pid)
        if len(stale) > 1 or (len(stale) == 1 and stale[0] != keep):
            get_logger().warning(
                "multiple broadcast kiosks detected pids=%s keep=%s — closing extras",
                stale,
                keep,
            )
            for pid in stale:
                if pid != keep:
                    _kill_process_tree(pid)
            wait_until_broadcast_closed(timeout=3.0)

        print(f"[{APP_NAME}] 방송 창: 전체화면 키오스크 (pid={keep})")
        get_logger().info("broadcast kiosk single instance pid=%s", keep)
        return True


def get_broadcast_pid() -> int | None:
    global _browser_proc
    if _browser_proc is None:
        return None
    try:
        return int(_browser_proc.pid)
    except Exception:
        return None


def is_broadcast_window_open() -> bool:
    """방송 키오스크(Edge/Chrome)가 실행 중인지."""
    global _browser_proc
    tracked_alive = False
    if _browser_proc is not None:
        try:
            tracked_alive = _browser_proc.poll() is None
        except Exception:
            tracked_alive = False
    if sys.platform == "win32":
        return tracked_alive or _any_broadcast_kiosk_running()
    return tracked_alive


def open_external_youtube_video(video_id: str, display_index: int = 0) -> None:
    """임베드 불가 YouTube — watch 페이지를 별도 키오스크 창에서 재생."""
    global _external_yt_proc
    close_external_youtube()
    vid = (video_id or "").strip()
    if not vid:
        return
    url = f"https://www.youtube.com/watch?v={vid}"
    proc = _launch_kiosk_browser(url, display_index, profile_kind="external-yt")
    if proc:
        _external_yt_proc = proc
        get_logger().info("external youtube playback video_id=%s", vid)


def get_external_youtube_pid() -> int | None:
    global _external_yt_proc
    if _external_yt_proc is None:
        return None
    try:
        return int(_external_yt_proc.pid)
    except Exception:
        return None


def external_youtube_running() -> bool:
    global _external_yt_proc
    if _external_yt_proc is None:
        return False
    try:
        return _external_yt_proc.poll() is None
    except Exception:
        return False


def close_external_youtube() -> None:
    global _external_yt_proc
    if _external_yt_proc is not None:
        try:
            _external_yt_proc.terminate()
            _external_yt_proc.wait(timeout=3)
        except Exception:
            try:
                _external_yt_proc.kill()
            except Exception:
                pass
        get_logger().info("external youtube window closed")
        _external_yt_proc = None


def close_broadcast_window() -> None:
    with _broadcast_lock:
        global _browser_proc
        close_external_youtube()

        tracked_pid: int | None = None
        if _browser_proc is not None:
            try:
                tracked_pid = int(_browser_proc.pid)
            except Exception:
                tracked_pid = None
            try:
                if _browser_proc.poll() is None:
                    _kill_process_tree(tracked_pid or 0)
            except Exception:
                if tracked_pid:
                    _kill_process_tree(tracked_pid)
            _browser_proc = None

        killed = kill_stale_broadcast_kiosks(keep_pid=tracked_pid)
        wait_until_broadcast_closed(timeout=6.0)
        if killed:
            get_logger().info("closed stale broadcast kiosk processes count=%s", killed)
        get_logger().info("broadcast window closed")


