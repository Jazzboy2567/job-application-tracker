# Job Application Tracker

[![Tests](https://github.com/Jazzboy2567/job-application-tracker/actions/workflows/tests.yml/badge.svg)](https://github.com/Jazzboy2567/job-application-tracker/actions/workflows/tests.yml)

A local web app for keeping track of job applications. Paste a link to a job
posting and it fills in the title, company, location, salary and posting date
for you. Then track each application from *Applied* to *Interview* to *Offer*
(or *Rejected*), see which ones need a follow-up, and get real numbers on your
search: response rate, how long companies take to reply, and applications per week.

I built it to replace the Google Sheet I was using during my own job search.
It runs entirely on your computer: no account, and your data stays in one
SQLite file.

**Stack:** Python 3.11+, FastAPI, SQLite (hand-written SQL, no ORM), Jinja2
templates, httpx, pytest.

## Features

- **Add by link.** Reads the schema.org `JobPosting` data that job boards embed
  for Google Jobs (Lever, Ashby, Workable, SmartRecruiters, many company career
  sites), uses Greenhouse's public job board API for Greenhouse links, and falls
  back to the page title. You always review the details before saving. Sites
  that require a login (LinkedIn, Indeed, Glassdoor) open a blank form with
  the link filled in.
- **Status history.** Every status change is recorded with its date.
- **Sort, filter and search** by date, company, title or status.
- **Follow-up list** of applications with no update for 14+ days.
- **Stats** computed in SQL: response rate, average days to first reply
  (a `ROW_NUMBER()` window function picks each application's first reply),
  applications per week (a recursive CTE fills in empty weeks), and results by
  job site.
- **Google Sheets import** from CSV, with column detection by header name and
  duplicate skipping, so re-importing the same sheet is safe. **CSV export**
  in the same format.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m tracker               # opens http://127.0.0.1:8000
```

Data is stored in `data/tracker.db`, which is ignored by git. Set `TRACKER_DB`
to use a different file.

### Importing from Google Sheets

In your sheet, choose **File → Download → Comma-separated values (.csv)**, then
upload it on the **Import** page. Recognized headers include *Job Title /
Position / Role*, *Company*, *Date Applied / Date*, *Link / URL*, *Status*,
*Location*, *Salary* and *Notes*. Dates like `9/5/2026`, `2026-09-05` and
`Sep 5, 2026` all work. Because a spreadsheet doesn't say *when* an
application's status changed, imported replies count toward your response rate
but not toward the average days to hear back.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite covers link parsing against sample pages (plus a fake Greenhouse
API), every SQL query against a temporary database with fixed dates, the
importer on messy spreadsheets, and the web pages end to end.

## Project layout

```
tracker/
  app.py        FastAPI routes (server-rendered pages, plain HTML forms)
  db.py         schema and all SQL queries
  extract.py    job link -> title, company, location, salary, dates
  importer.py   Google Sheets CSV import / export
  templates/    Jinja2 pages
  static/       stylesheet
tests/          pytest suite
```

## Security notes

The server only listens on `127.0.0.1`, rejects form posts sent from other
websites, accepts only `http(s)` links, and builds SQL with parameters, using a
whitelist for sort columns.
