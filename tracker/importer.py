"""Import applications from a spreadsheet exported as CSV
(Google Sheets: File -> Download -> Comma-separated values)."""

import csv
import io
import re
from datetime import date, datetime

from . import db
from .extract import source_for

# Header names people commonly use, matched case-insensitively
ALIASES = {
    "title": ["job title", "title", "job", "job name", "position", "role", "job position"],
    "company": ["company", "company name", "employer", "organization", "org"],
    "applied_on": ["date applied", "applied on", "applied", "date", "application date", "applied date"],
    "url": ["link", "url", "job link", "posting", "job url", "posting link", "listing"],
    "status": ["status", "stage", "result"],
    "location": ["location", "city"],
    "salary": ["salary", "pay", "compensation"],
    "notes": ["notes", "note", "comments"],
}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%m-%d-%Y"]

# Checked in this order, most advanced first, so "applied - rejected" reads as
# rejected and "not applied" as saved.
STATUS_WORDS = {
    "offer": ["offer", "accepted", "hired"],
    "withdrawn": ["withdrawn", "withdrew"],
    "rejected": ["rejected", "rejection", "declined", "denied", "closed", "no"],
    "interview": ["interview", "phone screen", "screen", "onsite", "technical", "assessment", "oa"],
    "saved": ["saved", "to apply", "wishlist", "interested", "not applied"],
    "applied": ["applied", "submitted", "pending", "waiting", "no response", "sent"],
}


def map_columns(headers):
    """Return {field: column index} for the headers we recognise."""
    normalized = [re.sub(r"[^a-z ]", "", h.lower()).strip() for h in headers]
    mapping = {}
    for field, names in ALIASES.items():
        for name in names:  # earlier aliases win
            if name in normalized and normalized.index(name) not in mapping.values():
                mapping[field] = normalized.index(name)
                break
    return mapping


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    # Google Sheets sometimes exports datetimes: "9/5/2026 14:03:11"
    first = value.split(" ")[0]
    if first != value:
        return parse_date(first)
    return None


def parse_status(value):
    value = (value or "").strip().lower()
    if not value:
        return "applied"
    if value == "no response":
        return "applied"
    for status, words in STATUS_WORDS.items():
        # Short words ("no", "oa") only count as the whole cell; longer ones
        # match at a word start, so "interviewing" counts as interview.
        if value in words or any(re.search(rf"\b{re.escape(w)}", value) for w in words if len(w) > 2):
            return status
    return "applied"


def import_csv(conn, text, today=None):
    """Import rows; returns a summary dict. Safe to run twice on the same sheet:
    rows already in the tracker (same link, or same company + title + date) are skipped."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    summary = {"imported": 0, "duplicates": 0, "skipped": [], "columns": {}}
    if not rows:
        return summary

    mapping = map_columns(rows[0])
    summary["columns"] = {field: rows[0][i] for field, i in mapping.items()}
    if "title" not in mapping or "company" not in mapping:
        raise ValueError(
            "Couldn't find a job title column and a company column. "
            f"Headers found: {', '.join(h for h in rows[0] if h) or '(none)'}"
        )

    for line, row in enumerate(rows[1:], start=2):
        cell = lambda field: row[mapping[field]].strip() if field in mapping and mapping[field] < len(row) else ""
        title, company = cell("title"), cell("company")
        if not any(c.strip() for c in row):
            continue
        if not title or not company:
            summary["skipped"].append(f"Row {line}: missing job title or company")
            continue

        raw_date = cell("applied_on")
        applied_on = parse_date(raw_date)
        if raw_date and not applied_on:
            summary["skipped"].append(f"Row {line}: couldn't read the date \"{raw_date}\"")
            continue

        url = cell("url")
        if url and not url.startswith(("http://", "https://")):
            url = ""
        if db.find_duplicate(conn, url=url or None, company=company, title=title, applied_on=applied_on):
            summary["duplicates"] += 1
            continue

        status = parse_status(cell("status"))
        fields = {
            "title": title,
            "company": company,
            "url": url,
            "applied_on": applied_on or "",
            "location": cell("location"),
            "salary": cell("salary"),
            "notes": cell("notes"),
            "source": source_for(url) if url else "",
        }
        app_id = db.add_application(conn, fields, status="applied" if status != "saved" else "saved",
                                    today=today or date.today(), default_applied_today=False)
        if status not in ("applied", "saved"):
            # Record the later status on the day it was imported; the sheet
            # doesn't say when it changed.
            db.set_status(conn, app_id, status, on=today or date.today(), date_unknown=True)
        summary["imported"] += 1

    return summary


def export_csv(conn):
    out = io.StringIO()
    writer = csv.writer(out)
    cols = ["title", "company", "status", "applied_on", "url", "location", "salary", "employment_type", "source", "notes"]
    writer.writerow(["Job Title", "Company", "Status", "Date Applied", "Link", "Location", "Salary", "Type", "Site", "Notes"])
    for row in db.list_applications(conn, sort="applied", direction="desc"):
        writer.writerow([row[c] or "" for c in cols])
    return out.getvalue()
