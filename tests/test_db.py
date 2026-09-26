from datetime import date

import pytest

from tracker import db

TODAY = date(2026, 9, 26)  # a Saturday


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    with db.connect(path) as c:
        yield c


def add(conn, title="Engineer", company="Acme", applied_on="2026-09-01", status="applied", **extra):
    return db.add_application(conn, {"title": title, "company": company, "applied_on": applied_on, **extra},
                              status=status, today=TODAY)


def test_add_records_initial_status(conn):
    app_id = add(conn)
    assert db.get_application(conn, app_id)["status"] == "applied"
    assert [tuple(r) for r in db.status_history(conn, app_id)] == [("applied", "2026-09-01")]


def test_applied_date_defaults_to_today_except_for_saved(conn):
    applied = db.add_application(conn, {"title": "A", "company": "B"}, today=TODAY)
    saved = db.add_application(conn, {"title": "C", "company": "D"}, status="saved", today=TODAY)
    assert db.get_application(conn, applied)["applied_on"] == "2026-09-26"
    assert db.get_application(conn, saved)["applied_on"] is None


def test_moving_saved_to_applied_sets_applied_date(conn):
    app_id = add(conn, status="saved", applied_on="")
    db.set_status(conn, app_id, "applied", on=date(2026, 9, 10))
    assert db.get_application(conn, app_id)["applied_on"] == "2026-09-10"


@pytest.mark.parametrize("fields, message", [
    ({"title": "", "company": "Acme"}, "required"),
    ({"title": "X", "company": "Acme", "url": "javascript:alert(1)"}, "http"),
    ({"title": "X", "company": "Acme", "applied_on": "9/5/2026"}, "date"),
])
def test_validation(conn, fields, message):
    with pytest.raises(ValueError, match=message):
        db.add_application(conn, fields, today=TODAY)


def test_status_changes_are_recorded_and_repeats_ignored(conn):
    app_id = add(conn)
    db.set_status(conn, app_id, "interview", on=date(2026, 9, 8))
    db.set_status(conn, app_id, "interview", on=date(2026, 9, 9))
    db.set_status(conn, app_id, "offer", on=date(2026, 9, 20))
    assert [r["status"] for r in db.status_history(conn, app_id)] == ["applied", "interview", "offer"]
    with pytest.raises(ValueError):
        db.set_status(conn, app_id, "hired")


def test_list_filter_search_and_sort(conn):
    add(conn, "Data Analyst", "Beta", "2026-09-03")
    add(conn, "QA Engineer", "alpha", "2026-09-05", location="Houston, TX")
    rejected = add(conn, "Programmer Analyst", "Gamma", "2026-09-04")
    db.set_status(conn, rejected, "rejected", on=TODAY)

    by_date = [r["title"] for r in db.list_applications(conn)]
    assert by_date == ["QA Engineer", "Programmer Analyst", "Data Analyst"]
    by_company = [r["company"] for r in db.list_applications(conn, sort="company", direction="asc")]
    assert by_company == ["alpha", "Beta", "Gamma"]  # case-insensitive
    assert [r["title"] for r in db.list_applications(conn, status="rejected")] == ["Programmer Analyst"]
    assert [r["title"] for r in db.list_applications(conn, q="houston")] == ["QA Engineer"]
    assert [r["title"] for r in db.list_applications(conn, q="analyst", status="applied")] == ["Data Analyst"]


def test_unknown_sort_key_cannot_inject_sql(conn):
    add(conn)
    rows = db.list_applications(conn, sort="id; DROP TABLE applications", direction="x")
    assert len(rows) == 1


def test_duplicates_by_link_or_company_title_date(conn):
    add(conn, url="https://example.com/job/1")
    assert db.find_duplicate(conn, url="https://example.com/job/1")
    assert db.find_duplicate(conn, company="ACME", title="engineer", applied_on="2026-09-01")
    assert not db.find_duplicate(conn, company="Acme", title="Engineer", applied_on="2026-09-02")
    with pytest.raises(Exception):
        add(conn, title="Other", url="https://example.com/job/1")  # UNIQUE(url)


def test_blank_links_are_not_duplicates(conn):
    add(conn, title="A", url="")
    add(conn, title="B", url="")
    assert len(db.list_applications(conn)) == 2


def test_follow_ups(conn):
    old = add(conn, "Old", applied_on="2026-09-01")         # 25 days, no reply
    add(conn, "Recent", applied_on="2026-09-20")            # 6 days
    replied = add(conn, "Replied", applied_on="2026-09-01")
    db.set_status(conn, replied, "interview", on=date(2026, 9, 5))
    rows = db.follow_ups(conn, today=TODAY)
    assert [(r["id"], r["days_waiting"]) for r in rows] == [(old, 25)]


def test_stats(conn):
    a = add(conn, "A", applied_on="2026-09-01")
    b = add(conn, "B", applied_on="2026-09-02")
    add(conn, "C", applied_on="2026-09-22")
    add(conn, "D", status="saved", applied_on="")
    db.set_status(conn, a, "interview", on=date(2026, 9, 5))    # 4 days
    db.set_status(conn, a, "rejected", on=date(2026, 9, 12))    # later response: ignored for "first"
    db.set_status(conn, b, "rejected", on=date(2026, 9, 12))    # 10 days

    s = db.stats(conn, today=TODAY, weeks=4)
    assert s["total"] == 4
    assert s["applied_total"] == 3
    assert s["responded"] == 2
    assert s["response_rate"] == pytest.approx(2 / 3)
    assert s["avg_days_to_response"] == pytest.approx(7.0)   # (4 + 10) / 2
    assert s["by_status"]["rejected"] == 2
    # Weeks start on Monday; the current week (Sep 21) holds C
    assert s["weekly"] == [
        {"week_start": "2026-08-31", "n": 2},
        {"week_start": "2026-09-07", "n": 0},
        {"week_start": "2026-09-14", "n": 0},
        {"week_start": "2026-09-21", "n": 1},
    ]


def test_deleting_removes_history(conn):
    app_id = add(conn)
    db.delete_application(conn, app_id)
    assert conn.execute("SELECT COUNT(*) FROM status_changes").fetchone()[0] == 0


def test_old_databases_are_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE applications (id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
            url TEXT UNIQUE, location TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '',
            employment_type TEXT NOT NULL DEFAULT '', date_posted TEXT, applied_on TEXT,
            status TEXT NOT NULL DEFAULT 'applied', source TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE status_changes (id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL,
            status TEXT NOT NULL, changed_on TEXT NOT NULL);
        INSERT INTO applications (title, company, applied_on) VALUES ('Engineer', 'Acme', '2026-09-01');
        INSERT INTO status_changes (application_id, status, changed_on) VALUES (1, 'applied', '2026-09-01');
    """)
    old.close()
    db.init(path)
    with db.connect(path) as c:
        db.set_status(c, 1, "interview", on=date(2026, 9, 4))
        assert db.stats(c, today=TODAY)["avg_days_to_response"] == 3
