"""MOCKED / pure: business rules R1-R5 and rounding, no database at all."""
import datetime as dt

from app import main

IST = main.IST


def ist(y, mo, d, h, mi, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(dt.timezone.utc)


def late(punch, start="09:30", end="18:30"):
    day = main.attendance_date(punch, start, end)
    return main.compute_late_minutes(punch, main.shift_window(day, start, end)[0])


def test_r2_examples_from_the_contract():
    assert late(ist(2026, 7, 6, 9, 40, 0)) == 0      # exactly 10:00 after start: still within grace
    assert late(ist(2026, 7, 6, 9, 40, 1)) == 10     # one second more: late, counted from shift start
    assert late(ist(2026, 7, 6, 10, 15, 59)) == 45   # floor of 45.98
    assert late(ist(2026, 7, 6, 9, 25, 0)) == 0      # early


def test_r3_overtime_threshold():
    end = main.shift_window(dt.date(2026, 7, 6), "09:30", "18:30")[1]
    assert main.compute_overtime_minutes(ist(2026, 7, 6, 18, 59, 59), end) == 0   # 29 min -> not counted
    assert main.compute_overtime_minutes(ist(2026, 7, 6, 19, 0, 0), end) == 30
    assert main.compute_overtime_minutes(ist(2026, 7, 6, 19, 10, 59), end) == 40  # floor
    assert main.compute_overtime_minutes(ist(2026, 7, 6, 17, 0, 0), end) == 0


def test_r4_half_up_not_bankers():
    start = ist(2026, 7, 6, 9, 0)
    assert main.compute_work_hours(start, start + dt.timedelta(seconds=450))[0] == 0.13   # 0.125 -> 0.13
    assert main.compute_work_hours(start, start + dt.timedelta(seconds=1_350))[0] == 0.38  # 0.375 -> 0.38
    assert main.compute_work_hours(start, start + dt.timedelta(hours=9, minutes=7))[0] == 9.12


def test_r5_half_day_uses_the_rounded_value():
    start = ist(2026, 7, 6, 9, 0)
    assert main.compute_work_hours(start, start + dt.timedelta(seconds=16_182)) == (4.5, False)  # 4.495 -> 4.50
    assert main.compute_work_hours(start, start + dt.timedelta(seconds=16_181)) == (4.49, True)   # 4.4947 -> 4.49
    assert main.compute_work_hours(start, start + dt.timedelta(hours=3, minutes=30)) == (3.5, True)


def test_r1_attendance_date_for_overnight_shift():
    s, e = "22:00", "06:00"
    assert main.attendance_date(ist(2026, 7, 6, 21, 55), s, e) == dt.date(2026, 7, 6)
    assert main.attendance_date(ist(2026, 7, 7, 5, 0), s, e) == dt.date(2026, 7, 6)   # earlier than shift_end
    assert main.attendance_date(ist(2026, 7, 7, 6, 0), s, e) == dt.date(2026, 7, 7)   # not earlier -> same day
    assert main.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == dt.date(2026, 7, 7)  # day shift


def test_r1_ist_date_differs_from_utc_date():
    punch = ist(2026, 7, 7, 1, 0)  # 01:00 IST on the 7th is still the 6th in UTC
    assert punch.date() == dt.date(2026, 7, 6)
    assert main.attendance_date(punch, "09:30", "18:30") == dt.date(2026, 7, 7)


def test_overnight_late_and_overtime_use_the_right_shift_day():
    s, e = "22:00", "06:00"
    punch_in = ist(2026, 7, 6, 21, 55)
    assert late(punch_in, s, e) == 0
    day = main.attendance_date(punch_in, s, e)
    derived = main.derive_fields("PRESENT", punch_in, ist(2026, 7, 7, 6, 40), day, s, e)
    assert derived == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


def test_absent_and_leave_have_zero_derived_values():
    derived = main.derive_fields("LEAVE", None, None, dt.date(2026, 7, 6), "09:30", "18:30")
    assert derived == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_round_half_up_helper():
    assert main.round_half_up(0.125, 2) == 0.13
    assert main.round_half_up(2.675, 2) == 2.68   # float round() would give 2.67
    assert main.round_half_up(0.03125, 4) == 0.0313
    assert main.round_half_up(2, 2) == 2.0


def test_ms_conversion_truncates_to_seconds_and_back():
    assert main.dt_to_ms(main.ms_to_dt(1_783_312_500_900)) == 1_783_312_500_000


def test_count_weekdays():
    assert main.count_weekdays(dt.date(2026, 7, 1), dt.date(2026, 7, 31)) == 23
    assert main.count_weekdays(dt.date(2026, 7, 15), dt.date(2026, 7, 31)) == 13
    assert main.count_weekdays(dt.date(2026, 8, 1), dt.date(2026, 7, 31)) == 0
