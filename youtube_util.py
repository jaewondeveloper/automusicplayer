"""YouTube URL 파싱·메타데이터 (yt-dlp)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yt_dlp

from app_meta import EXE_NAME
from panel_log import get_logger

_log = get_logger()

_YT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_YT_URL_PATTERNS = (
    re.compile(r"(?:youtube\.com/watch\?(?:[^&]+&)*v=|youtube\.com/watch\?v=)([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([a-zA-Z0-9_-]{11})"),
)

_YT_EXTRACTOR_ARGS = {
    "youtube": {
        "player_client": ["android", "web", "ios"],
    }
}

# 방송용 yt-dlp — 1080p DASH 영상 + bestaudio → mp4 병합
YT_MIN_DOWNLOAD_HEIGHT = 1080
YT_MAX_DOWNLOAD_HEIGHT = 1080
YT_TARGET_AUDIO_ABR = 128
YT_DOWNLOAD_FORMAT = (
    f"bestvideo[height<={YT_MAX_DOWNLOAD_HEIGHT}]+bestaudio/"
    f"best[height<={YT_MAX_DOWNLOAD_HEIGHT}]/best"
)
YT_DOWNLOAD_FORMAT_PROGRESSIVE = (
    f"best[height<={YT_MAX_DOWNLOAD_HEIGHT}][ext=mp4][acodec!=none][vcodec!=none]/"
    f"best[height<={YT_MAX_DOWNLOAD_HEIGHT}][acodec!=none][vcodec!=none]/"
    "best[ext=mp4][acodec!=none][vcodec!=none]/"
    "22/18/best[acodec!=none][vcodec!=none]"
)
YT_DOWNLOAD_FORMAT_CANDIDATES = (YT_DOWNLOAD_FORMAT, YT_DOWNLOAD_FORMAT_PROGRESSIVE)
_YTDL_AUTH_OPT_KEYS = ("cookiesfrombrowser", "cookiefile")
YT_DOWNLOAD_FORMAT_SORT = [
    "res",
    "br",
    "fps",
    "size",
    "codec:av01",
    "codec:vp9.2",
    "codec:vp9",
    "codec:h264",
]
# 1080p 포맷 목록 — tv_simply·web_safari 우선 (android/web 은 360p만 보이는 경우 많음)
YT_DOWNLOAD_PLAYER_CLIENT_ROTATIONS: tuple[tuple[str, ...], ...] = (
    ("tv_simply", "web_safari"),
    ("android", "web"),
    ("tv", "web"),
)
YT_STREAM_PLAYER_CLIENT_ROTATIONS: tuple[tuple[str, ...], ...] = (
    ("android", "web"),
    ("tv_simply", "web_safari"),
    ("web_safari", "mweb"),
    ("tv", "web"),
)

# 방송 iframe 과 동일 계열 클라이언트
_IFRAME_PROBE_CLIENTS = ("web", "tv_embedded", "web_embedded", "mweb")

_EMBED_HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# YT.IFramePlayer error 101/150/153 과 대응하는 내부 사유
_EMBED_ERROR_REASONS = frozenset(
    {
        "VIDEO_OWNER_DISABLED_EMBEDDING",
        "EMBEDDING_DISABLED",
        "EMBEDDING_UNAVAILABLE",
        "PLAYBACK_ON_OTHER_WEBSITE",
        "PLAYBACK_ON_OTHER_WEBSITE_DISABLED",
    }
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


def _resolve_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _js_runtimes_for_ydl() -> dict[str, dict]:
    """YouTube 1080p DASH·EJS용 JS 런타임 (deno 또는 node)."""
    found: dict[str, dict] = {}
    search_dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        search_dirs.append(Path(sys.executable).resolve().parent)
    for name in ("deno", "node"):
        path = shutil.which(name)
        if not path:
            for base in search_dirs:
                cand = base / f"{name}.exe"
                if cand.is_file():
                    path = str(cand)
                    break
        if path:
            found[name] = {}
    return found if found else {"deno": {}}


def _frozen_cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / EXE_NAME
    cache = base / "yt-dlp-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


class _QuietYtdlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


_QUIET_YDL_LOGGER = _QuietYtdlpLogger()


def _base_ydl_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "extractor_args": _YT_EXTRACTOR_ARGS,
        "logger": _QUIET_YDL_LOGGER,
    }
    if getattr(sys, "frozen", False):
        opts["cachedir"] = str(_frozen_cache_dir())
    return opts


def _ydl_opts_for_clients(clients: tuple[str, ...] | list[str]) -> dict[str, Any]:
    opts = dict(_base_ydl_opts())
    opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    return opts


def _extract_info_with_clients(video_id: str, clients: tuple[str, ...] | list[str]) -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(_ydl_opts_for_clients(clients)) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _http_get_text(url: str, timeout: int = 18) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _EMBED_HTTP_UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(512 * 1024)
    return raw.decode("utf-8", errors="ignore")


def _playability_from_info(info: dict[str, Any]) -> dict[str, Any]:
    ps = info.get("playability_status")
    return ps if isinstance(ps, dict) else {}


def _embed_client_blocked(info: dict[str, Any]) -> tuple[bool, str]:
    """웹 클라이언트 — 명확한 퍼가기 금지만 (오탐 방지)."""
    if info.get("playable_in_embed") is False:
        return True, "embed_blocked"

    ps = _playability_from_info(info)
    status = str(ps.get("status") or "").strip().upper()
    reason = str(ps.get("reason") or "").strip().upper()
    if status == "ERROR" and reason in _EMBED_ERROR_REASONS:
        return True, reason.lower()
    return False, ""


def _embed_signals_in_html(html: str) -> tuple[bool, str]:
    if re.search(r"playableInEmbed[\"']?\s*:\s*false", html, re.I):
        return True, "embed_page_blocked"

    upper = html.upper()
    for code in _EMBED_ERROR_REASONS:
        if code in upper:
            return True, code.lower()

    lower = html.lower()
    if "playback on other websites has been disabled" in lower:
        return True, "embed_page_disabled"
    if "다른 웹사이트에서 재생할 수 없습니다" in lower:
        return True, "embed_page_disabled_ko"

    return False, ""


def _probe_embed_http_page(video_id: str) -> tuple[bool, str]:
    """embed·watch 페이지에서 iframe(퍼가기) 불가 신호 확인."""
    for url in (
        f"https://www.youtube.com/embed/{video_id}",
        f"https://www.youtube.com/watch?v={video_id}",
    ):
        try:
            html = _http_get_text(url)
        except Exception:
            continue
        blocked, reason = _embed_signals_in_html(html)
        if blocked:
            return True, reason
    return False, ""


def fetch_youtube_video_meta(video_id: str) -> dict[str, Any]:
    """제목·썸네일·길이(초) 조회."""
    if not video_id:
        raise ValueError("video_id 필요")
    ydl_opts = _base_ydl_opts()
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


def _audio_bitrate_kbps(fmt: dict[str, Any]) -> float:
    try:
        abr = float(fmt.get("abr") or 0)
    except (TypeError, ValueError):
        abr = 0.0
    if abr > 0:
        return abr
    try:
        return float(fmt.get("tbr") or 0)
    except (TypeError, ValueError):
        return 0.0


def _audio_format_score(
    fmt: dict[str, Any],
    *,
    target_abr: int = YT_TARGET_AUDIO_ABR,
) -> tuple[float, float]:
    """음성 전용 — target_abr(기본 128k)에 가까울수록 선호 (용량·속도 절약)."""
    abr = _audio_bitrate_kbps(fmt)
    if abr <= 0:
        return (9999.0, 0.0)
    return (abs(abr - float(target_abr)), -abr)


def _video_only_format_score(fmt: dict[str, Any]) -> tuple[int, int, int, int]:
    """영상 전용 포맷 — 해상도·fps·비트레이트."""
    try:
        height = int(fmt.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    try:
        tbr = int(fmt.get("tbr") or 0)
    except (TypeError, ValueError):
        tbr = 0
    try:
        fps = int(fmt.get("fps") or 0)
    except (TypeError, ValueError):
        fps = 0
    return (height, fps, tbr, height * fps)


def _format_score(fmt: dict[str, Any]) -> tuple[int, int, int, int]:
    """높을수록 선호: 영상+음성 > 영상만, 해상도·fps·비트레이트."""
    has_video = fmt.get("vcodec") not in (None, "none")
    has_audio = fmt.get("acodec") not in (None, "none")
    try:
        height = int(fmt.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    try:
        tbr = int(fmt.get("tbr") or 0)
    except (TypeError, ValueError):
        tbr = 0
    try:
        fps = int(fmt.get("fps") or 0)
    except (TypeError, ValueError):
        fps = 0
    if has_video and has_audio:
        kind = 2
    elif has_video:
        kind = 1
    elif has_audio:
        kind = 0
    else:
        kind = -1
    return (kind, height, fps, tbr)


def _height_of_format(fmt: dict[str, Any]) -> int:
    try:
        return int(fmt.get("height") or 0)
    except (TypeError, ValueError):
        return 0


def _pick_stream_url(info: dict[str, Any]) -> tuple[str, int]:
    url = info.get("url")
    if url:
        return str(url), _height_of_format(info)

    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    combined = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    if combined:
        combined.sort(key=_format_score)
        picked = combined[-1]
        return str(picked["url"]), _height_of_format(picked)

    requested = info.get("requested_formats") or []
    if requested:
        parts = [f for f in requested if f.get("url")]
        if len(parts) == 1:
            picked = parts[0]
            return str(picked["url"]), _height_of_format(picked)
        video_parts = [
            f for f in parts if f.get("vcodec") not in (None, "none")
        ]
        if video_parts:
            video_parts.sort(key=_format_score)
            picked = video_parts[-1]
            return str(picked["url"]), _height_of_format(picked)

    if formats:
        formats.sort(key=_format_score)
        picked = formats[-1]
        return str(picked["url"]), _height_of_format(picked)

    raise ValueError("재생 가능한 스트림 URL을 찾을 수 없습니다")


def _extract_stream_info(video_id: str, format_selector: str) -> dict[str, Any]:
    ydl_opts = {
        **_base_ydl_opts(),
        "format": format_selector,
        "js_runtimes": _js_runtimes_for_ydl(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
    info = info or {}
    url, picked_height = _pick_stream_url(info)
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    http_headers: dict[str, str] = {}
    raw_headers = info.get("http_headers")
    if isinstance(raw_headers, dict):
        http_headers = {str(k): str(v) for k, v in raw_headers.items()}
    return {
        "url": url,
        "height": picked_height,
        "duration": max(0.0, duration),
        "http_headers": http_headers,
    }


def info_max_height(info: dict[str, Any]) -> int:
    """다운로드 결과·requested_formats 에서 최대 영상 높이."""
    try:
        height = int(info.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    for part in info.get("requested_formats") or []:
        if part.get("vcodec") in (None, "none"):
            continue
        try:
            height = max(height, int(part.get("height") or 0))
        except (TypeError, ValueError):
            pass
    if height <= 0:
        for part in info.get("formats") or []:
            if part.get("vcodec") in (None, "none"):
                continue
            try:
                height = max(height, int(part.get("height") or 0))
            except (TypeError, ValueError):
                pass
    return height


def list_video_heights_from_info(info: dict[str, Any]) -> list[int]:
    """포맷 목록에 있는 영상 높이(정렬)."""
    heights: set[int] = set()
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") in (None, "none"):
            continue
        try:
            h = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            heights.add(h)
    return sorted(heights)


def build_youtube_extractor_args(
    player_clients: tuple[str, ...] | list[str],
    *,
    include_missing_pot: bool = True,
) -> dict[str, list[str]]:
    args: dict[str, list[str]] = {
        "player_client": list(player_clients),
    }
    if include_missing_pot:
        args["formats"] = ["missing_pot"]
    return args


def clear_ytdlp_auth_opts(opts: dict[str, Any]) -> None:
    for key in _YTDL_AUTH_OPT_KEYS:
        opts.pop(key, None)


def is_ytdlp_cookie_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "cookie" in msg and (
        "could not copy" in msg or "cookie database" in msg
    )


def is_ytdlp_bot_or_login_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "sign in to confirm" in msg
        or "not a bot" in msg
        or "login required" in msg
        or "please sign in" in msg
        or ("cookies" in msg and "pass" in msg)
    )


_YTDLP_COOKIE_EXPORT_BROWSERS: tuple[str, ...] = (
    "edge",
    "chrome",
    "brave",
    "chromium",
)
_YOUTUBE_COOKIE_PROBE_URL = "https://www.youtube.com/robots.txt"
_COOKIE_EXPORT_KILL_EXE = ("msedge.exe", "chrome.exe")


def default_youtube_cookies_path() -> Path:
    from config_store import get_install_dir

    return get_install_dir() / "youtube_cookies.txt"


def _manual_youtube_cookie_sources() -> list[Path]:
    """확장 프로그램 등으로 사용자가 직접 둔 cookies.txt 후보."""
    sources: list[Path] = []
    for path in (
        Path.home() / "Downloads" / "youtube_cookies.txt",
        default_youtube_cookies_path(),
    ):
        if path not in sources:
            sources.append(path)
    return sources


def import_youtube_cookies_file(source: Path | str | None = None) -> bool:
    """수동 export 한 cookies.txt 를 앱 저장 위치로 복사."""
    dest = default_youtube_cookies_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source is not None:
        candidates = [Path(source).expanduser()]
    else:
        candidates = _manual_youtube_cookie_sources()
    for src in candidates:
        if not src.is_file() or src.resolve() == dest.resolve():
            continue
        if not cookiefile_has_youtube_entries(src):
            continue
        try:
            shutil.copy2(src, dest)
            _log.info("youtube cookies imported %s -> %s", src, dest)
            _persist_cookiefile_path(dest)
            return True
        except OSError as exc:
            _log.warning("youtube cookies import failed %s: %s", src, exc)
    return False


def cookiefile_has_youtube_entries(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "youtube.com" in text or ".youtube.com" in text


def youtube_cookie_setup_guide() -> str:
    """패널·API용 — 확장 프로그램으로 cookies.txt 만드는 방법 (쉬운 안내)."""
    dest = default_youtube_cookies_path()
    dl = Path.home() / "Downloads" / "youtube_cookies.txt"
    return f"""【 준비 】 방송용 YouTube 계정으로 로그인할 Chrome 또는 Edge

