"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB   (real environment variables win over a local .env)

Everything lives in this one file, in this order:
  1. configuration and database handles
  2. time / rounding helpers (rules R1-R5, R8)
  3. request models and validation helpers
  4. index creation (run at startup)
  5. aggregation-pipeline builders (shared by the analytics endpoints AND the explain endpoint)
  6. endpoints

Conventions: see DECISIONS.md. In short, instants are BSON datetimes (UTC, tz-aware) in MongoDB and epoch
milliseconds in the API; dates / months / shift times are plain strings and are interpreted as IST.
"""
import calendar
import datetime as dt
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # never overrides variables that are already set in the real environment

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")  # local-dev fallback only (no credentials)
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
# The client connects lazily, so importing this module never needs a running database.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

UTC = dt.timezone.utc
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENT_STATUSES = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60          # R2
OVERTIME_MIN_MINUTES = 30        # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out / correction window
MAX_TREND_DAYS = 92

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# An epoch-millisecond instant. strict=True rejects floats and strings; the lower bound rejects "seconds".
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

MONGO_UNAVAILABLE = "database unavailable"


# --------------------------------------------------------------------------- #
# 2. Helpers: time, rounding and the business rules R1-R5
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> dt.datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + dt.timedelta(seconds=ms // 1000)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Seeded data may come back naive (no tzinfo); MongoDB always stores UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def dt_to_ms(value: dt.datetime) -> int:
    return (ensure_utc(value) - EPOCH) // dt.timedelta(milliseconds=1)


def to_ms_or_none(value: Optional[dt.datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


def now_seconds() -> dt.datetime:
    """Server clock, truncated to whole seconds."""
    return dt.datetime.now(UTC).replace(microsecond=0)


def now_millis() -> dt.datetime:
    """Server clock at millisecond precision (MongoDB's BSON date precision)."""
    now = dt.datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def round_half_up(value: Any, places: int) -> float:
    """R8: half-up rounding done with Decimal. Python's round() is banker's/float based, Mongo's $round is
    banker's too, so rounding for reported numbers is always done here."""
    quantum = Decimal(1).scaleb(-places)
    number = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    return float(number.quantize(quantum, rounding=ROUND_HALF_UP))


def parse_hhmm(text: str) -> dt.time:
    hour, minute = text.split(":")
    return dt.time(int(hour), int(minute))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM strings compare correctly as text


def attendance_date(punch_in: dt.datetime, shift_start: str, shift_end: str) -> dt.date:
    """R1: the IST calendar date of the punch-in, except an overnight shift's early-morning punch-in, which
    belongs to the shift that started the previous day."""
    local = ensure_utc(punch_in).astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - dt.timedelta(days=1)
    return local.date()


def shift_window(day: dt.date, shift_start: str, shift_end: str) -> tuple[dt.datetime, dt.datetime]:
    """Start and end instants (IST) of the shift that belongs to attendance date `day`."""
    start = dt.datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end_day = day + dt.timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = dt.datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: dt.datetime, shift_start_at: dt.datetime) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    elapsed = int((ensure_utc(punch_in) - shift_start_at).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: dt.datetime, shift_end_at: dt.datetime) -> int:
    """R3: whole minutes after shift end, counted only when at least 30."""
    elapsed = int((ensure_utc(punch_out) - shift_end_at).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: dt.datetime, punch_out: dt.datetime) -> tuple[float, bool]:
    """R4 + R5: hours rounded half-up to 2 decimals (exact Decimal arithmetic), and the half-day flag, which is
    based on the ROUNDED value."""
    seconds = int((ensure_utc(punch_out) - ensure_utc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours), hours < HALF_DAY_LIMIT


def derive_fields(
    status: str,
    punch_in: Optional[dt.datetime],
    punch_out: Optional[dt.datetime],
    day: dt.date,
    shift_start: str,
    shift_end: str,
) -> dict:
    """All four derived fields for a record in its final state (used by punch-in, punch-out and PATCH)."""
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in NON_PRESENT_STATUSES or punch_in is None:
        return derived
    start_at, end_at = shift_window(day, shift_start, shift_end)
    derived["late_minutes"] = compute_late_minutes(punch_in, start_at)
    if punch_out is not None:
        derived["work_hours"], derived["half_day"] = compute_work_hours(punch_in, punch_out)
        derived["overtime_minutes"] = compute_overtime_minutes(punch_out, end_at)
    return derived


def count_weekdays(first: dt.date, last: dt.date) -> int:
    """Mon-Fri days in [first, last] (R7). Plain calendar arithmetic, no data involved."""
    days = (last - first).days + 1
    return sum(1 for i in range(max(days, 0)) if (first + dt.timedelta(days=i)).weekday() < 5)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    year, mon = int(month[:4]), int(month[5:])
    return dt.date(year, mon, 1), dt.date(year, mon, calendar.monthrange(year, mon)[1])


# --------------------------------------------------------------------------- #
# 3. Models, validation helpers, serialisation
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str) -> HTTPException:
    """A 422 whose body has the same shape as FastAPI's own validation errors."""
    return HTTPException(422, detail=[{"type": "value_error", "loc": loc, "msg": msg}])


