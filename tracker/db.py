"""SQLite storage. All queries are hand-written SQL (no ORM) on purpose."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "tracker.db"

STATUSES = ["saved", "applied", "interview", "offer", "rejected", "withdrawn"]
# Statuses that mean the company answered (used for response rate)
RESPONSE_STATUSES = ("interview", "offer", "rejected")
FOLLOW_UP_DAYS = 14

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    url             TEXT UNIQUE,
    location        TEXT NOT NULL DEFAULT '',
    salary          TEXT NOT NULL DEFAULT '',
    employment_type TEXT NOT NULL DEFAULT '',
    date_posted     TEXT,
    applied_on      TEXT,
    status          TEXT NOT NULL DEFAULT 'applied'
                    CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    source          TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every status an application has been in, with the date it changed.
CREATE TABLE IF NOT EXISTS status_changes (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,
    changed_on     TEXT NOT NULL,
    -- 1 when the real date is unknown (e.g. a status imported from a spreadsheet)
    date_unknown   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_applied_on ON applications(applied_on);
CREATE INDEX IF NOT EXISTS idx_changes_app ON status_changes(application_id, changed_on);
"""

EDITABLE = ["title", "company", "url", "location", "salary", "employment_type", "date_posted", "applied_on", "source", "notes"]

# Whitelisted ORDER BY clauses, so sort keys from the URL never reach SQL directly
SORTS = {
    "applied": "applied_on IS NULL, applied_on {dir}, id {dir}",
    "company": "company COLLATE NOCASE {dir}, applied_on DESC",
    "title": "title COLLATE NOCASE {dir}, applied_on DESC",
    "status": "status {dir}, applied_on DESC",
}


def db_path() -> Path:
    return Path(os.environ.get("TRACKER_DB", DEFAULT_DB))


@contextmanager
def connect(path=None):
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init(path=None):
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    with connect(p) as conn:
        conn.executescript(SCHEMA)
        # Upgrade databases created before a column existed
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(status_changes)")}
        if "date_unknown" not in columns:
            conn.execute("ALTER TABLE status_changes ADD COLUMN date_unknown INTEGER NOT NULL DEFAULT 0")


def _clean(fields):
    out = {}
    for key in EDITABLE:
        if key in fields:
            value = fields[key]
            out[key] = value.strip() if isinstance(value, str) else (value or "")
    for key in ("url", "date_posted", "applied_on"):
        if key in out and not out[key]:
            out[key] = None  # NULL, so UNIQUE(url) allows many blank links
    if out.get("url") and not out["url"].lower().startswith(("http://", "https://")):
        raise ValueError("The link must start with http:// or https://")
    for key in ("date_posted", "applied_on"):
        if out.get(key):
            try:
                date.fromisoformat(out[key])
            except ValueError:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a date (YYYY-MM-DD)") from None
    return out


# ------------------------------------------------------------------ writes

def add_application(conn, fields, status="applied", today=None, default_applied_today=True):
    data = _clean(fields)
    if not data.get("title") or not data.get("company"):
        raise ValueError("Title and company are required")
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    if status != "saved" and not data.get("applied_on") and default_applied_today:
        data["applied_on"] = (today or date.today()).isoformat()
    cols = list(data) + ["status"]
    cur = conn.execute(
        f"INSERT INTO applications ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [*data.values(), status],
    )
    app_id = cur.lastrowid
    changed_on = data.get("applied_on") or (today or date.today()).isoformat()
    conn.execute(
        "INSERT INTO status_changes (application_id, status, changed_on) VALUES (?, ?, ?)",
        (app_id, status, changed_on),
    )
    return app_id


def update_application(conn, app_id, fields):
    data = _clean(fields)
    if ("title" in data and not data["title"]) or ("company" in data and not data["company"]):
        raise ValueError("Title and company are required")
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(
        f"UPDATE applications SET {sets}, updated_at = datetime('now') WHERE id = ?",
        [*data.values(), app_id],
    )


def set_status(conn, app_id, status, on=None, date_unknown=False):
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    row = conn.execute("SELECT status, applied_on FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise LookupError(app_id)
    if row["status"] == status:
        return
    on = (on or date.today()).isoformat()
    conn.execute(
        """UPDATE applications
              SET status = ?,
                  applied_on = CASE WHEN applied_on IS NULL AND ? != 'saved' THEN ? ELSE applied_on END,
                  updated_at = datetime('now')
            WHERE id = ?""",
        (status, status, on, app_id),
    )
    conn.execute(
        "INSERT INTO status_changes (application_id, status, changed_on, date_unknown) VALUES (?, ?, ?, ?)",
        (app_id, status, on, int(date_unknown)),
    )


def delete_application(conn, app_id):
    conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))


