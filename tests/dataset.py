"""Hand-built July-2026 dataset with the awkward cases from the spec, plus an independent pure-Python 'oracle'
that computes the expected analytics WITHOUT any MongoDB (so the pipelines can be checked against it).

July 1 2026 is a Wednesday.  Stored derived values (late/overtime/work_hours/half_day) are written explicitly,
the way the grader's seeded data is: analytics must read them, not recompute them.
"""
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

UTC = dt.timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


def emp(code, dept, joined="2026-01-05", start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code[-4:]}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": dt.datetime(2026, 1, 5, 4, 30, tzinfo=UTC)}


EMPLOYEES = [
    emp("EMP0001", "Engineering"),
    emp("EMP0002", "Engineering"),
    emp("EMP0003", "Engineering", joined="2026-07-15"),      # mid-month joiner
    emp("EMP0004", "Engineering"),                           # no logs at all
    emp("EMP0005", "Engineering", joined="2026-08-03"),      # joins after the month: not in July headcount
    emp("EMP0006", "Operations", start="22:00", end="06:00"),  # overnight shift
    emp("EMP0007", "Operations"),
    emp("EMP0008", "Sales"),
    emp("EMP0009", "Sales"),
]


def log(code, date, status="PRESENT", wh=9.0, late=0, ot=0, half=False, legacy=False):
    present = status in PRESENCE
    day = dt.date.fromisoformat(date)
    pin = dt.datetime.combine(day, dt.time(4, 0), tzinfo=UTC) if present else None
    pout = pin + dt.timedelta(hours=wh) if present and wh is not None else None
    doc = {"emp_code": code, "date": date, "status": status, "punch_in": pin, "punch_out": pout,
           "work_hours": wh if present else None, "late_minutes": late, "overtime_minutes": ot,
           "half_day": half, "history": []}
    if legacy:
        del doc["half_day"], doc["history"]
    return doc


def weekdays(first, last):
    d = dt.date.fromisoformat(first)
    end = dt.date.fromisoformat(last)
    while d <= end:
        if d.weekday() < 5:
            yield d.isoformat()
        d += dt.timedelta(days=1)


def series(code, first, last, overrides=None, **defaults):
    overrides = overrides or {}
    out = []
    for date in weekdays(first, last):
        o = overrides.get(date, {})
        if o is None:          # a weekday with NO record at all
            continue
        out.append(log(code, date, **{**defaults, **o}))
    return out


LOGS = (
    series("EMP0001", "2026-07-01", "2026-07-31", {
        "2026-07-02": {"late": 35},
        "2026-07-03": {"status": "WFH", "wh": 4.0, "half": True},
        "2026-07-06": {"status": "ON_DUTY", "wh": 8.5, "late": 12},
        "2026-07-07": {"status": "LEAVE"},
        "2026-07-08": None,
    }) + [
        log("EMP0001", "2026-07-11", wh=6.0, late=15, ot=45),   # Saturday record: late/overtime count, present_days no
        log("EMP0001", "2026-07-12", status="ABSENT"),          # Sunday ABSENT record
    ]
    + series("EMP0002", "2026-07-01", "2026-07-14", {"2026-07-02": {"late": 31}, "2026-07-09": {"late": 31, "ot": 30}})
    + series("EMP0003", "2026-07-15", "2026-07-31", {"2026-07-20": {"status": "LEAVE"}})
    + series("EMP0006", "2026-07-01", "2026-07-10", {"2026-07-02": {"late": 50, "ot": 40}}, wh=8.5)
    + [log("EMP0006", "2026-07-04", wh=8.75, ot=40)]            # Saturday overnight shift
    + series("EMP0007", "2026-07-01", "2026-07-10", {"2026-07-02": {"status": "LEAVE"}, "2026-07-03": {"status": "LEAVE"},
                                                      "2026-07-06": {"status": "ON_DUTY"}})
    + [log("EMP0007", "2026-07-13", wh=9.12, legacy=True)]      # legacy record: no half_day, no history
    + series("EMP0008", "2026-07-01", "2026-07-03", {"2026-07-01": {"late": 40}})
    + series("EMP0009", "2026-07-01", "2026-07-03", {"2026-07-02": {"late": 40}})
    + [log("EMP9999", "2026-07-02", late=500)]                 # log whose employee does not exist
)


def seed(db):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([dict(e) for e in EMPLOYEES])
    db.attendance_logs.insert_many([dict(d) for d in LOGS])


# ----------------------------- the oracle ----------------------------------- #
def r2(x):
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), ROUND_HALF_UP))


