import { TimetableError } from './errors.js';
import type {
  ChangedPeriod,
  ClassTimetable,
  HomeroomInfo,
  PeriodSlot,
  RouteBundle,
  SchoolMeta,
  SubjectTeachers,
  TeacherSubjects,
  WeekLabel,
} from './types.js';
import { WEEKDAY_NAMES } from './types.js';

type Json = Record<string, unknown>;

function dataKey(code: string): string {
  return `자료${code}`;
}

export function weekIndexFromLabel(week: WeekLabel): number {
  if (typeof week === 'number') {
    if (week < 1) throw new Error('date_index must be >= 1');
    return week;
  }
  if (week === 'this') return 1;
  if (week === 'next') return 2;
  throw new Error("week must be 'this', 'next', or a positive number");
}

function decodePeriod(
  code: number | string,
  separator: number,
  subjects: unknown[],
  teachers: unknown[]
): [string, string] {
  const n = typeof code === 'number' ? code : parseInt(String(code), 10);
  if (!n) return ['', ''];
  const subjIdx = Math.floor(n / separator);
  const teachIdx = n % separator;
  const subject = subjIdx >= 0 && subjIdx < subjects.length ? String(subjects[subjIdx]) : '';
  const teacher = teachIdx >= 0 && teachIdx < teachers.length ? String(teachers[teachIdx]) : '';
  return [subject, teacher];
}

function classCount(gradeSizes: number[], virtual: number[], grade: number): number {
  return gradeSizes[grade] - virtual[grade];
}

function* iterGrades(gradeSizes: number[], virtual: number[]): Generator<number> {
  for (let g = 1; g < gradeSizes.length; g++) yield g;
}

function* iterClasses(gradeSizes: number[], virtual: number[], grade: number): Generator<number> {
  for (let c = 1; c <= classCount(gradeSizes, virtual, grade); c++) yield c;
}

function sliceClassMatrix(matrix: unknown[][], grade: number, classNum: number): number[][] {
  const gradeRow = matrix[grade] as unknown[];
  const classRow = gradeRow[classNum] as unknown[];
  const dayCount = classRow[0] as number;
  const days: number[][] = [];
  for (let d = 1; d <= dayCount; d++) {
    const day = classRow[d] as unknown[];
    const periodCount = day[0] as number;
    days.push((day.slice(1, periodCount + 1) as number[]));
  }
  return days;
}

function extractWeekRange(data: Json, weekIndex: number): string {
  const weeks = (data['일자자료'] as unknown[]) ?? [];
  for (const entry of weeks) {
    const row = entry as [number, string];
    if (row[0] === weekIndex) return String(row[1]);
  }
  const first = weeks[0] as [number, string] | undefined;
  return first ? String(first[1]) : '';
}

export function parseSchoolMeta(data: Json, routes: RouteBundle, schoolCode: number): SchoolMeta {
  const teachers = [...((data[dataKey(routes.teacherCode)] as unknown[]) ?? [])].map(String);
  const subjectsRaw = [...((data[dataKey(routes.subjectCode)] as unknown[]) ?? [])];
  const subjects = subjectsRaw.slice(1).map(String);
  const gradeSizes = [...(data['학급수'] as number[])];
  const virtual = [...(data['가상학급수'] as number[])];
  const grades = [...iterGrades(gradeSizes, virtual)];
  const classesPerGrade = grades.map((g) => [...iterClasses(gradeSizes, virtual, g)]);
  const updatedRaw = data[dataKey(routes.updatedCode)];
  const weeks: Array<[number, string]> = [];
  for (const entry of (data['일자자료'] as unknown[]) ?? []) {
    const row = entry as [number, string];
    weeks.push([row[0], String(row[1])]);
  }
  return {
    code: schoolCode,
    name: String(data['학교명'] ?? ''),
    region: String(data['지역명'] ?? ''),
    schoolYear: Number(data['학년도'] ?? 0),
    grades,
    classesPerGrade,
    teachers,
    subjects,
    periodTimes: ((data['일과시간'] as unknown[]) ?? []).map(String),
    lastUpdated: updatedRaw ? String(updatedRaw) : null,
    weekRanges: weeks,
    todayWeekIndex: Number(data['오늘r'] ?? 1),
  };
}

