import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from comcigan import ComciganClient  # noqa: E402


def main() -> None:
    c = ComciganClient()
    school = c.search_school("신송", index=0)
    assert school.code > 0
    h = c.get_homeroom_teacher(school.code, 1, 1)
    assert h and h.teacher
    tw = c.get_this_week_timetable(school.code, 1, 1)
    assert tw.days["월"]
    nw = c.get_next_week_timetable(school.code, 1, 1)
    assert nw.week_index == 2
    meta = c.get_school_meta(school.code)
    assert meta.grades
    st = c.get_subject_teachers(school.code)
    assert "국어" in st or len(st) > 0
    ts = c.get_teacher_subjects(school.code)
    assert len(ts) > 0
    print("OK", school.name, h.teacher, tw.week_range, len(st), "teachers mapped")


if __name__ == "__main__":
    main()
