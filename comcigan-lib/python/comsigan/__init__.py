"""컴시간알리미(comci.net) 비공식 시간표 클라이언트."""

from comsigan.client import ComciganClient
from comsigan.errors import ComciganError, ParseError, SchoolNotFoundError, TimetableError
from comsigan.types import (
    ChangedPeriod,
    ClassTimetable,
    HomeroomInfo,
    PeriodSlot,
    School,
    SchoolMeta,
    SubjectTeachers,
    TeacherSubjects,
    WeekLabel,
)

__all__ = [
    "ComciganClient",
    "ComciganError",
    "ParseError",
    "SchoolNotFoundError",
    "TimetableError",
    "School",
    "SchoolMeta",
    "ClassTimetable",
    "PeriodSlot",
    "ChangedPeriod",
    "HomeroomInfo",
    "SubjectTeachers",
    "TeacherSubjects",
    "WeekLabel",
]

__version__ = "1.0.0"
