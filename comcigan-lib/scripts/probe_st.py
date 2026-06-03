"""Probe comci.net /st for route codes (dev only)."""
import re
import urllib.request

d = urllib.request.urlopen("http://comci.net:4082/st", timeout=15).read()
t = d.decode("euc-kr", "replace")

m = re.search(r"function school_ra\(sc\)\{\$\.ajax\(\{ url:'\.\/(\d+)\?(\d+)l'", t)
print("search", m.groups() if m else None)

m2 = re.search(r"sc_data\('(\d+_)'", t)
print("timetable_prefix", m2.group(1) if m2 else None)

for pat, name in [
    (r"원자료=Q자료\(자료\.자료(\d+)", "original"),
    (r"일일자료=Q자료\(자료\.자료(\d+)", "daily"),
    (r"자료\.자료(\d+)\[sb\]", "subject"),
    (r"자료\.자료(\d+)\[th\]", "teacher"),
    (r"성명=자료\.자료(\d+)", "name"),
    (r"담임", "homeroom_kw"),
]:
    found = re.search(pat, t)
    print(name, found.group(1) if found and found.lastindex else ("found" if found else None))

# homeroom related snippets
for kw in ["담임", "담임교사", "homeroom", "반담임"]:
    if kw in t:
        idx = t.index(kw)
        print("snippet", kw, repr(t[max(0, idx - 80) : idx + 120]))
