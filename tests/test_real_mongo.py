"""REAL MongoDB integration tests (MongoDB 6.0+). SKIPPED unless MONGO_TEST_URI is set, e.g.

    docker run -d -p 27017:27017 mongo:7
    MONGO_TEST_URI=mongodb://localhost:27017 pytest tests/test_real_mongo.py -v

They use a throw-away database (name below) which is DROPPED at the end, never your own data. They check what
mongomock cannot: the aggregation pipelines ($lookup, $setWindowFields, $range, ...), the explain plans, and
the unique-index race protection against a real server.
"""
import datetime as dt
import os
import random
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.real_mongo
URI = os.getenv("MONGO_TEST_URI")
if not URI:
    pytest.skip("MONGO_TEST_URI not set: real-MongoDB tests skipped", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402

import dataset  # noqa: E402
from app import main  # noqa: E402
from conftest import H, IN_1005, make_employee  # noqa: E402

TEST_DB = "hrone_attendance_pytest"


@pytest.fixture(scope="module")
def real():
    real_client = MongoClient(URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    real_client.drop_database(TEST_DB)
    main.client, main.db = real_client, real_client[TEST_DB]
    main._indexes_ready = False
    dataset.seed(main.db)
    with TestClient(main.app) as test_client:        # startup creates the indexes on the seeded data
        yield test_client
    real_client.drop_database(TEST_DB)


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------- analytics ---------------------------------- #
@pytest.mark.parametrize("code", [e["emp_code"] for e in dataset.EMPLOYEES])
def test_employee_monthly_matches_oracle(real, code):
    assert get(real, f"/analytics/employees/{code}/monthly", month="2026-07") == dataset.expected_monthly(code, "2026-07")


def test_employee_monthly_edges(real):
    assert get(real, "/analytics/employees/EMP0005/monthly", month="2026-07")["attendance_pct"] is None   # not joined yet
    assert get(real, "/analytics/employees/EMP0004/monthly", month="2026-07")["present_days"] == 0.0     # no logs
    assert real.get("/analytics/employees/EMP7777/monthly", params={"month": "2026-07"}).status_code == 404
    assert real.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    assert real.get("/analytics/employees/EMP0001/monthly").status_code == 422


@pytest.mark.parametrize("month,dept", [("2026-07", None), ("2026-07", "Sales"), ("2026-08", None), ("2026-06", None)])
def test_department_summary_matches_oracle(real, month, dept):
    params = {"month": month, **({"department": dept} if dept else {})}
    assert get(real, "/analytics/departments/summary", **params) == dataset.expected_summary(month, dept)


def test_department_summary_counts_employees_without_logs(real):
    eng = get(real, "/analytics/departments/summary", month="2026-07")["items"][0]
    assert eng["department"] == "Engineering" and eng["headcount"] == 4     # EMP0004 has no logs; EMP0005 joins in August


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 10])
def test_leaderboard_ties_and_limit_after_ranking(real, limit):
    assert get(real, "/analytics/leaderboard/late", month="2026-07", limit=limit) == dataset.expected_leaderboard("2026-07", limit)


def test_leaderboard_specifics(real):
    items = get(real, "/analytics/leaderboard/late", month="2026-07", limit=1)["items"]
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0001"), (1, "EMP0002")]   # tie at the cut-off kept
    ranks = [i["rank"] for i in get(real, "/analytics/leaderboard/late", month="2026-07")["items"]]
    assert ranks == [1, 1, 3, 4, 4]                                                         # 1,1,3 not 1,1,2
    assert get(real, "/analytics/leaderboard/late", month="2026-07", department="Sales") == dataset.expected_leaderboard("2026-07", 10, "Sales")
    assert get(real, "/analytics/leaderboard/late", month="2026-05")["items"] == []         # empty month
    assert real.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


@pytest.mark.parametrize("dept,first,last", [
    ("Engineering", "2026-07-01", "2026-07-31"), ("Engineering", "2026-06-29", "2026-07-20"),
    ("Operations", "2026-07-01", "2026-07-12"), ("Sales", "2026-07-04", "2026-07-05"),
    ("Engineering", "2026-07-15", "2026-07-15"), ("Sales", "2026-01-01", "2026-03-31"),
])
def test_trend_matches_oracle(real, dept, first, last):
    got = get(real, f"/analytics/departments/{dept}/trend", **{"from": first, "to": last})
    assert got == dataset.expected_trend(dept, first, last)
    assert len(got["items"]) == (dt.date.fromisoformat(last) - dt.date.fromisoformat(first)).days + 1   # one row per day


