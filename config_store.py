"""config.json 로드·저장 (exe 빌드 시 실행 파일 옆에 데이터 저장)."""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8765
WEBSITE_PORT = 2026
ALLOWED_UPLOAD_EXT = {".mp4", ".mkv", ".mp3", ".wav", ".m4a", ".webm", ".ogg", ".flac"}

# 방송 안내 기본 로고 (exe/소스 번들, 절대 경로 저장 금지)
DEFAULT_NEXT_ALERT_LOGO = "bundled/njbs-logo.png"
DEFAULT_NEXT_ALERT_TEXT = "중동중학교 방송부"
DEFAULT_ALERT_THEME = "light"
VALID_ALERT_THEMES = frozenset({"dark", "light"})


def get_install_dir() -> Path:
    """설정·업로드 등 쓰기 가능 경로 (exe와 같은 폴더)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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


def ensure_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)


CF_DEFAULTS: dict[str, Any] = {
    "cloudflare_worker_url": "https://auto-music-player-backend.rukkit.workers.dev",
    "cf_username": "admin",
    "cf_password": "1234",
    "cf_auto_pull_on_start": True,   # pull from DB when app launches
    "cf_auto_push_on_stop": False,   # push to DB when app closes (opt-in)
    "playback_error_stall_seconds": 10,
    "playback_error_recover_mode": "manual",  # manual | auto
}

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
    if changed:
        save_config(data)
    return data


def broadcast_ui_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """방송 화면(종료·다음곡 안내)에 전달할 UI 설정."""
    return {
        "end_broadcast_image": cfg.get("end_broadcast_image", ""),
        "next_alert_logo": resolve_alert_logo_url(cfg.get("next_alert_logo")),
        "next_alert_text": normalize_next_alert_text(cfg.get("next_alert_text")),
        "next_alert_theme": normalize_alert_theme(cfg.get("next_alert_theme")),
        "now_playing_theme": normalize_alert_theme(cfg.get("now_playing_theme")),
    }


def save_config(data: dict[str, Any]) -> None:
    ensure_dirs()
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_setup_complete(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("admin_username") and cfg.get("password_hash"))
