import csv
import io
from datetime import date

import pytest

from tracker import db, importer

TODAY = date(2026, 9, 26)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    with db.connect(path) as c:
        yield c


SHEET = """﻿Company Name,Job Title,Date Applied,Link,Status,Notes
KBR,Software Engineer I,9/2/2026,https://careers.kbr.com/job/1,Applied,
Capgemini,Programmer Analyst,2026-09-03,,Phone screen scheduled,recruiter: Sam
Acme,QA Analyst,next week,,,
AspenTech,Data Analyst,9/10/26,https://example.com/j,rejected,
,Missing company,9/11/2026,,,
,,,,,
"""


@pytest.mark.parametrize("headers, expected", [
    (["Job Title", "Company", "Date"], {"title": 0, "company": 1, "applied_on": 2}),
    (["Position", "Employer", "Applied On", "URL"], {"title": 0, "company": 1, "applied_on": 2, "url": 3}),
    (["Role", "Company Name", "Job Link", "Stage"], {"title": 0, "company": 1, "url": 2, "status": 3}),
])
def test_map_columns(headers, expected):
    assert importer.map_columns(headers) == expected


@pytest.mark.parametrize("value, expected", [
    ("2026-09-02", "2026-09-02"), ("9/2/2026", "2026-09-02"), ("9/2/26", "2026-09-02"),
    ("Sep 2, 2026", "2026-09-02"), ("9/2/2026 14:03:11", "2026-09-02"), ("", None), ("soon", None),
])
def test_parse_date(value, expected):
    assert importer.parse_date(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("", "applied"), ("Applied", "applied"), ("no response", "applied"), ("Interviewing", "interview"),
    ("Phone screen scheduled", "interview"), ("applied - rejected", "rejected"), ("No", "rejected"),
    ("Offer!", "offer"), ("not applied yet", "saved"), ("???", "applied"),
])
def test_parse_status(value, expected):
    assert importer.parse_status(value) == expected


def test_import_sheet(conn):
    summary = importer.import_csv(conn, SHEET, today=TODAY)
    assert summary["imported"] == 3
    assert summary["columns"]["title"] == "Job Title"
    assert summary["skipped"] == [
        'Row 4: couldn\'t read the date "next week"',
        "Row 6: missing job title or company",
    ]
    rows = {r["company"]: r for r in db.list_applications(conn)}
    assert rows["KBR"]["applied_on"] == "2026-09-02"
    assert rows["KBR"]["source"] == "careers.kbr.com"
    assert rows["Capgemini"]["status"] == "interview"
    assert rows["Capgemini"]["notes"] == "recruiter: Sam"
    assert rows["AspenTech"]["status"] == "rejected"

    # The sheet doesn't say when replies came in: they count as responses,
    # but not toward the average days to hear back
    s = db.stats(conn, today=TODAY)
    assert s["responded"] == 2
    assert s["avg_days_to_response"] is None


def test_reimport_skips_duplicates(conn):
    importer.import_csv(conn, SHEET, today=TODAY)
    again = importer.import_csv(conn, SHEET, today=TODAY)
    assert again["imported"] == 0
    assert again["duplicates"] == 3
    assert len(db.list_applications(conn)) == 3


def test_row_without_date_stays_undated(conn):
    importer.import_csv(conn, "Title,Company\nDev,Zeta\n", today=TODAY)
    assert db.list_applications(conn)[0]["applied_on"] is None


def test_missing_required_columns(conn):
    with pytest.raises(ValueError, match="job title column"):
        importer.import_csv(conn, "Name,When\nfoo,bar\n")


def test_export_round_trip(conn, tmp_path):
    importer.import_csv(conn, SHEET, today=TODAY)
    exported = importer.export_csv(conn)
    rows = list(csv.DictReader(io.StringIO(exported)))
    assert {r["Company"] for r in rows} == {"KBR", "Capgemini", "AspenTech"}

    # The export can be imported into a fresh tracker
    path = tmp_path / "fresh.db"
    db.init(path)
    with db.connect(path) as fresh:
        assert importer.import_csv(fresh, exported, today=TODAY)["imported"] == 3
