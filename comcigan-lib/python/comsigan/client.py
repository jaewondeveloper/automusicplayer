from __future__ import annotations

from typing import Any

from comsigan._http import build_search_url, build_timetable_url, fetch_json
from comsigan.errors import SchoolNotFoundError, TimetableError
from comsigan.parser import (
    parse_changed_periods,
    parse_class_timetable,
    parse_homeroom,
    parse_school_meta,
    parse_subject_teachers,
    parse_teacher_subjects,
    week_index_from_label,
)
from comsigan.routes import RouteBundle, get_routes
from comsigan.types import (
    ChangedPeriod,
    ClassTimetable,
    HomeroomInfo,
    School,
    SchoolMeta,
    SubjectTeachers,
    TeacherSubjects,
    WeekLabel,
)


class ComciganClient:
    """컴시간알리미(comci.net:4082) 학생 시간표 비공식 API 클라이언트."""

    def __init__(self, *, route_cache_ttl: float = 300.0):
        self._route_cache_ttl = route_cache_ttl

    def _routes(self, *, force: bool = False) -> RouteBundle:
        return get_routes(force_refresh=force)

    def _fetch_raw(
        self, school_code: int, week: WeekLabel | int = "this", *, retry: bool = True
    ) -> dict[str, Any]:
        week_index = week_index_from_label(week)
        routes = self._routes()
        url = build_timetable_url(routes, school_code, week_index)
        data = fetch_json(url)
        if not data and retry:
            routes = self._routes(force=True)
            url = build_timetable_url(routes, school_code, week_index)
            data = fetch_json(url)
        if not data:
            raise TimetableError("Empty timetable response (invalid school code?)", code=1)
        return data

    def search_schools(self, name: str) -> list[School]:
        routes = self._routes()
        payload = fetch_json(build_search_url(routes, name))
        rows = payload.get("학교검색") or []
        schools: list[School] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 4:
                continue
            schools.append(
                School(
                    region_code=int(row[0]) if row[0] else None,
                    region=str(row[1]),
                    name=str(row[2]),
                    code=int(row[3]),
                )
            )
        return schools

    def search_school(self, name: str, *, index: int = 0) -> School:
        schools = self.search_schools(name)
        if not schools:
            raise SchoolNotFoundError(f'No school found for query "{name}"')
        if index < 0 or index >= len(schools):
            raise SchoolNotFoundError(f"index {index} out of range ({len(schools)} results)")
        return schools[index]

    def get_school_meta(self, school_code: int, week: WeekLabel | int = "this") -> SchoolMeta:
        data = self._fetch_raw(school_code, week)
        return parse_school_meta(data, self._routes(), school_code)

    def get_class_timetable(
        self,
        school_code: int,
        grade: int,
        class_num: int,
        week: WeekLabel | int = "this",
    ) -> ClassTimetable:
        data = self._fetch_raw(school_code, week)
        return parse_class_timetable(
            data,
            self._routes(),
            school_code=school_code,
            grade=grade,
            class_num=class_num,
            week_index=week_index_from_label(week),
        )

    def get_this_week_timetable(self, school_code: int, grade: int, class_num: int) -> ClassTimetable:
        return self.get_class_timetable(school_code, grade, class_num, week="this")

    def get_next_week_timetable(self, school_code: int, grade: int, class_num: int) -> ClassTimetable:
        return self.get_class_timetable(school_code, grade, class_num, week="next")

    def get_grade_timetables(
        self, school_code: int, grade: int, week: WeekLabel | int = "this"
    ) -> dict[str, ClassTimetable]:
        data = self._fetch_raw(school_code, week)
        routes = self._routes()
        week_index = week_index_from_label(week)
        meta = parse_school_meta(data, routes, school_code)
        result: dict[str, ClassTimetable] = {}
        for c in meta.classes_per_grade[grade - 1] if grade <= len(meta.classes_per_grade) else []:
            label = f"{grade}학년 {c}반"
            result[label] = parse_class_timetable(
                data,
                routes,
                school_code=school_code,
                grade=grade,
                class_num=c,
                week_index=week_index,
            )
        return result

    def get_all_class_timetables(
        self, school_code: int, week: WeekLabel | int = "this"
    ) -> dict[str, ClassTimetable]:
        data = self._fetch_raw(school_code, week)
        routes = self._routes()
        week_index = week_index_from_label(week)
        meta = parse_school_meta(data, routes, school_code)
        result: dict[str, ClassTimetable] = {}
        for g in meta.grades:
            for c in meta.classes_per_grade[g - 1]:
                label = f"{g}학년 {c}반"
                result[label] = parse_class_timetable(
                    data,
                    routes,
                    school_code=school_code,
                    grade=g,
                    class_num=c,
                    week_index=week_index,
                )
        return result

    def get_homeroom_teacher(
        self, school_code: int, grade: int, class_num: int, week: WeekLabel | int = "this"
    ) -> HomeroomInfo | None:
        rows = self.get_homeroom_teachers(school_code, week=week, grade=grade, class_num=class_num)
        return rows[0] if rows else None

    def get_homeroom_teachers(
        self,
        school_code: int,
        week: WeekLabel | int = "this",
        *,
        grade: int | None = None,
        class_num: int | None = None,
    ) -> list[HomeroomInfo]:
        data = self._fetch_raw(school_code, week)
        return parse_homeroom(data, self._routes(), grade=grade, class_num=class_num)

    def get_changed_periods(
        self,
        school_code: int,
        week: WeekLabel | int = "this",
        *,
        grade: int | None = None,
        class_num: int | None = None,
    ) -> list[ChangedPeriod]:
        data = self._fetch_raw(school_code, week)
        return parse_changed_periods(data, self._routes(), grade=grade, class_num=class_num)

    def get_subject_teachers(
        self, school_code: int, week: WeekLabel | int = "this"
    ) -> SubjectTeachers:
        data = self._fetch_raw(school_code, week)
        return parse_subject_teachers(data, self._routes())

    def get_teacher_subjects(
        self, school_code: int, week: WeekLabel | int = "this"
    ) -> TeacherSubjects:
        data = self._fetch_raw(school_code, week)
        return parse_teacher_subjects(data, self._routes())
