"""MOCKED: the HTTP API on top of mongomock (an in-memory imitation of MongoDB). Covers the CRUD side:
employees, punch-in/out, corrections + audit trail, listing, validation and database-failure behaviour.
The analytics pipelines and explain cannot run on mongomock; see test_real_mongo.py for those."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app import main
from conftest import H, IN_1005, make_employee

# ------------------------------- employees ---------------------------------- #


def test_create_and_list_employees(mock_client):
    created = make_employee(mock_client)
    assert created["shift_start"] == "09:30" and created["shift_end"] == "18:30"   # defaults
    assert isinstance(created["created_at"], int) and "_id" not in created
    make_employee(mock_client, "EMP0003", department="Sales")
    make_employee(mock_client, "EMP0002")
    page = mock_client.get("/employees").json()
    assert [e["emp_code"] for e in page["items"]] == ["EMP0001", "EMP0002", "EMP0003"]   # sorted by emp_code
    filtered = mock_client.get("/employees", params={"department": "Engineering", "page_size": 1}).json()
    assert filtered["total"] == 2 and len(filtered["items"]) == 1                         # total = filtered set
    second = mock_client.get("/employees", params={"department": "Engineering", "page_size": 1, "page": 2}).json()
    assert [e["emp_code"] for e in second["items"]] == ["EMP0002"]                        # page 1 is not skipped
    assert mock_client.get("/employees", params={"department": "engineering"}).json()["total"] == 0  # case-sensitive


def test_duplicate_employee_is_409(mock_client):
    make_employee(mock_client)
    again = mock_client.post("/employees", json={"emp_code": "EMP0001", "name": "X", "email": "x@y.zz",
                                                 "department": "D", "joined_on": "2026-01-01"})
    assert again.status_code == 409


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
    {"name": ""}, {"name": "x" * 101}, {"email": "not-an-email"}, {"department": ""},
    {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_end": "18:60"},
    {"shift_start": "09:30", "shift_end": "09:30"},          # shift_start must differ from shift_end
    {"joined_on": "2026-02-30"}, {"joined_on": "05-01-2026"}, {"name": 5},
])
def test_invalid_employee_payloads_are_422(mock_client, patch):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "D", "joined_on": "2026-01-05", **patch}
    assert mock_client.post("/employees", json=body).status_code == 422


def test_missing_required_employee_field_is_422(mock_client):
    assert mock_client.post("/employees", json={"emp_code": "EMP0001"}).status_code == 422


def test_concurrent_duplicate_employee_creates_yield_one_201(mock_client):
    body = {"emp_code": "EMP0009", "name": "A", "email": "a@b.co", "department": "D", "joined_on": "2026-01-05"}
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: mock_client.post("/employees", json=body).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


# --------------------------- punch-in / punch-out ---------------------------- #
def test_punch_in_then_out_happy_path(mock_client):
    make_employee(mock_client)
    r = mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005})
    assert r.status_code == 201
    assert r.json() == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": IN_1005,
                        "punch_out": None, "work_hours": None, "late_minutes": 35, "overtime_minutes": 0,
                        "half_day": False, "history": []}
    out = mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 9 * H + 5 * 60_000})
    assert out.status_code == 200
    body = out.json()
    assert body["work_hours"] == 9.08 and body["overtime_minutes"] == 40 and body["half_day"] is False
    assert body["punch_out"] == IN_1005 + 9 * H + 5 * 60_000 and body["history"] == []   # punches never write history


def test_punch_in_truncates_to_whole_seconds(mock_client):
    make_employee(mock_client)
    r = mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1_783_310_400_900})  # 09:30:00.900
    assert r.json()["punch_in"] == 1_783_310_400_000


def test_punch_in_defaults_and_statuses(mock_client):
    make_employee(mock_client)
    r = mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})   # now, PRESENT
    assert r.status_code == 201 and r.json()["status"] == "PRESENT" and r.json()["punch_in"] % 1000 == 0
    make_employee(mock_client, "EMP0002")
    assert mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "status": "WFH"}).json()["status"] == "WFH"


def test_punch_in_errors(mock_client):
    make_employee(mock_client)
    assert mock_client.post("/attendance/punch-in", json={"emp_code": "EMP7777"}).status_code == 404   # was a 500 in the starter
    assert mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005}).status_code == 201
    assert mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 60_000}).status_code == 409


@pytest.mark.parametrize("bad", [
    {"punched_at": 1_783_312_500},            # seconds
    {"punched_at": 1_783_312_500_000.5},      # float
    {"punched_at": 1_783_312_500_000.0},      # float that looks integral
    {"punched_at": "1783312500000"},          # string
    {"punched_at": None},                     # null
    {"punched_at": True},                     # bool
    {"punched_at": 4_102_444_800_001},        # out of range
    {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "BOGUS"},
])
def test_punch_in_bad_payloads_are_422(mock_client, bad):
    make_employee(mock_client)
    assert mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", **bad}).status_code == 422


def test_concurrent_punch_ins_yield_exactly_one_201(mock_client):
    make_employee(mock_client)
    body = {"emp_code": "EMP0001", "punched_at": IN_1005}
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: mock_client.post("/attendance/punch-in", json=body).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


def test_punch_out_errors(mock_client):
    make_employee(mock_client)
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP7777"}).status_code == 404       # unknown employee
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005}).status_code == 404  # no punch-in
    mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005})
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005}).status_code == 422   # not after
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 - H}).status_code == 404  # before any punch-in
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 24 * H + 1000}).status_code == 422
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 9 * H}).status_code == 200
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 10 * H}).status_code == 409


def test_punch_out_exactly_24_hours_is_allowed(mock_client):
    make_employee(mock_client)
    mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005})
    assert mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 24 * H}).status_code == 200


def test_concurrent_punch_outs_yield_exactly_one_200(mock_client):
    make_employee(mock_client)
    mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005})
    body = {"emp_code": "EMP0001", "punched_at": IN_1005 + 9 * H}
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: mock_client.post("/attendance/punch-out", json=body).status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


def test_half_day_after_punch_out(mock_client):
    make_employee(mock_client)
    mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005})
    r = mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 3 * H + 30 * 60_000})
    assert r.json()["work_hours"] == 3.5 and r.json()["half_day"] is True


def test_overnight_shift_across_midnight(mock_client):
    make_employee(mock_client, "EMP0005", shift_start="22:00", shift_end="06:00")
    punch_in = 1_783_355_100_000      # 2026-07-06 21:55 IST
    punch_out = 1_783_386_600_000     # 2026-07-07 06:40 IST
    r = mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": punch_in})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    out = mock_client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": punch_out}).json()
    assert out["date"] == "2026-07-06" and out["work_hours"] == 8.75 and out["overtime_minutes"] == 40
    # An early-morning punch-in belongs to the shift that started the previous day.
    make_employee(mock_client, "EMP0006", shift_start="22:00", shift_end="06:00")
    early = mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0006", "punched_at": 1_783_380_000_000})  # 04:50 IST on the 7th
    assert early.json()["date"] == "2026-07-06"


# ------------------------------ regularization -------------------------------- #
def seeded_day(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": IN_1005})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": IN_1005 + 9 * H})


def fix(client, **body):
    payload = {"reason": "biometric glitch", "regularized_by": "hr.admin", **body}
    return client.patch("/attendance/EMP0001/2026-07-06", json=payload)


def test_regularize_recomputes_and_records_only_actual_changes(mock_client):
    seeded_day(mock_client)
    new_in = IN_1005 - 37 * 60_000      # 09:28:00 IST
    r = fix(mock_client, punch_in=new_in)
    assert r.status_code == 200
    body = r.json()
    assert body["punch_in"] == new_in and body["late_minutes"] == 0 and body["work_hours"] == 9.62
    entry = body["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": IN_1005, "to": new_in},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 9.0, "to": 9.62},
    }   # status / punch_out / overtime / half_day did not change -> not listed


def test_history_is_append_only_across_corrections(mock_client):
    seeded_day(mock_client)
    first = fix(mock_client, punch_in=IN_1005 - 600_000).json()["history"]
    second = fix(mock_client, reason="second fix", status="WFH").json()["history"]
    assert len(second) == 2 and second[0] == first[0] and second[1]["reason"] == "second fix"
    assert second[1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_regularize_to_absent_clears_everything(mock_client):
    seeded_day(mock_client)
    r = fix(mock_client, status="ABSENT").json()
    assert (r["punch_in"], r["punch_out"], r["work_hours"], r["late_minutes"], r["half_day"]) == (None, None, None, 0, False)
    assert set(r["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
    back = fix(mock_client, status="PRESENT", punch_in=IN_1005)   # a presence status needs a punch_in
    assert back.status_code == 200 and back.json()["late_minutes"] == 35 and len(back.json()["history"]) == 2


@pytest.mark.parametrize("extra", [
    {"status": "LEAVE", "punch_in": IN_1005},          # LEAVE/ABSENT with punch times
    {"status": "ABSENT", "punch_out": IN_1005 + H},
    {"punch_in": IN_1005 + 30 * H},                    # leaves the attendance date (R1)
    {"punch_out": IN_1005 - H},                        # before punch_in
    {"punch_out": IN_1005 + 25 * H},                   # more than 24 h
    {"punch_in": IN_1005 + 10 * H},                    # would put the stored punch_out before the new punch_in
    {},                                                # nothing changes
    {"status": "PRESENT"},                             # same value -> nothing changes
    {"punch_in": IN_1005},                             # same value -> nothing changes
    {"punch_in": 1_783_312_500},                       # seconds-looking
    {"reason": "abc"},                                 # reason too short
    {"regularized_by": ""},
])
def test_invalid_regularizations_are_422(mock_client, extra):
    seeded_day(mock_client)
    assert fix(mock_client, **extra).status_code == 422
    stored = mock_client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert stored["history"] == [] and stored["punch_in"] == IN_1005     # a rejected request changes nothing


def test_regularize_404s_and_missing_fields(mock_client):
    seeded_day(mock_client)
    assert mock_client.patch("/attendance/EMP7777/2026-07-06", json={"reason": "abcde", "regularized_by": "x", "status": "LEAVE"}).status_code == 404
    assert mock_client.patch("/attendance/EMP0001/2026-07-07", json={"reason": "abcde", "regularized_by": "x", "status": "LEAVE"}).status_code == 404
    assert mock_client.patch("/attendance/EMP0001/2026-13-40", json={"reason": "abcde", "regularized_by": "x", "status": "LEAVE"}).status_code == 422
    assert mock_client.patch("/attendance/EMP0001/2026-07-06", json={"status": "LEAVE"}).status_code == 422   # reason + by required


def test_client_cannot_send_derived_fields(mock_client):
    seeded_day(mock_client)
    r = fix(mock_client, status="WFH", work_hours=1.0, late_minutes=99, overtime_minutes=99, half_day=True)
    assert r.status_code == 200 and r.json()["work_hours"] == 9.0 and r.json()["late_minutes"] == 35   # ignored


def test_missing_presence_requires_punch_in_when_reviving_a_leave_record(mock_client):
    make_employee(mock_client)
    main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "LEAVE", "punch_in": None,
                                        "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0})
    assert fix(mock_client, status="PRESENT").status_code == 422
    assert fix(mock_client, status="PRESENT", punch_in=IN_1005).status_code == 200


def test_concurrent_regularizations_never_lose_history(mock_client):
    seeded_day(mock_client)

    def attempt(i):
        return fix(mock_client, reason=f"reason {i:03d}", punch_in=IN_1005 - 60_000 * (i + 1)).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(attempt, range(8)))
    assert set(codes) <= {200, 409} and 200 in codes
    history = mock_client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"]
    assert len(history) == codes.count(200)        # every success is in the trail, nothing overwritten


def test_legacy_record_without_history_or_half_day_is_readable_and_correctable(mock_client):
    make_employee(mock_client)
    from datetime import datetime, timezone
    main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                                        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                        "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                                        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    item = mock_client.get("/attendance").json()["items"][0]
    assert item["history"] == [] and item["half_day"] is False
    r = mock_client.patch("/attendance/EMP0001/2026-07-14", json={"reason": "forgot lunch", "regularized_by": "hr", "status": "WFH"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1


# --------------------------------- listing ----------------------------------- #
def test_list_attendance_sorting_filters_and_pagination(mock_client):
    for code in ("EMP0001", "EMP0002"):
        make_employee(mock_client, code)
    day = 24 * H
    for i in range(3):                                           # 3 consecutive days for both employees
        for code in ("EMP0002", "EMP0001"):
            mock_client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": IN_1005 + i * day, "status": "WFH" if i == 1 else "PRESENT"})
    everything = mock_client.get("/attendance").json()
    keys = [(r["date"], r["emp_code"]) for r in everything["items"]]
    assert everything["total"] == 6
    assert keys == sorted(keys, key=lambda k: (-int(k[0].replace("-", "")), k[1]))               # date desc, emp_code asc
    assert keys[0] == ("2026-07-08", "EMP0001") and keys[1] == ("2026-07-08", "EMP0002")      # date desc, emp_code asc
    assert "_id" not in everything["items"][0] and "id" not in everything["items"][0]
    page2 = mock_client.get("/attendance", params={"page": 2, "page_size": 4}).json()
    assert page2["total"] == 6 and len(page2["items"]) == 2
    assert mock_client.get("/attendance", params={"page": 99}).json()["items"] == []
    ranged = mock_client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert ranged["total"] == 2 and {r["status"] for r in ranged["items"]} == {"WFH"}          # inclusive bounds
    assert mock_client.get("/attendance", params={"status": "WFH", "emp_code": "EMP0001"}).json()["total"] == 1


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-10", "date_to": "2026-07-01"}, {"date_from": "2026-02-30"}, {"date_to": "07/10/2026"},
    {"status": "NOPE"}, {"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"},
])
def test_list_attendance_validation_is_422(mock_client, params):
    assert mock_client.get("/attendance", params=params).status_code == 422


def test_list_employees_validation_is_422(mock_client):
    for params in ({"page": 0}, {"page_size": 101}, {"page_size": 0}):
        assert mock_client.get("/employees", params=params).status_code == 422


# ------------------------- indexes and database failures --------------------- #
def test_indexes_are_created_at_startup_and_idempotently(mock_client):
    names = set(main.db.attendance_logs.index_information())
    assert {"uniq_emp_date", "date_desc_emp", "status_date_emp"} <= names
    assert {"uniq_emp_code", "dept_joined_on", "joined_on"} <= set(main.db.employees.index_information())
    assert main.ensure_indexes() is True      # second run is a no-op, not an error


def test_health_ok(mock_client):
    assert mock_client.get("/health").json() == {"status": "ok"}


class _BrokenCollection:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise ServerSelectionTimeoutError("no servers")
        return fail


class _BrokenDb:
    employees = attendance_logs = _BrokenCollection()


class _BrokenClient:
    class admin:
        @staticmethod
        def command(*a, **k):
            raise ServerSelectionTimeoutError("no servers")

    @staticmethod
    def close():
        pass


def test_database_failures_are_503_not_hidden(mock_client):
    main.client, main.db = _BrokenClient(), _BrokenDb()
    assert mock_client.get("/health").status_code == 503
    assert mock_client.get("/employees").status_code == 503
    assert mock_client.post("/attendance/punch-in", json={"emp_code": "EMP0001"}).status_code == 503
