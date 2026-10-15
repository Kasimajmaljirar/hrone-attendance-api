# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicates, joins), `(department, joined_on)` (department filter, trend headcount), `(joined_on)` (summary without a filter). `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee queries), `(date desc, emp_code)` (list sort, month scans), `(status, date desc, emp_code)` (status filter). I rejected an index on `punch_in`: punch-out only looks at one employee's few records, already narrowed by `emp_code`.

2. **Punch-in race.** Both requests find the employee and compute the same attendance date, then call `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed; the other raises `DuplicateKeyError`, which I return as 409. There is no check before the insert.

3. **Ties.** The database ranks with `$rank`, so tied employees share a rank (1, 2, 2, 4). `limit` is applied after ranking, so a tie at the cutoff returns every tied employee, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees` (joined on or before month end), then `$lookup`s their logs and `$unwind`s with `preserveNullAndEmptyArrays`. Someone with no logs survives as one row and is counted.

5. **100x data.** I would stop aggregating raw logs per request and keep pre-computed monthly totals per employee, updated on each punch or correction.