【 1단계 】 확장 프로그램 설치 (한 번만)

  ■ Microsoft Edge
    1. Edge 주소창에 아래 주소 붙여넣기 후 Enter:
       https://microsoftedge.microsoft.com/addons/detail/get-cookiestxt-locally/opfcnfjmmnpaihjjfcgefdodjjfmafdm
    2. 또는 Edge 확장 스토어에서 검색: Get cookies.txt LOCALLY
    3. 「받기」 → 「확장 추가」

  ■ Google Chrome
    1. Chrome 웹 스토어:
       https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
    2. 「Chrome에 추가」 → 「확장 프로그램 추가」

【 2단계 】 YouTube 로그인

  1. 같은 브라우저(Edge 또는 Chrome)를 연다
  2. 주소창에 https://www.youtube.com 입력
  3. 방송에 쓸 Google 계정으로 로그인한다
  4. 홈 화면이 보이면 OK (채널 선택·동의 창이 있으면 처리)

【 3단계 】 쿠키 파일로 저장 (Export)

  1. YouTube 탭이 연 상태에서, 주소창 오른쪽 「확장」 아이콘(퍼즐 모양) 클릭
  2. 「Get cookies.txt LOCALLY」 핀 고정(선택) 후 클릭
  3. 팝업에서 아래 중 하나:
     · 「Export」 또는 「Export As」 클릭
     · 파일 이름: youtube_cookies.txt
     · 저장 위치 (아래 둘 중 하나):
       (권장) {dest}
       (편함) {dl}
  4. 저장 완료 메시지 확인