def r4(x):
    return float(Decimal(repr(float(x))).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def month_days(month):
    y, m = int(month[:4]), int(month[5:])
    d = dt.date(y, m, 1)
    while d.month == m:
        yield d
        d += dt.timedelta(days=1)


def _present_value(rec):
    if rec["status"] not in PRESENCE:
        return 0
    return 0.5 if rec.get("half_day", False) else 1


def _weekday(date_text):
    return dt.date.fromisoformat(date_text).weekday() < 5


def expected_monthly(code, month):
    e = next(x for x in EMPLOYEES if x["emp_code"] == code)
    days = list(month_days(month))
    working = sum(1 for d in days if d.weekday() < 5 and d.isoformat() >= e["joined_on"])
    recs = [r for r in LOGS if r["emp_code"] == code and r["date"].startswith(month)]
    present = sum(_present_value(r) for r in recs if _weekday(r["date"]))
    return {"emp_code": code, "month": month, "working_days": working, "present_days": r2(present),
            "leave_days": sum(1 for r in recs if r["status"] == "LEAVE"),
            "late_count": sum(1 for r in recs if r["late_minutes"] > 0),
            "total_late_minutes": sum(r["late_minutes"] for r in recs),
            "total_overtime_minutes": sum(r["overtime_minutes"] for r in recs),
            "attendance_pct": r2(present / working * 100) if working else None}


def expected_summary(month, department=None):
    last = list(month_days(month))[-1].isoformat()
    out = {}
    for e in EMPLOYEES:
        if e["joined_on"] > last or (department and e["department"] != department):
            continue
        g = out.setdefault(e["department"], {"headcount": 0, "present": 0, "whs": [], "late_count": 0, "late_min": 0,
                                             "leave": 0, "on_duty": 0})
        g["headcount"] += 1
        for r in LOGS:
            if r["emp_code"] != e["emp_code"] or not r["date"].startswith(month):
                continue
            if _weekday(r["date"]):
                g["present"] += _present_value(r)
            if r["status"] in PRESENCE and r["work_hours"] is not None:
                g["whs"].append(r["work_hours"])
            g["late_count"] += 1 if r["late_minutes"] > 0 else 0
            g["late_min"] += r["late_minutes"]
            g["leave"] += r["status"] == "LEAVE"
            g["on_duty"] += r["status"] == "ON_DUTY"
    items = []
    for name in sorted(out):
        g = out[name]
        avg = r2(sum(Decimal(repr(w)) for w in g["whs"]) / len(g["whs"])) if g["whs"] else None
        items.append({"department": name, "headcount": g["headcount"], "present_days": r2(g["present"]),
                      "avg_work_hours": avg, "late_count": g["late_count"], "total_late_minutes": g["late_min"],
                      "leave_count": g["leave"], "on_duty_count": g["on_duty"]})
    return {"month": month, "items": items}


def expected_leaderboard(month, limit=10, department=None):
    emps = {e["emp_code"]: e for e in EMPLOYEES}
    totals = {}
    for r in LOGS:
        if r["date"].startswith(month) and r["late_minutes"] > 0 and r["emp_code"] in emps:
            if department and emps[r["emp_code"]]["department"] != department:
                continue
            t = totals.setdefault(r["emp_code"], [0, 0])
            t[0] += r["late_minutes"]
            t[1] += 1
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for code, (minutes, count) in ordered:
        rank = 1 + sum(1 for _, (m, _c) in ordered if m > minutes)   # competition ranking
        if rank <= limit:
            items.append({"rank": rank, "emp_code": code, "name": emps[code]["name"],
                          "department": emps[code]["department"], "total_late_minutes": minutes, "late_count": count})
    return {"month": month, "items": items}


def expected_trend(department, first, last):
    codes = {e["emp_code"] for e in EMPLOYEES if e["department"] == department}
    d, end = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    rows, rates = [], []
    while d <= end:
        iso = d.isoformat()
        working = d.weekday() < 5
        headcount = sum(1 for e in EMPLOYEES if e["department"] == department and e["joined_on"] <= iso)
        todays = [r for r in LOGS if r["emp_code"] in codes and r["date"] == iso]
        present = sum(_present_value(r) for r in todays)
        rate = present / headcount if working and headcount > 0 else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({"date": iso, "is_working_day": working, "headcount": headcount, "present_count": r2(present),
                     "late_count": sum(1 for r in todays if r["late_minutes"] > 0),
                     "attendance_rate": None if rate is None else r4(rate),
                     "moving_avg_7d": r4(sum(window) / len(window)) if window else None})
        d += dt.timedelta(days=1)
    return {"department": department, "items": rows}
