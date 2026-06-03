import base64
import json
import re
import urllib.parse
import urllib.request

OUT = "c:/Users/신 재원/Desktop/auto-music-player-main/comcigan-lib/scripts/probe_out.json"

ST_URL = "http://comci.net:4082/st"

def fetch_st():
    return urllib.request.urlopen(ST_URL, timeout=15).read().decode("euc-kr", "replace")

def get_routes(st):
    m = re.search(r"function school_ra\(sc\)\{\$\.ajax\(\{ url:'\.\/(\d+)\?(\d+)l'", st)
    main_route, search_route = m.groups()
    prefix = re.search(r"sc_data\('(\d+_)'", st).group(1)
    codes = {}
    for key, pat in [
        ("original", r"원자료=Q자료\(자료\.자료(\d+)"),
        ("daily", r"일일자료=Q자료\(자료\.자료(\d+)"),
        ("subject", r"자료\.자료(\d+)\[sb\]"),
        ("teacher", r"자료\.자료(\d+)\[th\]"),
        ("updated", r"수정일: '\+H시간표\.자료(\d+)"),
    ]:
        codes[key] = re.search(pat, st).group(1)
    return main_route, search_route, prefix, codes

st = fetch_st()
main_route, search_route, prefix, codes = get_routes(st)

q = "신송".encode("euc-kr")
url = f"http://comci.net:4082/{main_route}?{search_route}l" + urllib.parse.quote_from_bytes(q)
schools = json.loads(
    urllib.request.urlopen(url, timeout=15).read().decode("utf-8", "replace").replace("\0", "")
)
sc = schools["학교검색"][0][3]

param = base64.b64encode(f"{prefix}{sc}_0_1".encode()).decode()
turl = f"http://comci.net:4082/{main_route}?{param}"
raw = urllib.request.urlopen(turl, timeout=15).read().decode("utf-8", "replace").replace("\0", "")
data = json.loads(raw)

summary = {
    "codes": codes,
    "keys": list(data.keys()),
    "meta": {k: v for k, v in data.items() if not str(k).startswith("자료")},
    "담임": data.get("담임"),
    "학급수": data.get("학급수"),
    "가상학급수": data.get("가상학급수"),
    "일자자료": data.get("일자자료"),
    "분리": data.get("분리"),
    "teachers_head": data.get("자료" + codes["teacher"], [])[:5],
    "subjects_head": data.get("자료" + codes["subject"], [])[:8],
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print("wrote", OUT)
