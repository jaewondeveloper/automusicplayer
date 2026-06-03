export type WeekLabel = 'this' | 'next' | number;

export const WEEKDAY_NAMES = ['월', '화', '수', '목', '금'] as const;

export interface School {
  regionCode: number | null;
  region: string;
  name: string;
  code: number;
}

export interface PeriodSlot {
  period: number;
  subject: string;
  teacher: string;
  room?: string;
  timeLabel?: string;
  changed?: boolean;
  previousSubject?: string;
  previousTeacher?: string;
}

export interface ChangedPeriod {
  grade: number;
  classNum: number;
  weekday: number;
  weekdayName: string;
  period: number;
  subject: string;
  teacher: string;
  previousSubject: string;
  previousTeacher: string;
}

export interface HomeroomInfo {
  grade: number;
  classNum: number;
  teacher: string;
  teacherIndex: number;
}

export interface ClassTimetable {
  schoolCode: number;
  grade: number;
  classNum: number;
  weekIndex: number;
  weekLabel: string;
  weekRange: string;
  lastUpdated: string | null;
  days: Record<string, Array<PeriodSlot | null>>;
}

export interface SchoolMeta {
  code: number;
  name: string;
  region: string;
  schoolYear: number;
  grades: number[];
  classesPerGrade: number[][];
  teachers: string[];
  subjects: string[];
  periodTimes: string[];
  lastUpdated: string | null;
  weekRanges: Array<[number, string]>;
  todayWeekIndex: number;
}

export type SubjectTeachers = Record<string, string[]>;
export type TeacherSubjects = Record<string, string[]>;

export interface RouteBundle {
  mainRoute: string;
  searchRoute: string;
  timetablePrefix: string;
  originalCode: string;
  dailyCode: string;
  subjectCode: string;
  teacherCode: string;
  updatedCode: string;
}
