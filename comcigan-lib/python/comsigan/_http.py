from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "comsigan-api/1.0 (+https://github.com/jaewondeveloper/comsigan-api-python)"


def fetch_bytes(url: str, *, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def decode_euc_kr(data: bytes) -> str:
    return data.decode("euc-kr", errors="replace")


def fetch_json(url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    raw = fetch_bytes(url, timeout=timeout).decode("utf-8", errors="replace").replace("\0", "")
    return json.loads(raw)


def build_timetable_url(routes, school_code: int, date_index: int) -> str:
    param = base64.b64encode(
        f"{routes.timetable_prefix}{school_code}_0_{date_index}".encode("ascii")
    ).decode("ascii")
    return f"{routes.endpoint}?{param}"


def build_search_url(routes, query: str) -> str:
    encoded = urllib.parse.quote_from_bytes(query.encode("euc-kr"))
    return f"{routes.endpoint}?{routes.search_route}l{encoded}"
