# comcigan-lib

[컴시간알리미](http://comci.net:4082/st) **학생 시간표** 페이지를 역공학한 비공식 클라이언트입니다.  
Python·Node.js 두 가지 구현을 제공합니다.

> **주의**: 컴시간알리미는 공식 Open API를 제공하지 않습니다. `/st` 스크립트의 라우트·필드명은 수시로 바뀔 수 있으며, 본 라이브러리는 교육·개인 프로젝트 용도로만 사용하세요.

## 지원 기능

| 기능 | 설명 |
|------|------|
| 학교 검색 | 학교명으로 지역·코드 조회 |
| 학년/반 시간표 | 일일 시간표(`자료147`) 파싱 |
| 이번 주 / 다음 주 | Base64 파라미터의 `r` (`1` / `2`) |
| 전 학년·전교 | 학급수·가상학급수 기준 전체 조회 |
| 담임 선생님 | JSON `담임` 배열 + 교사 목록 |
| 변동 시간표 | 일일 vs 원 시간표(`자료481`) diff |
| 과목별 선생님 | 시간표 전체 스캔 |
| 선생님별 과목 | 시간표 전체 스캔 |

## 동작 원리 (요약)

1. `GET http://comci.net:4082/st` (EUC-KR)에서 메인 라우트·검색 라우트·`sc_data` 접두사·`자료###` 코드를 정규식으로 추출합니다.
2. 학교 검색: `GET /{main}?{search}l{EUC-KR 학교명}` → `학교검색`
3. 시간표: `GET /{main}?base64("{prefix}{schoolCode}_0_{r}")` → JSON (NUL 제거 후 파싱)
4. 교시 코드: `과목 = code // 분리`, `교사 = code % 분리` (현재 `분리`는 보통 `1000`)

자세한 필드 구조는 [star0202/comcigan.ts/docs](https://github.com/star0202/comcigan.ts/blob/main/docs/README.md)와 유사합니다.

## 설치

### Python

```bash
cd python
pip install -e .
```

```python
from comcigan import ComciganClient

client = ComciganClient()
school = client.search_school("신송")
tt = client.get_this_week_timetable(school.code, 1, 1)
print(tt.days["월"])
print(client.get_homeroom_teacher(school.code, 1, 1))
```

### Node.js (≥18)

```bash
cd node
npm install
npm run build
```

```javascript
import { ComciganClient } from 'comcigan';

const client = new ComciganClient();
const school = await client.searchSchool('신송');
const tt = await client.getThisWeekTimetable(school.code, 1, 1);
console.log(tt.days['월']);
```

## API 대응표

| Python | Node.js |
|--------|---------|
| `search_schools` | `searchSchools` |
| `get_class_timetable(..., week="this"\|"next")` | `getClassTimetable` |
| `get_this_week_timetable` | `getThisWeekTimetable` |
| `get_next_week_timetable` | `getNextWeekTimetable` |
| `get_grade_timetables` | `getGradeTimetables` |
| `get_all_class_timetables` | `getAllClassTimetables` |
| `get_homeroom_teacher(s)` | `getHomeroomTeacher(s)` |
| `get_changed_periods` | `getChangedPeriods` |
| `get_subject_teachers` | `getSubjectTeachers` |
| `get_teacher_subjects` | `getTeacherSubjects` |
| `get_school_meta` | `getSchoolMeta` |

`week` / `date_index`: `1` = 이번 주, `2` = 다음 주 (드롭다운 `일자자료`와 동일).

## GitHub에 올리기

```bash
cd comcigan-lib
git init
git add .
git commit -m "Add comcigan-lib Python and Node clients"
gh repo create comcigan-lib --public --source=. --push
```

## 라이선스

MIT — `LICENSE` 참고.
