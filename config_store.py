"""config.json 로드·저장 (dev·exe 모두 %LOCALAPPDATA%\\3세대음방시스템)."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from app_meta import EXE_NAME

DEFAULT_PORT = 8765
WEBSITE_PORT = 2026
ALLOWED_UPLOAD_EXT = {".mp4", ".mkv", ".mp3", ".wav", ".m4a", ".webm", ".ogg", ".flac"}

# 방송 안내 기본 로고 (exe/소스 번들, 절대 경로 저장 금지)
DEFAULT_NEXT_ALERT_LOGO = "bundled/njbs-logo.png"
DEFAULT_NEXT_ALERT_TEXT = "중동중학교 방송부"
DEFAULT_ALERT_THEME = "light"
VALID_ALERT_THEMES = frozenset({"dark", "light"})


def _migrate_legacy_data_files(base: Path) -> None:
    """exe 옆·프로젝트 폴더에 있던 config/playlist를 LocalAppData로 1회 이전."""
    legacy_dirs: list[Path] = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):
        legacy_dirs.insert(0, Path(sys.executable).resolve().parent)
    seen: set[Path] = set()
    for legacy in legacy_dirs:
        try:
            resolved = legacy.resolve()
        except OSError:
            continue
        if resolved in seen or resolved == base.resolve():
            continue
        seen.add(resolved)
        for fname in ("config.json", "playlist.json"):
            src = legacy / fname
            dst = base / fname
            if src.is_file() and not dst.is_file():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass


def get_install_dir() -> Path:
    """설정·업로드·yt-dlp 캐시 — dev/main.py·exe 동일 (%LOCALAPPDATA%)."""
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / EXE_NAME
    base.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data_files(base)
    return base


def get_bundle_dir() -> Path:
    """패널/방송 HTML 등 번들 리소스 (PyInstaller _MEIPASS)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return get_install_dir()
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """실행 시점 번들 경로 (exe 압축 해제 후)."""
    return get_bundle_dir()


INSTALL_DIR = get_install_dir()
BUNDLE_DIR = get_bundle_dir()

CONFIG_PATH = INSTALL_DIR / "config.json"
UPLOADS_DIR = INSTALL_DIR / "uploads"
ASSETS_DIR = INSTALL_DIR / "assets"


def bundled_assets_dir() -> Path:
    return bundle_dir() / "assets" / "bundled"


def get_exe_dir() -> Path:
    """배포 exe가 있는 폴더 (개발 시 프로젝트 루트)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_dirs() -> None:
    """앱 데이터·캐시·로그 폴더를 첫 실행 시 모두 생성."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for path in (
        UPLOADS_DIR,
        ASSETS_DIR,
        ASSETS_DIR / "bundled",
        INSTALL_DIR / "logs",
        INSTALL_DIR / "ytdlp_broadcast",
        INSTALL_DIR / "yt-dlp-cache",
    ):
        path.mkdir(parents=True, exist_ok=True)
    keep = UPLOADS_DIR / ".gitkeep"
    if not keep.exists():
        try:
            keep.touch()
        except OSError:
            pass


CF_DEFAULTS: dict[str, Any] = {
    "cloudflare_worker_url": "https://auto-music-player-backend.rukkit.workers.dev",
    "cf_username": "admin",
    "cf_password": "1234",
    "cf_auto_pull_on_start": True,   # pull from DB when app launches
    "cf_auto_push_on_stop": False,   # push to DB when app closes (opt-in)
    "playback_error_stall_seconds": 10,
    "playback_error_recover_mode": "manual",  # manual | auto
    # YouTube: cookies.txt 또는 브라우저명(edge/chrome) — 비우면 쿠키 없이 다운로드
    "youtube_cookies_browser": "",
    "youtube_cookies_file": "",
    "youtube_allow_stream_fallback": True,
    "youtube_enforce_min_height": False,
    "youtube_embed_only": True,
    "youtube_iframe_quality": "highres",
}

YOUTUBE_IFRAME_QUALITIES = frozenset(
    {"highres", "hd1440", "hd1080", "hd720", "large", "medium"}
)

BRANDING_DEFAULTS: dict[str, Any] = {
    "next_alert_logo": DEFAULT_NEXT_ALERT_LOGO,
    "next_alert_text": DEFAULT_NEXT_ALERT_TEXT,
    "next_alert_theme": DEFAULT_ALERT_THEME,
    "now_playing_theme": DEFAULT_ALERT_THEME,
}


def normalize_next_alert_text(value: Any) -> str:
    t = str(value or "").strip()
    return t if t else DEFAULT_NEXT_ALERT_TEXT


def normalize_alert_theme(value: Any) -> str:
    v = str(value or DEFAULT_ALERT_THEME).strip().lower()
    return v if v in VALID_ALERT_THEMES else DEFAULT_ALERT_THEME


def normalize_next_alert_logo(stored: Any) -> str:
    """config.json 에 저장할 상대 경로 (절대 경로·Windows 경로 거부)."""
    raw = str(stored or "").strip().replace("\\", "/")
    if not raw:
        return DEFAULT_NEXT_ALERT_LOGO
    if ":" in raw or raw.startswith("/"):
        return DEFAULT_NEXT_ALERT_LOGO
    if raw.startswith("assets/bundled/"):
        return "bundled/" + Path(raw).name
    if raw == "bundled/njbs-logo.png" or raw.startswith("bundled/"):
        return raw
    if raw.startswith("assets/"):
        return raw
    return DEFAULT_NEXT_ALERT_LOGO


