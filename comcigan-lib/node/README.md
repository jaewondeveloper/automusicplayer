# comsigan-api (Node.js)

[컴시간알리미](http://comci.net:4082/st) **학생 시간표** 페이지를 역공학한 비공식 Node.js 클라이언트입니다.

- **GitHub**: https://github.com/jaewondeveloper/comsigan-api-nodejs  
- **npm**: `comsigan-api`

> 컴시간알리미는 공식 Open API를 제공하지 않습니다. `/st` 스크립트의 라우트·필드명은 수시로 바뀔 수 있습니다.

## 설치

```bash
npm install comsigan-api
```

개발(소스) 설치:

```bash
git clone https://github.com/jaewondeveloper/comsigan-api-nodejs.git
cd comsigan-api-nodejs
npm install
npm run build
```

**요구사항**: Node.js 18+ (내장 `fetch` 사용)

## 빠른 시작

```javascript
import { ComciganClient } from 'comsigan-api';

const client = new ComciganClient();

// 1. 학교 검색
const schools = await client.searchSchools('신송');
const school = await client.searchSchool('신송', 0);
console.log(school.region, school.name, school.code);

// 2. 이번 주 / 다음 주 시간표
const thisWeek = await client.getThisWeekTimetable(school.code, 1, 1);
const nextWeek = await client.getNextWeekTimetable(school.code, 1, 1);
console.log(thisWeek.weekRange);
console.log(thisWeek.days['월']);

// 3. 담임 선생님
const homeroom = await client.getHomeroomTeacher(school.code, 1, 1);
console.log(homeroom?.teacher);

// 4. 변동 시간표
const changes = await client.getChangedPeriods(school.code, 'this', { grade: 1, classNum: 1 });

// 5. 학년 전체 / 전교
const gradeAll = await client.getGradeTimetables(school.code, 1);
const schoolAll = await client.getAllClassTimetables(school.code);

// 6. 과목 ↔ 교사
const bySubject = await client.getSubjectTeachers(school.code);
const byTeacher = await client.getTeacherSubjects(school.code);

// 7. 메타데이터
const meta = await client.getSchoolMeta(school.code);
```

## API 레퍼런스

| 메서드 | 설명 |
|--------|------|
| `searchSchools(name)` | 학교명 검색 |
| `searchSchool(name, index?)` | 검색 결과 하나 |
| `getClassTimetable(code, grade, classNum, week?)` | 학년·반 시간표 |
| `getThisWeekTimetable(...)` | 이번 주 (`r=1`) |
| `getNextWeekTimetable(...)` | 다음 주 (`r=2`) |
| `getGradeTimetables(code, grade, week?)` | 학년 전체 |
| `getAllClassTimetables(code, week?)` | 전교 |
| `getHomeroomTeacher(...)` | 담임 1명 |
| `getHomeroomTeachers(...)` | 담임 목록 |
| `getChangedPeriods(...)` | 변동 교시 |
| `getSubjectTeachers(code)` | 과목별 교사 |
| `getTeacherSubjects(code)` | 교사별 과목 |
| `getSchoolMeta(code)` | 학교 메타 |

`week`: `'this'` \| `'next'` \| `number` (`1` = 이번 주, `2` = 다음 주)

## Python 버전

https://github.com/jaewondeveloper/comsigan-api-python (`pip install comsigan`)

## 예외

- `SchoolNotFoundError` — 검색 결과 없음  
- `TimetableError` — 잘못된 코드·학년·반  
- `ParseError` — `/st` 구조 변경

## 예제 실행

```bash
npm run build
npm run example
```

## 라이선스

**All Rights Reserved** — `LICENSE` 참고.  
무단 복제·배포·상업적 이용 금지.