# ------------------------------------------------------------------ reads

def get_application(conn, app_id):
    return conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()


def find_duplicate(conn, url=None, company=None, title=None, applied_on=None):
    """Same link, or same company + title applied on the same day."""
    if url:
        row = conn.execute("SELECT * FROM applications WHERE url = ?", (url,)).fetchone()
        if row:
            return row
    if company and title:
        return conn.execute(
            """SELECT * FROM applications
                WHERE company = ? COLLATE NOCASE AND title = ? COLLATE NOCASE
                  AND applied_on IS ?""",
            (company, title, applied_on),
        ).fetchone()
    return None


def status_history(conn, app_id):
    return conn.execute(
        "SELECT status, changed_on FROM status_changes WHERE application_id = ? ORDER BY changed_on, id",
        (app_id,),
    ).fetchall()


def list_applications(conn, status=None, q=None, sort="applied", direction="desc"):
    where, params = [], []
    if status in STATUSES:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ? OR notes LIKE ?)")
        params += [f"%{q}%"] * 4
    order = SORTS.get(sort, SORTS["applied"]).format(dir="ASC" if direction == "asc" else "DESC")
    sql = "SELECT * FROM applications"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(f"{sql} ORDER BY {order}", params).fetchall()


def follow_ups(conn, today=None, days=FOLLOW_UP_DAYS):
    """Applications still at 'applied' with no update for `days` days."""
    today = (today or date.today()).isoformat()
    return conn.execute(
        """SELECT a.*, CAST(julianday(?) - julianday(last.changed_on) AS INTEGER) AS days_waiting
             FROM applications a
             JOIN (SELECT application_id, MAX(changed_on) AS changed_on
                     FROM status_changes GROUP BY application_id) last
               ON last.application_id = a.id
            WHERE a.status = 'applied'
              AND julianday(?) - julianday(last.changed_on) >= ?
            ORDER BY days_waiting DESC""",
        (today, today, days),
    ).fetchall()


def stats(conn, today=None, weeks=12):
    today = today or date.today()
    this_monday = date.fromordinal(today.toordinal() - today.weekday()).isoformat()
    by_status = {s: 0 for s in STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status"):
        by_status[row["status"]] = row["n"]

    responses = ", ".join(f"'{s}'" for s in RESPONSE_STATUSES)

    # For each application, the first status change after applying that means
    # the company answered. ROW_NUMBER picks the earliest one per application.
    first_response = conn.execute(
        f"""WITH ranked AS (
                SELECT sc.application_id,
                       sc.changed_on,
                       sc.date_unknown,
                       ROW_NUMBER() OVER (PARTITION BY sc.application_id
                                          ORDER BY sc.changed_on, sc.id) AS rn
                  FROM status_changes sc
                 WHERE sc.status IN ({responses})
            )
            SELECT COUNT(*) AS responded,
                   -- AVG skips NULLs: undated applications and imported replies with unknown dates
                   AVG(CASE WHEN r.date_unknown = 0
                            THEN julianday(r.changed_on) - julianday(a.applied_on) END) AS avg_days
              FROM ranked r
              JOIN applications a ON a.id = r.application_id
             WHERE r.rn = 1""",
    ).fetchone()

    applied_total = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status != 'saved'"
    ).fetchone()[0]

    # Applications per week for the last `weeks` weeks (weeks start on Monday)
    weekly = conn.execute(
        """WITH RECURSIVE week(start, n) AS (
                SELECT ?, 1
                UNION ALL
                SELECT date(start, '-7 days'), n + 1 FROM week WHERE n < ?
            )
            SELECT week.start AS week_start,
                   COUNT(a.id) AS n
              FROM week
              LEFT JOIN applications a
                ON a.applied_on >= week.start AND a.applied_on < date(week.start, '+7 days')
             GROUP BY week.start
             ORDER BY week.start""",
        (this_monday, weeks),
    ).fetchall()

    by_source = conn.execute(
        f"""SELECT COALESCE(NULLIF(source, ''), 'no link') AS source,
                   COUNT(*) AS n,
                   SUM(CASE WHEN id IN (SELECT application_id FROM status_changes
                                         WHERE status IN ({responses})) THEN 1 ELSE 0 END) AS responded
              FROM applications
             WHERE status != 'saved'
             GROUP BY 1
             ORDER BY n DESC""",
    ).fetchall()

    return {
        "total": sum(by_status.values()),
        "applied_total": applied_total,
        "by_status": by_status,
        "responded": first_response["responded"],
        "response_rate": first_response["responded"] / applied_total if applied_total else None,
        "avg_days_to_response": first_response["avg_days"],
        "weekly": [dict(r) for r in weekly],
        "by_source": [dict(r) for r in by_source],
    }
