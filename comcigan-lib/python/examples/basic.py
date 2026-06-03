from comsigan import ComciganClient


def main() -> None:
    client = ComciganClient()
    school = client.search_school("신송", index=0)
    print(f"{school.region} {school.name} (code={school.code})")

    homeroom = client.get_homeroom_teacher(school.code, grade=1, class_num=1)
    print("담임:", homeroom.teacher if homeroom else "(없음)")

    this_week = client.get_this_week_timetable(school.code, 1, 1)
    print("이번 주:", this_week.week_range)
    mon = this_week.days.get("월") or []
    if mon and mon[0]:
        print("월 1교시:", mon[0]["subject"], mon[0]["teacher"])

    changes = client.get_changed_periods(school.code, grade=1, class_num=1)
    print("변동 교시 수:", len(changes))

    subj = client.get_subject_teachers(school.code)
    print("과목 수:", len(subj))


if __name__ == "__main__":
    main()