def resolve_alert_logo_url(stored: Any) -> str:
    """방송/패널에서 쓰는 HTTP 경로."""
    rel = normalize_next_alert_logo(stored)
    if rel.startswith("bundled/"):
        return f"/assets/bundled/{Path(rel).name}"
    if rel.startswith("assets/"):
        return "/" + rel
    return f"/assets/bundled/njbs-logo.png"


YOUTUBE_PLAYBACK_MODES = frozenset({"download", "stream", "iframe"})


def normalize_youtube_playback_mode(value: Any) -> str:
    """download=로컬 파일, stream=다운 없이 DASH 실시간 스트림, iframe=YouTube 퍼가기."""
    mode = str(value or "iframe").strip().lower()
    if mode not in YOUTUBE_PLAYBACK_MODES:
        return "iframe"
    return mode


def normalize_youtube_iframe_quality(value: Any) -> str:
    q = str(value or "highres").strip().lower()
    return q if q in YOUTUBE_IFRAME_QUALITIES else "highres"


def youtube_embed_only(cfg: dict[str, Any] | None = None) -> bool:
    """True면 임베드 검사 후 YouTube 퍼가기만 사용 (yt-dlp 다운로드·로컬 재생 생략)."""
    if cfg is None:
        cfg = load_config()
    return bool(cfg.get("youtube_embed_only", True))


def youtube_stream_only(cfg: dict[str, Any] | None = None) -> bool:
    """True면 스트리밍 전용(stream 모드). iframe/download 는 퍼가기·로컬 파일."""
    if cfg is None:
        cfg = load_config()
    return normalize_youtube_playback_mode(cfg.get("youtube_playback_mode")) == "stream"


def load_config() -> dict[str, Any]:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        cfg = {
            "secret_key": secrets.token_hex(32),
            "admin_username": "",
            "password_hash": "",
            "port": DEFAULT_PORT,
            "end_broadcast_image": "",
            **BRANDING_DEFAULTS,
            "autostart": False,
            "broadcast_browser": "auto",
            "onboarding_complete": False,
            "playback_error_stall_seconds": 10,
            "playback_error_recover_mode": "manual",
            "youtube_playback_mode": "iframe",
            "youtube_embed_only": True,
            "youtube_iframe_quality": "highres",
            **CF_DEFAULTS,
        }
        save_config(cfg)
        return cfg
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("secret_key"):
        data["secret_key"] = secrets.token_hex(32)
        save_config(data)
    if "onboarding_complete" not in data and is_setup_complete(data):
        data["onboarding_complete"] = True
        save_config(data)
    # Inject CF defaults for existing configs missing them
    changed = False
    for k, v in CF_DEFAULTS.items():
        if k not in data:
            data[k] = v
            changed = True
    for k, v in BRANDING_DEFAULTS.items():
        if k not in data:
            data[k] = v
            changed = True
    logo_norm = normalize_next_alert_logo(data.get("next_alert_logo"))
    if data.get("next_alert_logo") != logo_norm:
        data["next_alert_logo"] = logo_norm
        changed = True
    text_norm = normalize_next_alert_text(data.get("next_alert_text"))
    if data.get("next_alert_text") != text_norm:
        data["next_alert_text"] = text_norm
        changed = True
    if "youtube_enforce_min_height" not in data:
        data["youtube_enforce_min_height"] = True
        changed = True
    if "youtube_playback_mode" not in data:
        data["youtube_playback_mode"] = "iframe"
        changed = True
    mode_norm = normalize_youtube_playback_mode(data.get("youtube_playback_mode"))
    if data.get("youtube_playback_mode") != mode_norm:
        data["youtube_playback_mode"] = mode_norm
        changed = True
    if data.get("youtube_playback_mode") == "stream":
        data["youtube_playback_mode"] = "iframe"
        changed = True
    if "youtube_embed_only" not in data:
        data["youtube_embed_only"] = True
        changed = True
    q_norm = normalize_youtube_iframe_quality(data.get("youtube_iframe_quality"))
    if data.get("youtube_iframe_quality") != q_norm:
        data["youtube_iframe_quality"] = q_norm
        changed = True
    env_cookies = str(os.getenv("YTDLP_COOKIES", "") or "").strip()
    if env_cookies and not str(data.get("youtube_cookies_file") or "").strip():
        data["youtube_cookies_file"] = env_cookies
        changed = True
    if changed:
        save_config(data)
    return data


def broadcast_ui_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """방송 화면(종료·다음곡 안내·YouTube 재생)에 전달할 UI 설정."""
    return {
        "end_broadcast_image": cfg.get("end_broadcast_image", ""),
        "next_alert_logo": resolve_alert_logo_url(cfg.get("next_alert_logo")),
        "next_alert_text": normalize_next_alert_text(cfg.get("next_alert_text")),
        "next_alert_theme": normalize_alert_theme(cfg.get("next_alert_theme")),
        "now_playing_theme": normalize_alert_theme(cfg.get("now_playing_theme")),
        "youtube_embed_only": youtube_embed_only(cfg),
        "youtube_iframe_quality": normalize_youtube_iframe_quality(
            cfg.get("youtube_iframe_quality")
        ),
    }


def save_config(data: dict[str, Any]) -> None:
    ensure_dirs()
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_setup_complete(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("admin_username") and cfg.get("password_hash"))
