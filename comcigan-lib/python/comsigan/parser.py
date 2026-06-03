from __future__ import annotations

from collections import defaultdict
from typing import Any

from comsigan.errors import TimetableError
from comsigan.routes import RouteBundle
from comsigan.types import (
    WEEKDAY_NAMES,
    ChangedPeriod,
    ClassTimetable,
    HomeroomInfo,
    PeriodSlot,
    SchoolMeta,
    SubjectTeachers,
    TeacherSubjects,
    WeekLabel,
)


def week_index_from_label(label: WeekLabel | int) -> int:
    if isinstance(label, int):
        if label < 1:
            raise ValueError("date_index must be >= 1")
        return label
    if label == "this":
        return 1
    if label == "next":
        return 2
    raise ValueError("week must be 'this', 'next', or a positive date_index")


def _key(data: dict[str, Any], code: str) -> str:
    return f"자료{code}"


def _decode_period_code(
    code: int | str, separator: int, subjects: list[Any], teachers: list[Any]
) -> tuple[str, str]:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "", ""
    if code == 0:
        return "", ""
    subj_idx = code // separator
    teach_idx = code % separator
    subject = subjects[subj_idx] if 0 <= subj_idx < len(subjects) else ""
    teacher = teachers[teach_idx] if 0 <= teach_idx < len(teachers) else ""
    return str(subject), str(teacher)


def _class_count(grade_sizes: list[int], virtual: list[int], grade: int) -> int:
    return grade_sizes[grade] - virtual[grade]


def _iter_grades(grade_sizes: list[int], virtual: list[int]) -> range:
    return range(1, len(grade_sizes))


def _iter_classes(grade_sizes: list[int], virtual: list[int], grade: int) -> range:
    return range(1, _class_count(grade_sizes, virtual, grade) + 1)


def _extract_week_range(data: dict[str, Any], week_index: int) -> str:
    weeks = data.get("일자자료") or []
    for entry in weeks:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[0] == week_index:
            return str(entry[1])
    if weeks:
        return str(weeks[0][1]) if len(weeks[0]) > 1 else ""
    return ""


def _slice_class_matrix(matrix: list[Any], grade: int, class_num: int) -> list[list[int]]:
    grade_row = matrix[grade][class_num]
    day_count = grade_row[0]
    days: list[list[int]] = []
    for d in range(1, day_count + 1):
        day = grade_row[d]
        period_count = day[0]
        days.append([int(x) if x else 0 for x in day[1 : period_count + 1]])
    return days


def parse_school_meta(data: dict[str, Any], routes: RouteBundle, school_code: int) -> SchoolMeta:
    teachers = list(data[_key(data, routes.teacher_code)])
    subjects_raw = list(data[_key(data, routes.subject_code)])
    subjects = [str(s) for s in subjects_raw[1:]] if subjects_raw else []

    grade_sizes = list(data["학급수"])
    virtual = list(data["가상학급수"])
    grades = list(_iter_grades(grade_sizes, virtual))

    classes_per_grade: list[list[int]] = []
    for g in grades:
        classes_per_grade.append(list(_iter_classes(grade_sizes, virtual, g)))

    updated_raw = data.get(_key(data, routes.updated_code))
    last_updated = str(updated_raw) if updated_raw else None

    weeks: list[tuple[int, str]] = []
    for entry in data.get("일자자료") or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            weeks.append((int(entry[0]), str(entry[1])))

    return SchoolMeta(
        code=school_code,
        name=str(data.get("학교명", "")),
        region=str(data.get("지역명", "")),
        school_year=int(data.get("학년도", 0)),
        grades=grades,
        classes_per_grade=classes_per_grade,
        teachers=[str(t) for t in teachers],
        subjects=subjects,
        period_times=[str(t) for t in data.get("일과시간", [])],
        last_updated=last_updated,
        week_ranges=weeks,
        today_week_index=int(data.get("오늘r", 1)),
    )


def parse_homeroom(
    data: dict[str, Any], routes: RouteBundle, *, grade: int | None = None, class_num: int | None = None
) -> list[HomeroomInfo]:
    homeroom = data.get("담임")
    if not homeroom:
        return []
    teachers = list(data[_key(data, routes.teacher_code)])
    grade_sizes = list(data["학급수"])
    virtual = list(data["가상학급수"])

    result: list[HomeroomInfo] = []
    grades = [grade] if grade is not None else list(_iter_grades(grade_sizes, virtual))
    for g in grades:
        classes = [class_num] if class_num is not None else list(_iter_classes(grade_sizes, virtual, g))
        for c in classes:
            idx = homeroom[g - 1][c - 1]
            if not idx:
                continue
            name = str(teachers[idx]) if idx < len(teachers) else ""
            result.append(HomeroomInfo(grade=g, class_num=c, teacher=name, teacher_index=idx))
    return result