def parse_date(text: str, loc: list) -> dt.date:
    """Strict YYYY-MM-DD that must be a real calendar date (2026-02-30 -> 422)."""
    if not re.fullmatch(DATE_PATTERN, text):
        raise unprocessable(loc, "must be a date in YYYY-MM-DD format")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise unprocessable(loc, "must be a real calendar date") from None


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError("must be a real calendar date") from None
        return value

    @model_validator(mode="after")
    def shifts_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# For the optional instants below, the default is only a marker for "not sent". Defaults are not validated by
# Pydantic, so an explicit JSON null still fails validation (the contract does not allow null here).
class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMs = Field(default=None)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMs = Field(default=None)


class RegularizeIn(BaseModel):
    status: Status = Field(default=None)
    punch_in: EpochMs = Field(default=None)
    punch_out: EpochMs = Field(default=None)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms_or_none(doc.get("created_at")),
    }


def _history_value(field: str, value: Any) -> Any:
    """Inside history[].changes only punch_in / punch_out hold datetimes; they become epoch ms."""
    if field in ("punch_in", "punch_out") and isinstance(value, dt.datetime):
        return dt_to_ms(value)
    return value


def serialize_record(doc: dict) -> dict:
    """Stored attendance document -> API shape. Missing legacy fields get their documented defaults."""
    history = []
    for entry in doc.get("history") or []:
        changes = {
            field: {"from": _history_value(field, v.get("from")), "to": _history_value(field, v.get("to"))}
            for field, v in (entry.get("changes") or {}).items()
        }
        history.append(
            {"at": dt_to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms_or_none(doc.get("punch_in")),
        "punch_out": to_ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
EMPLOYEE_INDEXES = [
    # Uniqueness of emp_code is enforced by the database (race-safe POST /employees); also serves lookups/$lookup.
    IndexModel([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code"),
    # department filter, department trend (department + joined_on <= day), department summary with a filter.
    IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on"),
    # department summary without a department filter: joined_on <= end of month.
    IndexModel([("joined_on", ASCENDING)], name="joined_on"),
]
ATTENDANCE_INDEXES = [
    # One record per (emp_code, date): makes punch-in race-safe and serves per-employee queries / $lookup.
    IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"),
    # GET /attendance default sort (date desc, emp_code asc), date ranges, month scans for the leaderboard.
    IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"),
    # GET /attendance?status=... (equality, then the same sort order).
    IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"),
]

_indexes_ready = False


def ensure_indexes() -> bool:
    """Create all indexes. Safe to call repeatedly. Returns False (and logs) instead of crashing the app."""
    global _indexes_ready
    try:
        db.employees.create_indexes(EMPLOYEE_INDEXES)
        db.attendance_logs.create_indexes(ATTENDANCE_INDEXES)
    except PyMongoError:
        log.exception("could not create indexes")
        return False
    _indexes_ready = True
    return True


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    """A database failure is reported as 503, never hidden and never turned into a success."""
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": MONGO_UNAVAILABLE})


# --------------------------------------------------------------------------- #
# 5. Aggregation pipeline builders (used by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
def _day_of_week(p: str) -> dict:
    """Mongo: 1 = Sunday ... 7 = Saturday, so Monday-Friday is 2..6. `date` is a plain string, parsed as UTC,
    which keeps the calendar day unchanged."""
    return {"$dayOfWeek": {"$dateFromString": {"dateString": f"{p}date", "onError": None, "onNull": None}}}


def _is_present(p: str) -> dict:
    return {"$in": [{"$ifNull": [f"{p}status", None]}, list(PRESENT_STATUSES)]}


def _present_value(p: str, weekdays_only: bool) -> dict:
    """1 for a present record, 0.5 for a present half day, 0 otherwise (R5, R6; R7 when weekdays_only)."""
    condition: dict = _is_present(p)
    if weekdays_only:
        condition = {"$and": [condition, {"$in": [_day_of_week(p), [2, 3, 4, 5, 6]]}]}
    half = {"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}
    return {"$cond": [condition, {"$cond": [half, 0.5, 1]}, 0]}


def _late_flag(p: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}, 1, 0]}


def _late_minutes(p: str) -> dict:
    return {"$ifNull": [f"{p}late_minutes", 0]}


def _has_work_hours(p: str) -> dict:
    numeric = {"$in": [{"$type": f"{p}work_hours"}, ["double", "int", "long", "decimal"]]}
    return {"$and": [_is_present(p), numeric]}


def date_range_filter(first: dt.date, last: dt.date) -> dict:
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[dt.date],
    date_to: Optional[dt.date],
    status: Optional[str],
) -> dict:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, last = month_bounds(month)
    p = "$"
    return [
        {"$match": {"emp_code": emp_code, "date": date_range_filter(first, last)}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value(p, weekdays_only=True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag(p)},
                "total_late_minutes": {"$sum": _late_minutes(p)},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    """Starts from `employees` so that people with ZERO logs are still counted (R9). Each employee gets their
    month's logs through $lookup; $unwind with preserveNullAndEmptyArrays keeps the log-less employees."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        match["department"] = department
    p = "$logs."
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": date_range_filter(first, last)}}],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # Level 1: one row per employee (so headcount is "number of employees", not "number of logs").
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "present_days": {"$sum": _present_value(p, weekdays_only=True)},
                "work_hours_sum": {"$sum": {"$cond": [_has_work_hours(p), f"{p}work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": [_has_work_hours(p), 1, 0]}},
                "late_count": {"$sum": _late_flag(p)},
                "total_late_minutes": {"$sum": _late_minutes(p)},
                "leave_count": {"$sum": {"$cond": [{"$eq": [f"{p}status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": [f"{p}status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        # Level 2: one row per department. Sums and counts are added up (NOT averages of averages).
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    first, last = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": date_range_filter(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist have an empty `emp` and are dropped here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def build_trend_pipeline(department: str, first: dt.date, last: dt.date, emp_codes: list[str]) -> list[dict]:
    """One output row per calendar day. The days are generated inside the database with $range (no Python loop),
    which also fills the gaps; $setWindowFields then adds the 7-row moving average."""
    days = (last - first).days + 1
    first_at = dt.datetime.combine(first, dt.time(), tzinfo=UTC)
    p = "$"
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joins": {"$push": "$joined_on"}}},
        # One uncorrelated lookup: per-day present / late numbers for the whole range.
        {
            "$lookup": {
                "from": "attendance_logs",
                "pipeline": [
                    {"$match": {"emp_code": {"$in": emp_codes}, "date": date_range_filter(first, last)}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {"$sum": _present_value(p, weekdays_only=False)},
                            "late_count": {"$sum": _late_flag(p)},
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {"$project": {"_id": 0, "joins": 1, "daily": 1, "offset": {"$range": [0, days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"at": {"$add": [first_at, {"$multiply": ["$offset", 86_400_000]}]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$at"}},
                "dow": {"$dayOfWeek": "$at"},
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
                "stats": {
                    "$arrayElemAt": [
                        {"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]}}},
                        0,
                    ]
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                "headcount": 1,
                "present_count": {"$ifNull": ["$stats.present_count", 0]},
                "late_count": {"$ifNull": ["$stats.late_count", 0]},
            }
        },
        {
            "$addFields": {
                "raw_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # $avg ignores nulls and returns null when the window has no number; documents [-6, 0] = this row + 6 before.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg": {"$avg": "$raw_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0}},
    ]


def prepare_trend(department: str, from_text: str, to_text: str) -> tuple[list[dict], dt.date, dt.date]:
    """Validation + pipeline for the trend endpoint (shared with explain so both behave identically)."""
    first = parse_date(from_text, ["query", "from"])
    last = parse_date(to_text, ["query", "to"])
    if last < first:
        raise unprocessable(["query", "to"], "'to' must not be before 'from'")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], f"range must not exceed {MAX_TREND_DAYS} days")
    codes = [d["emp_code"] for d in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
    if not codes:
        raise HTTPException(404, f"department {department} not found")
    return build_trend_pipeline(department, first, last, codes), first, last


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, MONGO_UNAVAILABLE) from None
    if not _indexes_ready:
        ensure_indexes()  # startup may have raced the database; try again, best effort
    return {"status": "ok"}


# ------------------------------- employees ---------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)  # counts the FILTERED set
    skip = (page - 1) * page_size
    items: list[dict] = []
    if skip < total:  # also avoids sending an absurd skip value to MongoDB
        cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ------------------------------- attendance --------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derive_fields(body.status, punched_at, None, day, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()

    # The employee's most recent record whose punch_in is at or before the punch-out instant.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_at = ensure_utc(record["punch_in"])
    elapsed = (punched_at - punch_in_at).total_seconds()
    if elapsed <= 0:
        raise unprocessable(["body", "punched_at"], "punched_at must be after the punch-in")
    if elapsed > MAX_SHIFT_SECONDS:
        raise unprocessable(["body", "punched_at"], "punch-out is more than 24 hours after the punch-in")

    day = dt.date.fromisoformat(record["date"])
    derived = derive_fields(record["status"], punch_in_at, punched_at, day, emp["shift_start"], emp["shift_end"])
    # Compare-and-set: only close the record if it is STILL open (punch_out null) and unchanged. Of two simultaneous
    # punch-outs exactly one matches this filter.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or modified concurrently")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    first = parse_date(date_from, ["query", "date_from"]) if date_from else None
    last = parse_date(date_to, ["query", "date_to"]) if date_to else None
    if first and last and first > last:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")

    query = build_attendance_query(emp_code, first, last, status)
    collection = db.attendance_logs
    total = collection.count_documents(query) if query else collection.estimated_document_count()
    skip = (page - 1) * page_size
    items: list[dict] = []
    if skip < total:
        cursor = collection.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [serialize_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, ["path", "date"])
    emp = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if record is None:
        raise HTTPException(404, f"no attendance record for {emp_code} on {day.isoformat()}")

    sent = body.model_fields_set
    cur_status = record["status"]
    cur_in = ensure_utc(record["punch_in"]) if record.get("punch_in") is not None else None
    cur_out = ensure_utc(record["punch_out"]) if record.get("punch_out") is not None else None

    # ---- the final state of the three fields a client may change ----
    new_status = body.status or cur_status
    if new_status in NON_PRESENT_STATUSES:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable(["body"], f"status {new_status} clears the punch times; do not send them")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else cur_in
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else cur_out
        if new_in is None:
            raise unprocessable(["body", "punch_in"], f"status {new_status} requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > MAX_SHIFT_SECONDS:
                raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

    if (new_status, new_in, new_out) == (cur_status, cur_in, cur_out):
        raise unprocessable(["body"], "the request changes nothing")

    # ---- recompute derived fields and work out what actually changed ----
    derived = derive_fields(new_status, new_in, new_out, day, emp["shift_start"], emp["shift_end"])
    old = {
        "status": cur_status,
        "punch_in": cur_in,
        "punch_out": cur_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {field: {"from": old[field], "to": new[field]} for field in old if old[field] != new[field]}
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- optimistic concurrency ----
    # The update only applies if the record is exactly what we read (same status/punches and the same number of
    # history entries). If another correction (or a punch-out) got in first, nothing is written and we answer 409,
    # so history entries can never be lost or overwritten.
    history_len = len(record.get("history") or [])
    history_guard = (
        {"$or": [{"history": None}, {"history": {"$size": 0}}]} if history_len == 0 else {"history": {"$size": history_len}}
    )
    guard = {
        "emp_code": emp_code,
        "date": day.isoformat(),
        "status": cur_status,
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        **history_guard,
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload it and try again")
    return serialize_record(updated)


# ------------------------------- analytics ---------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, dt.date.fromisoformat(emp["joined_on"])), last)  # R7

    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {}
    present = Decimal(repr(float(row.get("present_days", 0))))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(present / Decimal(working_days) * 100, 2) if working_days else None,
    }


def _mean_half_up(total: float, count: int) -> Optional[float]:
    if not count:
        return None
    exact_total = Decimal(repr(round(float(total), 6)))  # strips binary-float noise from the sum
    return round_half_up(exact_total / Decimal(count), 2)


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_PATTERN), department: Optional[str] = None):
    rows = db.employees.aggregate(build_department_summary_pipeline(month, department))
    items = [
        {
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": _mean_half_up(r["work_hours_sum"], int(r["work_hours_count"])),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=DATE_PATTERN),
    to: str = Query(pattern=DATE_PATTERN),
):
    pipeline, _, _ = prepare_trend(department, from_, to)
    rows = db.employees.aggregate(pipeline)
    items = [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if r.get("raw_rate") is None else round_half_up(r["raw_rate"], 4),
            "moving_avg_7d": None if r.get("moving_avg") is None else round_half_up(r["moving_avg"], 4),
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------- admin ------------------------------------ #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(name: str, value: Any) -> Any:
    if value is None or value == "":
        raise unprocessable(["query", name], "field required")
    return value


def _require_month(value: Optional[str]) -> str:
    month = _require("month", value)
    if not re.fullmatch(MONTH_PATTERN, month):
        raise unprocessable(["query", "month"], "month must be YYYY-MM")
    return month


def _explain_json(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the endpoint's main query, built by the SAME builder functions the endpoint uses."""
    if endpoint == "attendance_list":
        first = parse_date(date_from, ["query", "date_from"]) if date_from else None
        last = parse_date(date_to, ["query", "date_to"]) if date_to else None
        if first and last and first > last:
            raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
        command = {
            "find": "attendance_logs",
            "filter": build_attendance_query(emp_code, first, last, status),
            "projection": {"_id": 0},
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        pipeline = build_monthly_pipeline(_require("emp_code", emp_code), _require_month(month))
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_summary":
        pipeline = build_department_summary_pipeline(_require_month(month), department)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "late_leaderboard":
        pipeline = build_leaderboard_pipeline(_require_month(month), limit, department)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:  # department_trend
        pipeline, _, _ = prepare_trend(_require("department", department), _require("from", from_), _require("to", to))
        collection = "employees"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain_json(command)}