export function parseHomeroom(
  data: Json,
  routes: RouteBundle,
  opts: { grade?: number; classNum?: number } = {}
): HomeroomInfo[] {
  const homeroom = data['담임'] as number[][] | undefined;
  if (!homeroom) return [];
  const teachers = (data[dataKey(routes.teacherCode)] as unknown[]) ?? [];
  const gradeSizes = data['학급수'] as number[];
  const virtual = data['가상학급수'] as number[];
  const result: HomeroomInfo[] = [];
  const grades = opts.grade != null ? [opts.grade] : [...iterGrades(gradeSizes, virtual)];
  for (const g of grades) {
    const classes =
      opts.classNum != null ? [opts.classNum] : [...iterClasses(gradeSizes, virtual, g)];
    for (const c of classes) {
      const idx = homeroom[g - 1][c - 1];
      if (!idx) continue;
      result.push({
        grade: g,
        classNum: c,
        teacher: idx < teachers.length ? String(teachers[idx]) : '',
        teacherIndex: idx,
      });
    }
  }
  return result;
}

export function parseClassTimetable(
  data: Json,
  routes: RouteBundle,
  opts: { schoolCode: number; grade: number; classNum: number; weekIndex: number }
): ClassTimetable {
  const gradeSizes = data['학급수'] as number[];
  const virtual = data['가상학급수'] as number[];
  if (opts.grade >= gradeSizes.length) throw new TimetableError(`grade ${opts.grade} out of range`, 2);
  if (opts.classNum > classCount(gradeSizes, virtual, opts.grade)) {
    throw new TimetableError(`class ${opts.classNum} out of range`, 3);
  }
  const separator = Number(data['분리']);
  const subjects = (data[dataKey(routes.subjectCode)] as unknown[]) ?? [];
  const teachers = (data[dataKey(routes.teacherCode)] as unknown[]) ?? [];
  const daily = data[dataKey(routes.dailyCode)] as unknown[][];
  const original = data[dataKey(routes.originalCode)] as unknown[][];
  const dailyDays = sliceClassMatrix(daily, opts.grade, opts.classNum);
  const originalDays = sliceClassMatrix(original, opts.grade, opts.classNum);
  const times = ((data['일과시간'] as unknown[]) ?? []).map(String);
  const days: Record<string, Array<PeriodSlot | null>> = {};

  dailyDays.forEach((periods, dayIdx) => {
    if (dayIdx >= WEEKDAY_NAMES.length) return;
    const dayName = WEEKDAY_NAMES[dayIdx];
    const orig = originalDays[dayIdx] ?? [];
    const slots: Array<PeriodSlot | null> = [];
    const maxLen = Math.max(periods.length, orig.length);
    for (let p = 0; p < maxLen; p++) {
      const code = periods[p] ?? 0;
      const prevCode = orig[p] ?? 0;
      if (code === 0 && prevCode === 0) {
        slots.push(null);
        continue;
      }
      const [subject, teacher] = decodePeriod(code, separator, subjects, teachers);
      const [prevSubj, prevTeach] = decodePeriod(prevCode, separator, subjects, teachers);
      const changed = code !== prevCode && prevCode !== 0;
      const slot: PeriodSlot = {
        period: p + 1,
        subject,
        teacher,
        changed: changed || (code !== prevCode && code !== 0 && prevCode !== 0),
      };
      if (p < times.length) slot.timeLabel = times[p];
      if (changed || (code !== prevCode && prevCode !== 0)) {
        slot.previousSubject = prevSubj;
        slot.previousTeacher = prevTeach;
      }
      slots.push(slot);
    }
    days[dayName] = slots;
  });

  const weekLabel =
    opts.weekIndex === 1 ? 'this' : opts.weekIndex === 2 ? 'next' : String(opts.weekIndex);
  const updatedRaw = data[dataKey(routes.updatedCode)];

  return {
    schoolCode: opts.schoolCode,
    grade: opts.grade,
    classNum: opts.classNum,
    weekIndex: opts.weekIndex,
    weekLabel,
    weekRange: extractWeekRange(data, opts.weekIndex),
    lastUpdated: updatedRaw ? String(updatedRaw) : null,
    days,
  };
}

