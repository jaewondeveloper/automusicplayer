from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, TypedDict

WeekLabel = Literal["this", "next"]
WEEKDAY_NAMES = ("월", "화", "수", "목", "금")


class PeriodSlot(TypedDict, total=False):
    period: int
    subject: str
    teacher: str
    room: str
    time_label: str
    changed: bool
    previous_subject: str
    previous_teacher: str


class ChangedPeriod(TypedDict):
    grade: int
    class_num: int
    weekday: int
    weekday_name: str
    period: int
    subject: str
    teacher: str
    previous_subject: str
    previous_teacher: str


@dataclass(frozen=True)
class School:
    region: str
    name: str
    code: int
    region_code: int | None = None


@dataclass
class SchoolMeta:
    code: int
    name: str
    region: str
    school_year: int
    grades: list[int]
    classes_per_grade: list[list[int]]
    teachers: list[str]
    subjects: list[str]
    period_times: list[str]
    last_updated: str | None
    week_ranges: list[tuple[int, str]]
    today_week_index: int


@dataclass
class HomeroomInfo:
    grade: int
    class_num: int
    teacher: str
    teacher_index: int


@dataclass
class ClassTimetable:
    school_code: int
    grade: int
    class_num: int
    week_index: int
    week_label: str
    week_range: str
    last_updated: str | None
    days: dict[str, list[PeriodSlot | None]] = field(default_factory=dict)


SubjectTeachers = dict[str, list[str]]
TeacherSubjects = dict[str, list[str]]
