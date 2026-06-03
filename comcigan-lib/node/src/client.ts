import { buildSearchUrl, buildTimetableUrl, fetchJson } from './http.js';
import { SchoolNotFoundError, TimetableError } from './errors.js';
import {
  parseChangedPeriods,
  parseClassTimetable,
  parseHomeroom,
  parseSchoolMeta,
  parseSubjectTeachers,
  parseTeacherSubjects,
  weekIndexFromLabel,
} from './parser.js';
import { getRoutes } from './routes.js';
import type {
  ChangedPeriod,
  ClassTimetable,
  HomeroomInfo,
  School,
  SchoolMeta,
  SubjectTeachers,
  TeacherSubjects,
  WeekLabel,
} from './types.js';

export class ComciganClient {
  async searchSchools(name: string): Promise<School[]> {
    const routes = await getRoutes();
    const payload = await fetchJson(buildSearchUrl(routes, name));
    const rows = (payload['학교검색'] as unknown[]) ?? [];
    const schools: School[] = [];
    for (const row of rows) {
      if (!Array.isArray(row) || row.length < 4) continue;
      schools.push({
        regionCode: row[0] != null ? Number(row[0]) : null,
        region: String(row[1]),
        name: String(row[2]),
        code: Number(row[3]),
      });
    }
    return schools;
  }

  async searchSchool(name: string, index = 0): Promise<School> {
    const schools = await this.searchSchools(name);
    if (!schools.length) throw new SchoolNotFoundError(`No school found for "${name}"`);
    if (index < 0 || index >= schools.length) {
      throw new SchoolNotFoundError(`index ${index} out of range (${schools.length} results)`);
    }
    return schools[index];
  }

  private async fetchRaw(schoolCode: number, week: WeekLabel, retry = true): Promise<Record<string, unknown>> {
    const weekIndex = weekIndexFromLabel(week);
    let routes = await getRoutes();
    let data = await fetchJson(buildTimetableUrl(routes, schoolCode, weekIndex));
    if (!Object.keys(data).length && retry) {
      routes = await getRoutes(true);
      data = await fetchJson(buildTimetableUrl(routes, schoolCode, weekIndex));
    }
    if (!Object.keys(data).length) {
      throw new TimetableError('Empty timetable response', 1);
    }
    return data;
  }

  async getSchoolMeta(schoolCode: number, week: WeekLabel = 'this'): Promise<SchoolMeta> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    return parseSchoolMeta(data, routes, schoolCode);
  }

  async getClassTimetable(
    schoolCode: number,
    grade: number,
    classNum: number,
    week: WeekLabel = 'this'
  ): Promise<ClassTimetable> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    return parseClassTimetable(data, routes, {
      schoolCode,
      grade,
      classNum,
      weekIndex: weekIndexFromLabel(week),
    });
  }

  getThisWeekTimetable(schoolCode: number, grade: number, classNum: number) {
    return this.getClassTimetable(schoolCode, grade, classNum, 'this');
  }

  getNextWeekTimetable(schoolCode: number, grade: number, classNum: number) {
    return this.getClassTimetable(schoolCode, grade, classNum, 'next');
  }

  async getGradeTimetables(
    schoolCode: number,
    grade: number,
    week: WeekLabel = 'this'
  ): Promise<Record<string, ClassTimetable>> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    const weekIndex = weekIndexFromLabel(week);
    const meta = parseSchoolMeta(data, routes, schoolCode);
    const result: Record<string, ClassTimetable> = {};
    for (const c of meta.classesPerGrade[grade - 1] ?? []) {
      result[`${grade}학년 ${c}반`] = parseClassTimetable(data, routes, {
        schoolCode,
        grade,
        classNum: c,
        weekIndex,
      });
    }
    return result;
  }

  async getAllClassTimetables(
    schoolCode: number,
    week: WeekLabel = 'this'
  ): Promise<Record<string, ClassTimetable>> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    const weekIndex = weekIndexFromLabel(week);
    const meta = parseSchoolMeta(data, routes, schoolCode);
    const result: Record<string, ClassTimetable> = {};
    for (const g of meta.grades) {
      for (const c of meta.classesPerGrade[g - 1] ?? []) {
        result[`${g}학년 ${c}반`] = parseClassTimetable(data, routes, {
          schoolCode,
          grade: g,
          classNum: c,
          weekIndex,
        });
      }
    }
    return result;
  }

  async getHomeroomTeacher(
    schoolCode: number,
    grade: number,
    classNum: number,
    week: WeekLabel = 'this'
  ): Promise<HomeroomInfo | null> {
    const rows = await this.getHomeroomTeachers(schoolCode, week, { grade, classNum });
    return rows[0] ?? null;
  }

  async getHomeroomTeachers(
    schoolCode: number,
    week: WeekLabel = 'this',
    opts: { grade?: number; classNum?: number } = {}
  ): Promise<HomeroomInfo[]> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    return parseHomeroom(data, routes, opts);
  }

  async getChangedPeriods(
    schoolCode: number,
    week: WeekLabel = 'this',
    opts: { grade?: number; classNum?: number } = {}
  ): Promise<ChangedPeriod[]> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    return parseChangedPeriods(data, routes, opts);
  }

  async getSubjectTeachers(schoolCode: number, week: WeekLabel = 'this'): Promise<SubjectTeachers> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    return parseSubjectTeachers(data, routes);
  }

  async getTeacherSubjects(schoolCode: number, week: WeekLabel = 'this'): Promise<TeacherSubjects> {
    const data = await this.fetchRaw(schoolCode, week);
    const routes = await getRoutes();
    return parseTeacherSubjects(data, routes);
  }
}
