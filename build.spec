# -*- mode: python ; coding: utf-8 -*-
# PyInstaller — 단일 exe (WebView2Runtime + panel/broadcast/website 번들)
#
# 포함 기능 (2026-05):
#   - WebView2 패널, Edge/Chrome 방송 키오스크, 포트 2026 관리자 웹
#   - Cloudflare D1 동기화, yt-dlp 스트림 폴백(1080p)·선로딩
#   - 방송 재생 오류 감지·복구, DB song_id 동기화 수정
#
# 빌드: build.bat  또는  pyinstaller --noconfirm --clean build.spec
# 주의: runtime_tmpdir 을 빌드 PC 경로로 고정하면 다른 PC에서 LOADER 실패함.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# None = 실행 PC의 %TEMP% 에 자동 압축 해제 (다른 PC 호환)

block_cipher = None
root = Path(SPECPATH)
binaries = []

datas = [
    (str(root / "panel"), "panel"),
    (str(root / "broadcast"), "broadcast"),
    (str(root / "website"), "website"),
    (str(root / "14720088.png"), "."),
]
_ico = root / "app_icon.ico"
if _ico.is_file():
    datas.append((str(_ico), "."))

_bundled_assets = root / "assets" / "bundled"
_logo = _bundled_assets / "njbs-logo.png"
_fallback_logo = root / "4ca7b4607_njbs-logo.png"
if not _logo.is_file() and _fallback_logo.is_file():
    _bundled_assets.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(_fallback_logo, _logo)
    print("INFO: assets/bundled/njbs-logo.png <- 4ca7b4607_njbs-logo.png")
if _bundled_assets.is_dir() and any(_bundled_assets.iterdir()):
    datas.append((str(_bundled_assets), "assets/bundled"))
else:
    print("WARNING: assets/bundled 없음 — njbs-logo.png 를 assets/bundled 에 넣으세요.")

_wv2 = root / "WebView2Runtime"
if _wv2.is_dir():
    datas.append((str(_wv2), "WebView2Runtime"))
else:
    print(
        "WARNING: WebView2Runtime 폴더 없음 — "
        "빌드 전에 python prepare_webview2_runtime.py 를 실행하세요."
    )

for pkg in ("flask", "flask_login", "werkzeug", "jinja2", "certifi"):
    try:
        datas += collect_data_files(pkg, include_py_files=True)
    except Exception:
        pass

hiddenimports_ytdlp: list[str] = []
hiddenimports_extra: list[str] = []
for pkg in ("yt_dlp", "yt_dlp_ejs", "imageio_ffmpeg", "Cryptodome", "certifi", "cffi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        if pkg == "yt_dlp":
            hiddenimports_ytdlp = h
        else:
            hiddenimports_extra += h
    except Exception:
        pass

# 앱 전용 모듈 (Analysis 가 놓치기 쉬운 항목)
_APP_MODULES = (
    "playback_recovery",
    "youtube_download_cache",
    "website_server",
    "network_utils",
    "cloudflare_sync",
    "broadcast_window",
    "panel_window",
    "panel_log",
    "playlist_store",
    "config_store",
    "state",
    "youtube_util",
    "youtube_search",
    "webview2_runtime",
    "win_desktop",
    "tray_icon",
    "single_instance",
    "startup",
    "app_meta",
    "app_icon_util",
)

hiddenimports = [
    *hiddenimports_ytdlp,
    *hiddenimports_extra,
    *_APP_MODULES,
    "engineio.async_drivers.threading",
    "simple_websocket",
    "simple_websocket.ws",
    "certifi",
    "dns",
    "dns.resolver",
    "bcrypt",
    "screeninfo",
    "pystray",
    "pystray._base",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL._imaging",
    "webview2",
    "win32api",
    "win32con",
    "win32gui",
    "pythoncom",
    "pywintypes",
    "six",
    "Cryptodome",
    "Cryptodome.Cipher",
    "Cryptodome.Hash",
    "Cryptodome.PublicKey",
    "concurrent.futures",
]

_collect_packages = (
    "engineio",
    "socketio",
    "flask_socketio",
    "flask",
    "flask_login",
    "flask_wtf",
    "werkzeug",
    "jinja2",
    "itsdangerous",
    "click",
    "markupsafe",
    "wtforms",
    "yt_dlp",
    "yt_dlp_ejs",
    "imageio_ffmpeg",
    "cryptography",
    "certifi",
    "urllib3",
    "requests",
    "pystray",
    "PIL",
)

for pkg in ("pystray", "PIL", "webview2", "pywin32"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

for pkg in _collect_packages:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    ["main.py"],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "pyi_rth_eumbang.py")],
    excludes=[
        "eventlet",
        "gevent",
        "matplotlib",
        "numpy",
        "pandas",
        "Cryptodome.SelfTest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon = root / "app_icon.ico"
if not _icon.is_file():
    _icon = root / "14720088.png"

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="3세대음방시스템",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_icon) if _icon.is_file() else None,
)