export function parseChangedPeriods(
  data: Json,
  routes: RouteBundle,
  opts: { grade?: number; classNum?: number } = {}
): ChangedPeriod[] {
  const gradeSizes = data['학급수'] as number[];
  const virtual = data['가상학급수'] as number[];
  const separator = Number(data['분리']);
  const subjects = (data[dataKey(routes.subjectCode)] as unknown[]) ?? [];
  const teachers = (data[dataKey(routes.teacherCode)] as unknown[]) ?? [];
  const daily = data[dataKey(routes.dailyCode)] as unknown[][];
  const original = data[dataKey(routes.originalCode)] as unknown[][];
  const changed: ChangedPeriod[] = [];
  const grades = opts.grade != null ? [opts.grade] : [...iterGrades(gradeSizes, virtual)];
  for (const g of grades) {
    const classes =
      opts.classNum != null ? [opts.classNum] : [...iterClasses(gradeSizes, virtual, g)];
    for (const c of classes) {
      const dailyDays = sliceClassMatrix(daily, g, c);
      const originalDays = sliceClassMatrix(original, g, c);
      dailyDays.forEach((periods, dayIdx) => {
        if (dayIdx >= WEEKDAY_NAMES.length) return;
        const orig = originalDays[dayIdx] ?? [];
        periods.forEach((code, p) => {
          const prev = orig[p] ?? 0;
          if (code === prev || code === 0) return;
          const [subject, teacher] = decodePeriod(code, separator, subjects, teachers);
          const [previousSubject, previousTeacher] = decodePeriod(prev, separator, subjects, teachers);
          changed.push({
            grade: g,
            classNum: c,
            weekday: dayIdx + 1,
            weekdayName: WEEKDAY_NAMES[dayIdx],
            period: p + 1,
            subject,
            teacher,
            previousSubject,
            previousTeacher,
          });
        });
      });
    }
  }
  return changed;
}

export function parseSubjectTeachers(data: Json, routes: RouteBundle): SubjectTeachers {
  const separator = Number(data['분리']);
  const subjects = (data[dataKey(routes.subjectCode)] as unknown[]) ?? [];
  const teachers = (data[dataKey(routes.teacherCode)] as unknown[]) ?? [];
  const daily = data[dataKey(routes.dailyCode)] as unknown[][];
  const gradeSizes = data['학급수'] as number[];
  const virtual = data['가상학급수'] as number[];
  const mapping = new Map<string, Set<string>>();
  for (const g of iterGrades(gradeSizes, virtual)) {
    for (const c of iterClasses(gradeSizes, virtual, g)) {
      for (const day of sliceClassMatrix(daily, g, c)) {
        for (const code of day) {
          if (code === 0) continue;
          const [subj, teach] = decodePeriod(code, separator, subjects, teachers);
          if (!subj || !teach) continue;
          if (!mapping.has(subj)) mapping.set(subj, new Set());
          mapping.get(subj)!.add(teach);
        }
      }
    }
  }
  const out: SubjectTeachers = {};
  [...mapping.keys()].sort().forEach((k) => {
    out[k] = [...mapping.get(k)!].sort();
  });
  return out;
}

export function parseTeacherSubjects(data: Json, routes: RouteBundle): TeacherSubjects {
  const separator = Number(data['분리']);
  const subjects = (data[dataKey(routes.subjectCode)] as unknown[]) ?? [];
  const teachers = (data[dataKey(routes.teacherCode)] as unknown[]) ?? [];
  const daily = data[dataKey(routes.dailyCode)] as unknown[][];
  const gradeSizes = data['학급수'] as number[];
  const virtual = data['가상학급수'] as number[];
  const mapping = new Map<string, Set<string>>();
  for (const g of iterGrades(gradeSizes, virtual)) {
    for (const c of iterClasses(gradeSizes, virtual, g)) {
      for (const day of sliceClassMatrix(daily, g, c)) {
        for (const code of day) {
          if (code === 0) continue;
          const [subj, teach] = decodePeriod(code, separator, subjects, teachers);
          if (!subj || !teach) continue;
          if (!mapping.has(teach)) mapping.set(teach, new Set());
          mapping.get(teach)!.add(subj);
        }
      }
    }
  }
  const out: TeacherSubjects = {};
  [...mapping.keys()].sort().forEach((k) => {
    if (!k) return;
    out[k] = [...mapping.get(k)!].sort();
  });
  return out;
}
