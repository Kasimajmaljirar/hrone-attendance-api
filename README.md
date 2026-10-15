# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and analytics built on
aggregation pipelines. All application code is in `app/main.py`.

## Run it
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# MongoDB 6.0+ (local Docker, or a free Atlas M0 cluster)
docker run -d --name mongo -p 27017:27017 mongo:7

export MONGO_URI="mongodb://localhost:27017"            # or put both lines in a local .env (git-ignored)
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Open http://localhost:8000/docs for the interactive API. Indexes are created automatically at startup.
`MONGO_URI` / `MONGO_DB` come from real environment variables first; `.env` is only a fallback. If `MONGO_URI` is not
set at all the app falls back to `mongodb://localhost:27017` (no credentials) for local development.

## Endpoints
| Method | Path | Notes |
|---|---|---|
| GET | `/health` | 200 only if MongoDB answers a ping, else 503 |
| POST / GET | `/employees` | 201 / 409 duplicate / 422; list is filtered by `department`, sorted by `emp_code`, paginated |
| POST | `/attendance/punch-in` | 201; 404 unknown employee; 409 already punched in that date; 422 |
| POST | `/attendance/punch-out` | 200; 404 employee / no punch-in; 409 already closed; 422 not after punch-in or > 24 h |
| GET | `/attendance` | filters `emp_code`, `date_from`, `date_to`, `status`; sorted date desc, emp_code asc |
| PATCH | `/attendance/{emp_code}/{date}` | manual correction; recomputes derived fields; appends one `history` entry; 409 on a concurrent edit |
| GET | `/analytics/employees/{emp_code}/monthly` | `month=YYYY-MM` |
| GET | `/analytics/departments/summary` | `month`, optional `department` |
| GET | `/analytics/leaderboard/late` | `month`, `limit` (1-50, default 10), optional `department` |
| GET | `/analytics/departments/{department}/trend` | `from`, `to` (max 92 days) |
| GET | `/admin/explain/{endpoint}` | `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend` |

The contract is `openapi.yaml` from the assignment kit; the generated routes match it one to one.

## Conventions and assumptions
* **Time.** Instants are BSON datetimes (UTC, tz-aware) in MongoDB and integer epoch **milliseconds** in the API
  (seconds-looking values, floats, strings, `null` -> 422). Dates, months and shift times are strings interpreted as
  **IST (UTC+05:30)**. Instants are truncated to whole seconds before any calculation (R1).
* **Rounding.** Reported numbers use Decimal half-up (2 decimals, 4 for rates). MongoDB's `$round` is banker's rounding, so
  pipelines return raw sums/ratios and the final rounding happens in Python.
* **Derived fields** (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are stored when punching/correcting;
  analytics read the stored values and never recompute them.
* **Audit trail.** `history` is appended only by `PATCH`, with `from`/`to` for each field that actually changed. A
  correction is applied only if the record is unchanged since it was read (same status/punches and history length),
  otherwise 409, so history entries cannot be lost or overwritten.
* **Assumptions where the contract is silent:** (a) the 92-day trend limit counts calendar days *inclusive*
  (a 92-day range is allowed, 93 is 422); (b) `moving_avg_7d` averages the *unrounded* daily rates and is rounded once;
  (c) `avg_work_hours` includes weekend records, because only `present_days` is restricted to Mon-Fri;
  (d) a record with a punch-in but no punch-out yet counts as a full present day (it is not a half day until punch-out);
  (e) punch-out targets the latest record with `punch_in <= punched_at`; if that record is already closed the answer is
  409 (an older open record is not used); (f) if the explained trend/summary needs a department with no employees,
  explain answers 404 like the real endpoint.

## Tests
```bash
pip install -r requirements-dev.txt
pytest                                   # mocked tests: pure rules + API on mongomock (no MongoDB needed)

# Real MongoDB tests (pipelines, explain plans, 100k-record check, race tests). Uses a throw-away database
# named hrone_attendance_pytest which is dropped afterwards.
MONGO_TEST_URI=mongodb://localhost:27017 pytest tests/test_real_mongo.py -v -s
```
* `tests/test_rules.py` and `tests/test_api_mocked.py` are **mocked**: `mongomock` imitates MongoDB in memory.
* `tests/test_real_mongo.py` is **real**: it needs a MongoDB server and is skipped without `MONGO_TEST_URI`.
* `tests/dataset.py` is a hand-built dataset (ties, weekend records, no-log employee, mid-month joiner, overnight shift,
  missing weekday, legacy record, orphan log) with an independent pure-Python oracle for the expected analytics.

## Test status / known limitations
* Mocked tests: **83 passed** (run on Python 3.12, FastAPI 0.143, Pydantic 2.14, PyMongo 4.18, mongomock 4.3).
* The real-MongoDB tests (`test_real_mongo.py`) were written but **not yet executed against a real MongoDB** in the
  environment where this code was written (no MongoDB server was available there). The analytics pipelines were only
  checked with a small hand-written interpreter, which is not a substitute. *Update this section after running them.*
* `mongomock` cannot run `$setWindowFields`, `$range` or explain, so the leaderboard, trend and explain endpoints have no
  mocked test.
* Concurrency tests on mongomock only show the logic is sound (unique index + compare-and-set); the real guarantee
  comes from MongoDB and is exercised by the real tests.
* `history: null` (instead of missing/empty) on a seeded record is not handled by `PATCH`.
* No authentication: `regularized_by` is free text, as the assignment specifies.