def test_trend_errors(real):
    base = "/analytics/departments"
    assert real.get(f"{base}/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert real.get(f"{base}/Sales/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert real.get(f"{base}/Sales/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422   # 93 days
    assert real.get(f"{base}/Sales/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200   # 92 days
    assert real.get(f"{base}/Sales/trend", params={"from": "2026-07-01"}).status_code == 422


# --------------------------------- explain ----------------------------------- #
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001"}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT", "page": 2, "page_size": 5}),
    ("attendance_list", {"status": "WFH"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 5}),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
]


@pytest.mark.parametrize("endpoint,params", EXPLAIN_CASES)
def test_explain_uses_an_index_and_never_a_collection_scan(real, endpoint, params):
    body = get(real, f"/admin/explain/{endpoint}", **params)
    text = str(body["explain"])
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    assert "IXSCAN" in text, text[:2000]
    assert "COLLSCAN" not in text, text[:2000]


def test_explain_missing_parameters_are_422(real):
    assert real.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert real.get("/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422
    assert real.get("/admin/explain/late_leaderboard").status_code == 422
    assert real.get("/admin/explain/nonsense").status_code == 422


# ------------------------------ scale and races ------------------------------ #
def test_scale_100k_records_uses_indexes_and_stays_fast(real):
    """Loads ~100k generated records and checks that listing and the analytics still return quickly with IXSCAN."""
    import time

    rnd = random.Random(7)
    employees = [dataset.emp(f"EMP{n:05d}", rnd.choice(["Engineering", "Sales", "Ops", "HR"])) for n in range(10_000, 10_500)]
    main.db.employees.insert_many([dict(e) for e in employees])
    docs, start = [], dt.date(2026, 1, 1)
    for e in employees:
        for offset in range(200):
            day = start + dt.timedelta(days=offset)
            late = rnd.choice([0, 0, 0, 15, 40])
            docs.append(dataset.log(e["emp_code"], day.isoformat(), status=rnd.choice(["PRESENT", "PRESENT", "WFH", "LEAVE", "ABSENT"]),
                                    wh=round(rnd.uniform(3, 10), 2), late=late))
    main.db.attendance_logs.insert_many(docs)           # 500 x 200 = 100,000 records
    timings = {}
    for name, path, params in [
        ("list", "/attendance", {"date_from": "2026-03-01", "date_to": "2026-03-31", "page_size": 100}),
        ("list-status", "/attendance", {"status": "WFH", "page": 3}),
        ("monthly", "/analytics/employees/EMP10010/monthly", {"month": "2026-03"}),
        ("summary", "/analytics/departments/summary", {"month": "2026-03"}),
        ("leaderboard", "/analytics/leaderboard/late", {"month": "2026-03", "limit": 10}),
        ("trend", "/analytics/departments/Engineering/trend", {"from": "2026-01-01", "to": "2026-03-31"}),
    ]:
        t0 = time.perf_counter()
        assert real.get(path, params=params).status_code == 200
        timings[name] = time.perf_counter() - t0
    print({k: round(v, 3) for k, v in timings.items()})
    assert max(timings.values()) < 3.0, timings
    for endpoint, params in EXPLAIN_CASES:
        text = str(get(real, f"/admin/explain/{endpoint}", **params)["explain"])
        assert "IXSCAN" in text and "COLLSCAN" not in text, endpoint
    main.db.attendance_logs.delete_many({"emp_code": {"$gte": "EMP10000"}})
    main.db.employees.delete_many({"emp_code": {"$gte": "EMP10000"}})


def test_real_unique_index_makes_punch_in_race_safe(real):
    make_employee(real, "EMP0777")
    body = {"emp_code": "EMP0777", "punched_at": IN_1005}
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: real.post("/attendance/punch-in", json=body).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15


def test_real_single_winner_for_punch_out_and_no_lost_history(real):
    body = {"emp_code": "EMP0777", "punched_at": IN_1005 + 9 * H}
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: real.post("/attendance/punch-out", json=body).status_code, range(16)))
    assert sorted(codes) == [200] + [409] * 15

    def correct(i):
        return real.patch("/attendance/EMP0777/2026-07-06", json={"reason": f"fix {i:03d}", "regularized_by": "hr",
                                                                  "punch_in": IN_1005 - 60_000 * (i + 1)}).status_code
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(correct, range(16)))
    history = get(real, "/attendance", emp_code="EMP0777")["items"][0]["history"]
    assert set(codes) <= {200, 409} and len(history) == codes.count(200) >= 1
