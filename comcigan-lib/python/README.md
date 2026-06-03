# comsigan-api (Python)

[![PyPI version](https://img.shields.io/pypi/v/comsigan?label=PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/comsigan/)
[![Python](https://img.shields.io/pypi/pyversions/comsigan?label=Python&logo=python&logoColor=white)](https://pypi.org/project/comsigan/)
[![CI / Publish](https://github.com/jaewondeveloper/comsigan-api-python/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/jaewondeveloper/comsigan-api-python/actions/workflows/publish-pypi.yml)
[![License](https://img.shields.io/badge/License-All%20Rights%20Reserved-red)](LICENSE)
[![Copyright](https://img.shields.io/badge/Copyright-%C2%A9%202026%20jaewondeveloper-lightgrey)](LICENSE)

[컴시간알리미](http://comci.net:4082/st) **학생 시간표** 페이지를 역공학한 비공식 Python 클라이언트입니다.

- **GitHub**: https://github.com/jaewondeveloper/comsigan-api-python  
- **PyPI**: https://pypi.org/project/comsigan/

> 컴시간알리미는 공식 Open API를 제공하지 않습니다. `/st` 스크립트의 라우트·필드명은 수시로 바뀔 수 있습니다.

## 설치

```bash
pip install comsigan
```

개발(소스) 설치:

```bash
git clone https://github.com/jaewondeveloper/comsigan-api-python.git
cd comsigan-api-python
pip install -e .
```

**의존성**: Python 3.8+ 표준 라이브러리만 사용 (`urllib`).

## 빠른 시작

```python
from comsigan import ComciganClient

client = ComciganClient()

# 1. 학교 검색
schools = client.search_schools("신송")
school = client.search_school("신송", index=0)
print(school.region, school.name, school.code)

# 2. 이번 주 / 다음 주 시간표
this_week = client.get_this_week_timetable(school.code, grade=1, class_num=1)
next_week = client.get_next_week_timetable(school.code, grade=1, class_num=1)
print(this_week.week_range)   # 예: 26-05-25 ~ 26-05-30
print(this_week.days["월"])   # 월요일 교시 목록

# 3. 담임 선생님
homeroom = client.get_homeroom_teacher(school.code, grade=1, class_num=1)
print(homeroom.teacher if homeroom else "없음")

# 4. 변동 시간표 (일일 vs 원 시간표 diff)
changes = client.get_changed_periods(school.code, grade=1, class_num=1)
for c in changes:
    print(f"{c['weekday_name']} {c['period']}교시: {c['previous_subject']} → {c['subject']}")

# 5. 학년 전체 / 전교 시간표
grade_all = client.get_grade_timetables(school.code, grade=1)
school_all = client.get_all_class_timetables(school.code)

# 6. 과목 ↔ 교사 매핑
by_subject = client.get_subject_teachers(school.code)   # {"국어": ["김*", ...], ...}
by_teacher = client.get_teacher_subjects(school.code)   # {"김*": ["국어", "창체"], ...}

# 7. 학교 메타데이터
meta = client.get_school_meta(school.code)
print(meta.grades, meta.period_times, meta.week_ranges)
```

## API 레퍼런스

| 메서드 | 설명 |
|--------|------|
| `search_schools(name)` | 학교명 검색 → `list[School]` |
| `search_school(name, index=0)` | 검색 결과 하나 선택 |
| `get_class_timetable(code, grade, class_num, week="this"\|"next"\|int)` | 학년·반 시간표 |
| `get_this_week_timetable(...)` | 이번 주 (`r=1`) |
| `get_next_week_timetable(...)` | 다음 주 (`r=2`) |
| `get_grade_timetables(code, grade, week=...)` | 해당 학년 전체 반 |
| `get_all_class_timetables(code, week=...)` | 전교 시간표 |
| `get_homeroom_teacher(code, grade, class_num)` | 담임 1명 |
| `get_homeroom_teachers(code, grade=..., class_num=...)` | 담임 목록 |
| `get_changed_periods(code, grade=..., class_num=...)` | 변동 교시 |
| `get_subject_teachers(code)` | 과목별 담당 교사 |
| `get_teacher_subjects(code)` | 교사별 담당 과목 |
| `get_school_meta(code)` | 학년·반 수, 교사·과목 목록, 주차 등 |

`week` / `date_index`: `1` = 이번 주, `2` = 다음 주 (`일자자료` 드롭다운과 동일).

## 시간표 데이터 구조

`ClassTimetable.days`는 요일 키(`"월"`~`"금"`)에 교시 리스트가 매핑됩니다. 각 교시:

```python
{
    "period": 1,
    "subject": "국어",
    "teacher": "김*",
    "time_label": "1(09:10)",
    "changed": False,
    "previous_subject": "...",  # 변동 시에만
    "previous_teacher": "...",
}
```

공강·미배정은 `None`.

## 동작 원리

1. `GET http://comci.net:4082/st` (EUC-KR)에서 라우트·`자료###` 코드 추출  
2. 학교 검색: `GET /{main}?{search}l{EUC-KR 학교명}`  
3. 시간표: `GET /{main}?base64("{prefix}{schoolCode}_0_{r}")`  
4. 교시 코드: `과목 = code // 분리`, `교사 = code % 분리`

## 예외

- `SchoolNotFoundError` — 검색 결과 없음  
- `TimetableError` — 잘못된 학교 코드·학년·반  
- `ParseError` — `/st` 페이지 구조 변경

## PyPI 자동 배포

`main` 브랜치에 코드가 push되면 GitHub Actions가 PyPI에 자동 업로드합니다.

1. GitHub **Settings → Secrets → Actions**에 `PYPI_API_TOKEN` 등록 (PyPI API 토큰)
2. `pyproject.toml`의 **`version`을 올린 뒤** push  
   - PyPI는 같은 버전 재업로드가 불가능합니다 (예: `1.0.0` → `1.0.1`)
3. Actions 탭에서 [Publish to PyPI](https://github.com/jaewondeveloper/comsigan-api-python/actions/workflows/publish-pypi.yml) 워크플로 확인

트리거: `comsigan/**` 또는 `pyproject.toml` 변경 시 · Release 생성 시 · 수동 실행(`workflow_dispatch`)

수동 업로드:

```bash
pip install build twine
python -m build
twine upload dist/*
# Username: __token__  Password: pypi-XXXX...
```

## 라이선스

**All Rights Reserved** — `LICENSE` 참고.  
무단 복제·배포·상업적 이용 금지. 사용 허가는 copyright holder에게 문의하세요.