【 4단계 】 이 앱에 반영

  1. 3세대 음방시스템 앱 → 설정 탭
  2. 「YouTube 쿠키 (다운로드용)」에서
     「YouTube 쿠키 파일 가져오기」 버튼 클릭
  3. 상태가 「저장됨」이면 성공
  4. 방송 시작

【 주의 】
  · 쿠키 파일을 다른 사람에게 보내지 마세요 (비밀번호와 같습니다)
  · 로그인이 풀리면 2~3단계를 다시 하세요
  · 「쿠키 파일 가져오기」는 다운로드 폴더의 youtube_cookies.txt 도 자동으로 찾습니다

【 안 될 때 】
  · 파일 첫 줄이 # Netscape HTTP Cookie File 인지 확인
  · youtube.com 이 포함된 큰 txt 파일인지 확인
  · 저장 후 앱을 완전히 끄고 다시 실행
"""


def youtube_cookie_help_message() -> str:
    return youtube_cookie_setup_guide()


def list_browsers_blocking_cookie_export() -> list[str]:
    if sys.platform != "win32":
        return []
    running: list[str] = []
    for exe in _COOKIE_EXPORT_KILL_EXE:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if exe.lower() in (r.stdout or "").lower():
                running.append(exe)
        except Exception:
            pass
    return running


def close_browsers_for_cookie_export() -> list[str]:
    """쿠키 DB 잠금용 Edge/Chrome 프로세스만 종료 (WebView2·패널은 유지)."""
    if sys.platform != "win32":
        return []
    closed: list[str] = []
    for exe in _COOKIE_EXPORT_KILL_EXE:
        try:
            r = subprocess.run(
                ["taskkill", "/IM", exe, "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                closed.append(exe)
        except Exception as exc:
            _log.debug("taskkill %s: %s", exe, exc)
    if closed:
        time.sleep(1.0)
        _log.info("closed browsers for cookie export: %s", ", ".join(closed))
    return closed


def _save_cookie_jar_to_file(jar: Any, dest: Path) -> bool:
    from yt_dlp.cookies import YoutubeDLCookieJar

    dest.parent.mkdir(parents=True, exist_ok=True)
    out = YoutubeDLCookieJar(str(dest))
    for cookie in jar:
        out.set_cookie(cookie)
    out.save(ignore_discard=True, ignore_expires=True)
    if not dest.is_file() or dest.stat().st_size <= 64:
        return False
    if not cookiefile_has_youtube_entries(dest):
        try:
            dest.unlink()
        except OSError:
            pass
        return False
    return True


def _persist_cookiefile_path(dest: Path) -> None:
    try:
        from config_store import load_config, save_config

        cfg = load_config()
        if not str(cfg.get("youtube_cookies_file") or "").strip():
            cfg["youtube_cookies_file"] = str(dest)
            save_config(cfg)
    except Exception:
        pass


def resolve_youtube_cookiefile(cfg: dict[str, Any] | None = None) -> Path | None:
    """사용할 cookies.txt 경로 (설정 → 환경변수 → 기본 저장 위치)."""
    if cfg is None:
        from config_store import load_config

        cfg = load_config()
    candidates: list[Path] = []
    for raw in (
        str(cfg.get("youtube_cookies_file") or "").strip(),
        str(os.getenv("YTDLP_COOKIES", "") or "").strip(),
        str(default_youtube_cookies_path()),
        str(Path.home() / "Downloads" / "youtube_cookies.txt"),
    ):
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path not in candidates:
            candidates.append(path)
    for path in candidates:
        if (
            path.is_file()
            and path.stat().st_size > 64
            and cookiefile_has_youtube_entries(path)
        ):
            return path
    return None


def youtube_cookies_status() -> dict[str, Any]:
    path = resolve_youtube_cookiefile()
    dest = default_youtube_cookies_path()
    return {
        "ok": path is not None,
        "path": str(path) if path else str(dest),
        "size": path.stat().st_size if path else 0,
        "blocking_browsers": list_browsers_blocking_cookie_export(),
        "help": youtube_cookie_help_message(),
    }


def refresh_youtube_cookies_file(*, close_browsers: bool = False) -> bool:
    """
    Edge/Chrome 쿠키를 cookies.txt 로 저장.
    Windows 에서는 close_browsers=True 권장 (백그라운드 Edge/Chrome 잠금 해제).
    자동 추출이 실패하면 확장 프로그램으로보낸 cookies.txt 를 default 경로에 두세요.
    """
    dest = default_youtube_cookies_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if import_youtube_cookies_file():
        return True

    if close_browsers:
        close_browsers_for_cookie_export()

    try:
        from yt_dlp.cookies import YDLLogger, extract_cookies_from_browser

        logger = YDLLogger()
        for browser in _YTDLP_COOKIE_EXPORT_BROWSERS:
            try:
                jar = extract_cookies_from_browser(browser, None, logger)
                if _save_cookie_jar_to_file(jar, dest):
                    _log.info(
                        "youtube cookies exported from %s to %s (%s bytes)",
                        browser,
                        dest,
                        dest.stat().st_size,
                    )
                    _persist_cookiefile_path(dest)
                    return True
            except Exception as exc:
                _log.warning(
                    "youtube cookies export from %s failed: %s", browser, exc
                )
    except ImportError:
        pass

    for browser in _YTDLP_COOKIE_EXPORT_BROWSERS:
        try:
            opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "logger": _QUIET_YDL_LOGGER,
                "skip_download": True,
                "cookiesfrombrowser": (browser,),
                "cookiefile": str(dest),
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(_YOUTUBE_COOKIE_PROBE_URL, download=False)
            if dest.is_file() and cookiefile_has_youtube_entries(dest):
                _log.info(
                    "youtube cookies exported (ydl) from %s to %s",
                    browser,
                    dest,
                )
                _persist_cookiefile_path(dest)
                return True
        except Exception as exc:
            _log.warning("youtube cookies export from %s failed: %s", browser, exc)

    existing = resolve_youtube_cookiefile()
    if existing:
        _log.info("youtube cookies using existing file %s", existing)
        return True
    blocking = list_browsers_blocking_cookie_export()
    if blocking:
        _log.warning(
            "youtube cookies: still running %s — close them or use manual cookies.txt",
            ", ".join(blocking),
        )
    return False


def _ytdlp_auth_attempt_labels(opts: dict[str, Any]) -> str:
    if opts.get("cookiefile"):
        return f"file:{opts['cookiefile']}"
    browser = opts.get("cookiesfrombrowser")
    if browser:
        return f"browser:{browser[0] if isinstance(browser, tuple) else browser}"
    return "none"


def apply_ytdlp_auth_opts(
    opts: dict[str, Any],
    *,
    browser: str | None = None,
    file_only: bool = False,
) -> bool:
    """cookies.txt 우선. file_only=True 이면 실행 중 브라우저 DB 는 읽지 않음."""
    clear_ytdlp_auth_opts(opts)
    path = resolve_youtube_cookiefile()
    if path:
        opts["cookiefile"] = str(path)
        return True
    if file_only:
        return False
    try:
        from config_store import load_config
    except ImportError:
        return False
    cfg = load_config()
    browser_name = (browser or str(cfg.get("youtube_cookies_browser") or "")).strip().lower()
    if browser_name:
        opts["cookiesfrombrowser"] = (browser_name,)
        return True
    return False


def _ytdlp_auth_attempts(base_opts: dict[str, Any]) -> list[dict[str, Any]]:
    """다운로드는 cookies.txt 만 (방송 키오스크가 Edge 를 쓰는 동안 DB 잠금 방지)."""
    attempts: list[dict[str, Any]] = []
    with_file = dict(base_opts)
    if apply_ytdlp_auth_opts(with_file, file_only=True):
        attempts.append(with_file)
    if not attempts and sys.platform != "win32":
        bare = dict(base_opts)
        clear_ytdlp_auth_opts(bare)
        attempts.append(bare)
    return attempts


def ytdlp_extract_info(
    url: str,
    opts: dict[str, Any],
    *,
    download: bool = True,
) -> dict[str, Any]:
    """cookies.txt 로 다운로드 (키오스크 실행 중 브라우저 쿠키 DB 미사용)."""
    base = dict(opts)
    attempts = _ytdlp_auth_attempts(base)
    if not attempts:
        raise ValueError(
            "YouTube 쿠키가 없습니다.\n" + youtube_cookie_help_message()
        )
    last_error: Exception | None = None
    bot_blocked = False

    for attempt in attempts:
        label = _ytdlp_auth_attempt_labels(attempt)
        try:
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(url, download=download) or {}
            _log.info("ytdlp auth ok via %s", label)
            return info
        except Exception as exc:
            last_error = exc
            if is_ytdlp_bot_or_login_error(exc):
                bot_blocked = True
                _log.warning("ytdlp auth %s bot/login blocked: %s", label, exc)
                continue
            if is_ytdlp_cookie_error(exc):
                _log.warning("ytdlp auth %s cookie error: %s", label, exc)
                continue
            raise

    if last_error is not None:
        if bot_blocked:
            raise ValueError(
                "YouTube 로그인(봇 확인)이 필요합니다.\n"
                + youtube_cookie_help_message()
            ) from last_error
        raise last_error
    raise ValueError("다운로드에 실패했습니다")


def build_separate_av_format_selector(
    info: dict[str, Any],
    *,
    min_height: int = 0,
    max_height: int = YT_MAX_DOWNLOAD_HEIGHT,
    target_audio_abr: int = YT_TARGET_AUDIO_ABR,
) -> str | None:
    """
    영상(최대 1080p)·오디오(중간 음질) 각각 최적 선택 후 video_id+audio_id 병합.
    """
    formats = list(info.get("formats") or [])
    if not formats:
        return None

    videos = [
        f
        for f in formats
        if f.get("format_id")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") in (None, "none")
        and int(f.get("height") or 0) <= max_height
    ]
    audios = [
        f
        for f in formats
        if f.get("format_id")
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]

    if min_height > 0:
        hi = [v for v in videos if int(v.get("height") or 0) >= min_height]
        if hi:
            videos = hi
        elif videos:
            return build_separate_av_format_selector(
                info,
                min_height=0,
                max_height=max_height,
                target_audio_abr=target_audio_abr,
            )
        else:
            return None

    if not videos:
        return None

    videos.sort(key=_video_only_format_score)
    best_v = videos[-1]

    if not audios:
        return str(best_v["format_id"])

    # 최고 음질이 아니라 목표 kbps(기본 128)에 가까운 트랙 — 다운로드 빠름
    audios.sort(
        key=lambda f: _audio_format_score(f, target_abr=target_audio_abr)
    )
    best_a = audios[0]
    return f"{best_v['format_id']}+{best_a['format_id']}"


def describe_separate_av_selection(
    info: dict[str, Any],
    format_selector: str,
) -> str:
    """로그용 — 선택된 영상 높이·음성 kbps."""
    formats = {str(f.get("format_id")): f for f in (info.get("formats") or [])}
    parts = [p.strip() for p in str(format_selector).split("+") if p.strip()]
    bits: list[str] = []
    for fid in parts:
        fmt = formats.get(fid)
        if not fmt:
            continue
        if fmt.get("vcodec") not in (None, "none"):
            bits.append(f"v{fmt.get('height') or '?'}p/{fmt.get('vcodec')}")
        if fmt.get("acodec") not in (None, "none"):
            bits.append(f"a{fmt.get('abr') or fmt.get('tbr') or '?'}k/{fmt.get('acodec')}")
    return " + ".join(bits) if bits else format_selector


def build_format_selector_for_info(
    info: dict[str, Any],
    *,
    min_height: int = YT_MIN_DOWNLOAD_HEIGHT,
) -> str | None:
    """별칭 — 분리 다운로드 전용."""
    return build_separate_av_format_selector(info, min_height=min_height)


def pick_progressive_format_selectors(
    info: dict[str, Any],
    *,
    max_height: int = YT_MAX_DOWNLOAD_HEIGHT,
    min_height: int = YT_MIN_DOWNLOAD_HEIGHT,
) -> list[str]:
    """영상+음성 합쳐진 단일 포맷 — 화질(높이)만 최대, ffmpeg 병합 없음."""
    formats = list(info.get("formats") or [])
    combined = [
        f
        for f in formats
        if f.get("format_id")
        and f.get("url")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and int(f.get("height") or 0) <= max_height
    ]
    if not combined:
        return []
    hi = [f for f in combined if int(f.get("height") or 0) >= min_height]
    pool = hi if hi else combined
    pool.sort(key=_format_score)
    return [str(pool[-1]["format_id"])]


def pick_separate_av_format_selectors(info: dict[str, Any]) -> list[str]:
    """DASH 분리 — 영상 최고 + 음성 ~128k (합쳐진 포맷 없을 때만)."""
    seen: set[str] = set()
    out: list[str] = []
    for min_h in (YT_MIN_DOWNLOAD_HEIGHT, 720, 0):
        sel = build_separate_av_format_selector(
            info,
            min_height=min_h,
            max_height=YT_MAX_DOWNLOAD_HEIGHT,
            target_audio_abr=YT_TARGET_AUDIO_ABR,
        )
        if sel and sel not in seen:
            seen.add(sel)
            out.append(sel)
    return out


def pick_download_format_selectors(info: dict[str, Any]) -> list[str]:
    """합쳐진 mp4 우선 → 없으면 분리 다운로드."""
    prog = pick_progressive_format_selectors(info)
    if prog:
        return prog
    return pick_separate_av_format_selectors(info)


def describe_progressive_selection(info: dict[str, Any], format_id: str) -> str:
    formats = {str(f.get("format_id")): f for f in (info.get("formats") or [])}
    fmt = formats.get(str(format_id))
    if not fmt:
        return format_id
    return (
        f"progressive v{fmt.get('height') or '?'}p"
        f"/{fmt.get('ext') or '?'}"
        f" a{fmt.get('abr') or fmt.get('tbr') or '?'}k"
    )


def build_download_ydl_opts(
    format_selector: str,
    outtmpl: str,
    *,
    player_clients: tuple[str, ...] | list[str] | None = None,
    download: bool = True,
) -> dict[str, Any]:
    """다운로드 — DASH 병합(mp4), 필요 시 ffmpeg."""
    clients = list(player_clients or YT_DOWNLOAD_PLAYER_CLIENT_ROTATIONS[0])
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "format": format_selector,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "overwrites": True,
        "nocheckcertificate": True,
        "format_sort": list(YT_DOWNLOAD_FORMAT_SORT),
        "prefer_free_formats": False,
        "js_runtimes": _js_runtimes_for_ydl(),
        "logger": _QUIET_YDL_LOGGER,
        "concurrent_fragment_downloads": 12,
        "retries": 6,
        "fragment_retries": 6,
        "http_chunk_size": 16 * 1024 * 1024,
        "socket_timeout": 90,
        "force_ipv4": True,
        "extractor_args": {
            "youtube": build_youtube_extractor_args(clients, include_missing_pot=True),
        },
    }
    if not download:
        opts["skip_download"] = True
    if getattr(sys, "frozen", False):
        opts["cachedir"] = str(_frozen_cache_dir())
    ffmpeg = _resolve_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        # merge_output_format 만으로 영상+음성 병합 (FFmpegMerger 수동 지정 시 yt-dlp 버전 오류)
        opts["postprocessor_args"] = {
            "ffmpeg": ["-movflags", "+faststart"],
        }
    return opts


def fetch_youtube_progressive_stream(
    video_id: str,
    *,
    max_height: int = YT_MAX_DOWNLOAD_HEIGHT,
) -> dict[str, Any]:
    """영상+음성 합쳐진 progressive URL (브라우저 <video> 재생용)."""
    vid = (video_id or "").strip()
    if not vid:
        raise ValueError("video_id 필요")
    url = f"https://www.youtube.com/watch?v={vid}"
    fmt = (
        f"best[height<={max_height}][ext=mp4][acodec!=none][vcodec!=none]/"
        f"best[height<={max_height}][acodec!=none][vcodec!=none]/"
        "best[ext=mp4][acodec!=none][vcodec!=none]/"
        "22/18/best[acodec!=none][vcodec!=none]/best"
    )
    last_error: Exception | None = None
    for clients in YT_STREAM_PLAYER_CLIENT_ROTATIONS:
        try:
            probe_opts = build_download_ydl_opts(
                fmt,
                "%(id)s",
                player_clients=clients,
                download=False,
            )
            meta = ytdlp_extract_info(url, probe_opts, download=False)
            formats = list(meta.get("formats") or [])
            combined = [
                f
                for f in formats
                if f.get("url")
                and f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none")
                and YT_MIN_DOWNLOAD_HEIGHT
                <= int(f.get("height") or 0)
                <= max_height
            ]
            if not combined:
                combined = [
                    f
                    for f in formats
                    if f.get("url")
                    and f.get("vcodec") not in (None, "none")
                    and f.get("acodec") not in (None, "none")
                    and int(f.get("height") or 0) <= max_height
                ]
            if not combined:
                raise ValueError("progressive(합쳐진) 스트림 없음")
            combined.sort(key=_format_score)
            picked = combined[-1]
            if int(picked.get("height") or 0) < YT_MIN_DOWNLOAD_HEIGHT:
                _log.warning(
                    "progressive id=%s: %sp 미만 (%sp)",
                    vid,
                    YT_MIN_DOWNLOAD_HEIGHT,
                    picked.get("height"),
                )
            stream_url, height = str(picked["url"]), _height_of_format(picked)
            try:
                duration = float(meta.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            http_headers: dict[str, str] = {}
            raw_headers = meta.get("http_headers")
            if isinstance(raw_headers, dict):
                http_headers = {str(k): str(v) for k, v in raw_headers.items()}
            return {
                "url": stream_url,
                "height": height,
                "duration": max(0.0, duration),
                "http_headers": http_headers,
                "progressive": True,
            }
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise ValueError("progressive(합쳐진) 스트림 없음 — DASH 분리만 가능")


def fetch_youtube_stream_info(
    video_id: str,
    *,
    min_height: int = YT_MIN_DOWNLOAD_HEIGHT,
) -> dict[str, Any]:
    """고화질 스트림 URL (다운로드 실패 시 폴백)."""
    if not video_id:
        raise ValueError("video_id 필요")
    last_error: Exception | None = None
    for clients in YT_STREAM_PLAYER_CLIENT_ROTATIONS:
        try:
            probe_opts = build_download_ydl_opts(
                YT_DOWNLOAD_FORMAT,
                "%(id)s",
                player_clients=clients,
                download=False,
            )
            url = f"https://www.youtube.com/watch?v={video_id}"
            meta = ytdlp_extract_info(url, probe_opts, download=False)
            best_info: dict[str, Any] | None = None
            best_h = 0
            for picked in pick_download_format_selectors(meta):
                try:
                    info = _extract_stream_info_with_clients(
                        video_id, picked, clients
                    )
                except Exception:
                    continue
                h = int(info.get("height") or 0)
                if h > best_h:
                    best_h = h
                    best_info = info
                if h >= min_height:
                    return info
            if best_info is not None and best_h > 0:
                return best_info
        except Exception as exc:
            last_error = exc
            continue
        for fmt in YT_DOWNLOAD_FORMAT_CANDIDATES:
            try:
                info = _extract_stream_info_with_clients(video_id, fmt, clients)
                h = int(info.get("height") or 0)
                if h >= min_height or min_height <= 0:
                    return info
            except Exception as exc:
                last_error = exc
                continue
    if last_error:
        raise last_error
    raise ValueError(
        f"{min_height}p 이상 스트림을 가져오지 못했습니다 (YouTube 제한 또는 쿠키 필요)"
    )


def _extract_stream_info_with_clients(
    video_id: str,
    format_selector: str,
    player_clients: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    ydl_opts = {
        **_base_ydl_opts(),
        "format": format_selector,
        "js_runtimes": _js_runtimes_for_ydl(),
        "extractor_args": {
            "youtube": build_youtube_extractor_args(
                player_clients, include_missing_pot=True
            ),
        },
    }
    info = ytdlp_extract_info(
        f"https://www.youtube.com/watch?v={video_id}",
        ydl_opts,
        download=False,
    )
    info = info or {}
    url, picked_height = _pick_stream_url(info)
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    http_headers: dict[str, str] = {}
    raw_headers = info.get("http_headers")
    if isinstance(raw_headers, dict):
        http_headers = {str(k): str(v) for k, v in raw_headers.items()}
    return {
        "url": url,
        "height": picked_height,
        "duration": max(0.0, duration),
        "http_headers": http_headers,
    }


YTDLP_MOCK_PLAYBACK_SECONDS = 3
YTDLP_PROBE_MIN_BYTES = 196_608
YTDLP_PROBE_MAX_BYTES = 2_097_152


def probe_youtube_mock_playback(
    video_id: str,
    *,
    seconds: float = YTDLP_MOCK_PLAYBACK_SECONDS,
) -> tuple[bool, str]:
    """
    방송 전 모의 재생: 스트림 앞부분을 읽어 재생 가능 여부 확인.
    (약 N초 분량 바이트를 받을 수 없으면 yt-dlp 필요로 판단)
    """
    if not video_id:
        return False, "missing_id"
    try:
        meta = fetch_youtube_stream_info(video_id)
    except Exception as exc:
        return False, f"mock_meta:{exc}"

    url = str(meta.get("url") or "").strip()
    if not url:
        return False, "mock_url_empty"

    target = max(
        YTDLP_PROBE_MIN_BYTES,
        min(YTDLP_PROBE_MAX_BYTES, int(seconds * 320 * 1024 / 8)),
    )
    headers = dict(meta.get("http_headers") or {})
    headers["Range"] = f"bytes=0-{target - 1}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            read = 0
            while read < target:
                chunk = resp.read(65536)
                if not chunk:
                    break
                read += len(chunk)
        if read < 65536:
            return False, "mock_short_read"
        return True, "mock_ok"
    except urllib.error.HTTPError as exc:
        return False, f"mock_http_{exc.code}"
    except Exception as exc:
        return False, f"mock_read:{exc}"


def inspect_youtube_playback_mode(video_id: str) -> dict[str, Any]:
    """
    방송과 동일: YouTube iframe(퍼가기) 재생 불가 → yt-dlp 필요.
    web/embed 클라이언트 + embed 페이지를 함께 본다.
    """
    if not video_id:
        raise ValueError("video_id 필요")

    reasons: list[str] = []
    playable = None
    availability = ""
    embed_ok = False

    for client in _IFRAME_PROBE_CLIENTS:
        try:
            info = _extract_info_with_clients(video_id, (client,))
            playable = info.get("playable_in_embed")
            availability = str(info.get("availability") or "").strip().lower()
            if client in ("tv_embedded", "web_embedded") and playable is True:
                embed_ok = True
            blocked, reason = _embed_client_blocked(info)
            if blocked and reason:
                reasons.append(reason)
        except Exception:
            continue

    page_blocked, page_reason = _probe_embed_http_page(video_id)
    if page_blocked and page_reason:
        reasons.append(page_reason)
        embed_ok = False

    if embed_ok:
        reasons = []

    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in reasons:
        if r in seen:
            continue
        seen.add(r)
        unique_reasons.append(r)

    requires_ytdlp = bool(unique_reasons) and not embed_ok
    reason = unique_reasons[0] if unique_reasons else ""

    return {
        "requires_ytdlp": requires_ytdlp,
        "reason": reason,
        "reasons": unique_reasons,
        "playable_in_embed": playable,
        "availability": availability,
    }
