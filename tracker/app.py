"""Web UI. Server-rendered pages and plain HTML forms; no JavaScript framework."""

import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, importer
from .extract import ExtractError, fetch_job, source_for

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(STATUSES=db.STATUSES, today=lambda: date.today().isoformat())

FIELDS = ["title", "company", "url", "location", "salary", "employment_type", "date_posted", "applied_on", "notes"]


def create_app(db_file=None, fetcher=fetch_job):
    """App factory: tests pass a temporary database and a fake fetcher."""
    app = FastAPI(title="Job Application Tracker", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    db.init(db_file)

    def conn():
        return db.connect(db_file)

    def back(path, **params):
        query = urlencode({k: v for k, v in params.items() if v})
        return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)

    def safe_next(value, default="/"):
        return value if value and value.startswith("/") and not value.startswith("//") else default

    @app.middleware("http")
    async def same_origin_forms(request: Request, call_next):
        # This runs on localhost; refuse form posts sent from other websites.
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlparse(origin).netloc != request.headers.get("host"):
                return Response("Cross-site request blocked", status_code=403)
        return await call_next(request)

    # -------------------------------------------------------------- list

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str = "", q: str = "", sort: str = "applied", dir: str = "desc",
              msg: str = "", highlight: int = 0):
        with conn() as c:
            rows = db.list_applications(c, status=status, q=q, sort=sort, direction=dir)
            follow = db.follow_ups(c)
            counts = db.stats(c)["by_status"]
        return templates.TemplateResponse(request, "index.html", {
            "rows": rows, "follow_ups": follow, "counts": counts, "status": status,
            "q": q, "sort": sort, "dir": dir, "msg": msg, "highlight": highlight,
        })

    # -------------------------------------------------------------- add

    @app.get("/add", response_class=HTMLResponse)
    def add_form(request: Request, url: str = ""):
        job, error, duplicate = {"url": url.strip()}, "", None
        if job["url"]:
            with conn() as c:
                duplicate = db.find_duplicate(c, url=job["url"])
            if not duplicate:
                try:
                    job = fetcher(job["url"])
                except ExtractError as err:
                    error = str(err)
                    job["source"] = source_for(job["url"])
        job.setdefault("applied_on", date.today().isoformat())
        return templates.TemplateResponse(request, "form.html", {
            "job": job, "error": error, "duplicate": duplicate, "mode": "add",
        })

    @app.post("/applications")
    async def create(request: Request):
        form = await request.form()
        fields = {k: form.get(k, "") for k in FIELDS}
        fields["source"] = source_for(fields["url"]) if fields["url"] else ""
        status = form.get("status", "applied")
        with conn() as c:
            dup = db.find_duplicate(c, url=fields["url"] or None, company=fields["company"],
                                    title=fields["title"], applied_on=fields["applied_on"] or None)
            if dup:
                return back(f"/applications/{dup['id']}", msg="You're already tracking this job.")
            try:
                app_id = db.add_application(c, fields, status=status)
            except ValueError as err:
                return templates.TemplateResponse(request, "form.html", {
                    "job": fields, "error": str(err), "duplicate": None, "mode": "add",
                }, status_code=400)
        return back("/", msg=f"Added {fields['title']} at {fields['company']}.", highlight=app_id)

    # -------------------------------------------------------------- edit

    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def edit_form(request: Request, app_id: int, msg: str = ""):
        with conn() as c:
            row = db.get_application(c, app_id)
            if row is None:
                raise HTTPException(404, "Application not found")
            history = db.status_history(c, app_id)
        return templates.TemplateResponse(request, "form.html", {
            "job": dict(row), "history": history, "error": "", "duplicate": None, "mode": "edit", "msg": msg,
        })

    @app.post("/applications/{app_id}")
    async def update(request: Request, app_id: int):
        form = await request.form()
        fields = {k: form.get(k, "") for k in FIELDS}
        fields["source"] = source_for(fields["url"]) if fields["url"] else ""
        with conn() as c:
            if db.get_application(c, app_id) is None:
                raise HTTPException(404, "Application not found")
            try:
                db.update_application(c, app_id, fields)
            except ValueError as err:
                return back(f"/applications/{app_id}", msg=str(err))
            except sqlite3.IntegrityError:  # UNIQUE(url): another application already has this link
                return back(f"/applications/{app_id}", msg="Another application already uses that link.")
        return back(f"/applications/{app_id}", msg="Saved.")

    @app.post("/applications/{app_id}/status")
    def change_status(app_id: int, status: str = Form(...), on: str = Form(""), next: str = Form("")):
        try:
            on_date = date.fromisoformat(on) if on else None
        except ValueError:
            on_date = None
        with conn() as c:
            try:
                db.set_status(c, app_id, status, on=on_date)
            except LookupError:
                raise HTTPException(404, "Application not found")
            except ValueError as err:
                raise HTTPException(400, str(err))
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/applications/{app_id}/delete")
    def delete(app_id: int):
        with conn() as c:
            row = db.get_application(c, app_id)
            if row is None:
                raise HTTPException(404, "Application not found")
            db.delete_application(c, app_id)
        return back("/", msg=f"Deleted {row['title']} at {row['company']}.")

    # -------------------------------------------------------------- import / export

    @app.get("/import", response_class=HTMLResponse)
    def import_form(request: Request):
        return templates.TemplateResponse(request, "import.html", {"summary": None, "error": ""})

    @app.post("/import", response_class=HTMLResponse)
    async def import_upload(request: Request, file: UploadFile = File(...)):
        raw = await file.read()
        summary, error = None, ""
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        try:
            with conn() as c:
                summary = importer.import_csv(c, text)
        except ValueError as err:
            error = str(err)
        return templates.TemplateResponse(request, "import.html", {"summary": summary, "error": error},
                                          status_code=400 if error else 200)

    @app.get("/export.csv")
    def export():
        with conn() as c:
            body = importer.export_csv(c)
        filename = f"job-applications-{date.today().isoformat()}.csv"
        return Response(body, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # -------------------------------------------------------------- stats

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request):
        with conn() as c:
            s = db.stats(c)
        peak = max((w["n"] for w in s["weekly"]), default=0)
        return templates.TemplateResponse(request, "stats.html", {"s": s, "peak": peak})

    return app

