import { ComciganClient } from '../dist/index.js';

const client = new ComciganClient();
const school = await client.searchSchool('신송', 0);
console.log(`${school.region} ${school.name} (code=${school.code})`);

const homeroom = await client.getHomeroomTeacher(school.code, 1, 1);
console.log('담임:', homeroom?.teacher ?? '(없음)');

const thisWeek = await client.getThisWeekTimetable(school.code, 1, 1);
console.log('이번 주:', thisWeek.weekRange);
const mon = thisWeek.days['월']?.[0];
if (mon) console.log('월 1교시:', mon.subject, mon.teacher);

const changes = await client.getChangedPeriods(school.code, 'this', { grade: 1, classNum: 1 });
console.log('변동 교시 수:', changes.length);

const subj = await client.getSubjectTeachers(school.code);
console.log('과목 수:', Object.keys(subj).length);
