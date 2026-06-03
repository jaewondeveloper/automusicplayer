from __future__ import annotations

import re
import time
from dataclasses import dataclass

from comsigan.errors import ParseError
from comsigan._http import fetch_bytes, decode_euc_kr

BASE_URL = "http://comci.net:4082"
ST_PATH = "/st"


@dataclass(frozen=True)
class RouteBundle:
    main_route: str
    search_route: str
    timetable_prefix: str
    original_code: str
    daily_code: str
    subject_code: str
    teacher_code: str
    updated_code: str

    @property
    def endpoint(self) -> str:
        return f"{BASE_URL}/{self.main_route}"


_CACHE: RouteBundle | None = None
_CACHE_AT = 0.0
_CACHE_TTL = 300.0


def _extract_one(pattern: str, text: str, label: str) -> str:
    m = re.search(pattern, text)
    if not m:
        raise ParseError(f"Could not find {label} in /st (API layout may have changed)")
    return m.group(1)


def parse_routes(st_html: str) -> RouteBundle:
    m = re.search(r"function school_ra\(sc\)\{\$\.ajax\(\{ url:'\.\/(\d+)\?(\d+)l'", st_html)
    if not m:
        raise ParseError("Could not find search endpoint in /st (API layout may have changed)")
    return RouteBundle(
        main_route=m.group(1),
        search_route=m.group(2),
        timetable_prefix=_extract_one(r"sc_data\('(\d+_)'", st_html, "timetable prefix"),
        original_code=_extract_one(r"원자료=Q자료\(자료\.자료(\d+)", st_html, "original data code"),
        daily_code=_extract_one(r"일일자료=Q자료\(자료\.자료(\d+)", st_html, "daily data code"),
        subject_code=_extract_one(r"자료\.자료(\d+)\[sb\]", st_html, "subject array code"),
        teacher_code=_extract_one(r"자료\.자료(\d+)\[th\]", st_html, "teacher array code"),
        updated_code=_extract_one(r"수정일: '\+H시간표\.자료(\d+)", st_html, "updated timestamp code"),
    )


def get_routes(*, force_refresh: bool = False) -> RouteBundle:
    global _CACHE, _CACHE_AT
    now = time.time()
    if not force_refresh and _CACHE is not None and now - _CACHE_AT < _CACHE_TTL:
        return _CACHE
    st_html = decode_euc_kr(fetch_bytes(f"{BASE_URL}{ST_PATH}"))
    _CACHE = parse_routes(st_html)
    _CACHE_AT = now
    return _CACHE