def parse_class_timetable(
    data: dict[str, Any],
    routes: RouteBundle,
    *,
    school_code: int,
    grade: int,
    class_num: int,
    week_index: int,
) -> ClassTimetable:
    grade_sizes = list(data["학급수"])
    virtual = list(data["가상학급수"])
    if grade >= len(grade_sizes):
        raise TimetableError(f"grade {grade} out of range", code=2)
    if class_num > _class_count(grade_sizes, virtual, grade):
        raise TimetableError(f"class {class_num} out of range for grade {grade}", code=3)

    separator = int(data["분리"])
    subjects = list(data[_key(data, routes.subject_code)])
    teachers = list(data[_key(data, routes.teacher_code)])
    daily = data[_key(data, routes.daily_code)]
    original = data[_key(data, routes.original_code)]

    daily_days = _slice_class_matrix(daily, grade, class_num)
    original_days = _slice_class_matrix(original, grade, class_num)
    times = list(data.get("일과시간", []))

    days: dict[str, list[PeriodSlot | None]] = {}
    for day_idx, periods in enumerate(daily_days):
        if day_idx >= len(WEEKDAY_NAMES):
            break
        day_name = WEEKDAY_NAMES[day_idx]
        orig = original_days[day_idx] if day_idx < len(original_days) else []
        slots: list[PeriodSlot | None] = []
        max_len = max(len(periods), len(orig))
        for p in range(max_len):
            code = periods[p] if p < len(periods) else 0
            prev_code = orig[p] if p < len(orig) else 0
            if code == 0 and prev_code == 0:
                slots.append(None)
                continue
            subject, teacher = _decode_period_code(code, separator, subjects, teachers)
            prev_subj, prev_teach = _decode_period_code(prev_code, separator, subjects, teachers)
            changed = code != prev_code and prev_code != 0
            slot: PeriodSlot = {
                "period": p + 1,
                "subject": subject,
                "teacher": teacher,
                "changed": changed or (code != prev_code and code != 0 and prev_code != 0),
            }
            if p < len(times):
                slot["time_label"] = str(times[p])
            if changed or (code != prev_code and prev_code != 0):
                slot["previous_subject"] = prev_subj
                slot["previous_teacher"] = prev_teach
            slots.append(slot)
        days[day_name] = slots

    week_label = "this" if week_index == 1 else ("next" if week_index == 2 else str(week_index))
    updated_raw = data.get(_key(data, routes.updated_code))

    return ClassTimetable(
        school_code=school_code,
        grade=grade,
        class_num=class_num,
        week_index=week_index,
        week_label=week_label,
        week_range=_extract_week_range(data, week_index),
        last_updated=str(updated_raw) if updated_raw else None,
        days=days,
    )


def parse_changed_periods(
    data: dict[str, Any], routes: RouteBundle, *, grade: int | None = None, class_num: int | None = None
) -> list[ChangedPeriod]:
    grade_sizes = list(data["학급수"])
    virtual = list(data["가상학급수"])
    separator = int(data["분리"])
    subjects = list(data[_key(data, routes.subject_code)])
    teachers = list(data[_key(data, routes.teacher_code)])
    daily = data[_key(data, routes.daily_code)]
    original = data[_key(data, routes.original_code)]

    changed: list[ChangedPeriod] = []
    grades = [grade] if grade is not None else list(_iter_grades(grade_sizes, virtual))
    for g in grades:
        classes = [class_num] if class_num is not None else list(_iter_classes(grade_sizes, virtual, g))
        for c in classes:
            daily_days = _slice_class_matrix(daily, g, c)
            original_days = _slice_class_matrix(original, g, c)
            for day_idx, periods in enumerate(daily_days):
                if day_idx >= len(WEEKDAY_NAMES):
                    break
                orig = original_days[day_idx] if day_idx < len(original_days) else []
                for p, code in enumerate(periods):
                    prev = orig[p] if p < len(orig) else 0
                    if code == prev or code == 0:
                        continue
                    subject, teacher = _decode_period_code(code, separator, subjects, teachers)
                    prev_subj, prev_teach = _decode_period_code(prev, separator, subjects, teachers)
                    changed.append(
                        ChangedPeriod(
                            grade=g,
                            class_num=c,
                            weekday=day_idx + 1,
                            weekday_name=WEEKDAY_NAMES[day_idx],
                            period=p + 1,
                            subject=subject,
                            teacher=teacher,
                            previous_subject=prev_subj,
                            previous_teacher=prev_teach,
                        )
                    )
    return changed


def parse_subject_teachers(data: dict[str, Any], routes: RouteBundle) -> SubjectTeachers:
    separator = int(data["분리"])
    subjects = list(data[_key(data, routes.subject_code)])
    teachers = list(data[_key(data, routes.teacher_code)])
    daily = data[_key(data, routes.daily_code)]

    mapping: dict[str, set[str]] = defaultdict(set)
    grade_sizes = list(data["학급수"])
    virtual = list(data["가상학급수"])
    for g in _iter_grades(grade_sizes, virtual):
        for c in _iter_classes(grade_sizes, virtual, g):
            for day in _slice_class_matrix(daily, g, c):
                for code in day:
                    if code == 0:
                        continue
                    subj, teach = _decode_period_code(code, separator, subjects, teachers)
                    if subj and teach:
                        mapping[subj].add(teach)
    return {k: sorted(v) for k, v in sorted(mapping.items())}


def parse_teacher_subjects(data: dict[str, Any], routes: RouteBundle) -> TeacherSubjects:
    separator = int(data["분리"])
    subjects = list(data[_key(data, routes.subject_code)])
    teachers = list(data[_key(data, routes.teacher_code)])
    daily = data[_key(data, routes.daily_code)]

    mapping: dict[str, set[str]] = defaultdict(set)
    grade_sizes = list(data["학급수"])
    virtual = list(data["가상학급수"])
    for g in _iter_grades(grade_sizes, virtual):
        for c in _iter_classes(grade_sizes, virtual, g):
            for day in _slice_class_matrix(daily, g, c):
                for code in day:
                    if code == 0:
                        continue
                    subj, teach = _decode_period_code(code, separator, subjects, teachers)
                    if subj and teach:
                        mapping[teach].add(subj)
    return {k: sorted(v) for k, v in sorted(mapping.items()) if k}
