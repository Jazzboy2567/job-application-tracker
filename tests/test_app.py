import pytest
from fastapi.testclient import TestClient

from tracker import db
from tracker.app import create_app
from tracker.extract import ExtractError


def fake_fetcher(url):
    if "blocked" in url:
        raise ExtractError("That site requires a login. Fill in the details below.")
    return {"url": url, "source": "Greenhouse", "title": "Software Engineer I", "company": "Acme",
            "location": "Houston, TX", "salary": "$90,000 / year", "employment_type": "Full Time",
            "date_posted": "2026-09-01", "found_via": "the job data embedded in the page"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "app.db", fetcher=fake_fetcher))


def create(client, **fields):
    data = {"title": "Engineer", "company": "Acme", "url": "", "applied_on": "2026-09-01", "status": "applied", **fields}
    return client.post("/applications", data=data, follow_redirects=False)


def test_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No applications yet" in r.text


def test_add_by_link_prefills_the_form(client):
    r = client.get("/add", params={"url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert 'value="Software Engineer I"' in r.text
    assert 'value="Houston, TX"' in r.text
    assert "Filled in automatically from the job data embedded in the page" in r.text


def test_add_by_link_falls_back_to_manual_entry(client):
    r = client.get("/add", params={"url": "https://blocked.example/job"})
    assert "requires a login" in r.text
    assert 'value="https://blocked.example/job"' in r.text


def test_create_list_and_duplicate_link(client):
    r = create(client, url="https://boards.greenhouse.io/acme/jobs/1")
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Added Engineer at Acme" in page
    assert "Greenhouse ↗" in page

    again = client.get("/add", params={"url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert "already tracking this job" in again.text


def test_create_rejects_bad_input(client):
    assert create(client, title="").status_code == 400
    assert create(client, url="javascript:alert(1)").status_code == 400


def test_status_change_and_history(client):
    create(client)
    r = client.post("/applications/1/status", data={"status": "interview", "on": "2026-09-08", "next": "/"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"
    detail = client.get("/applications/1").text
    assert "2026-09-08" in detail and "Interview" in detail


def test_status_redirect_stays_on_site(client):
    create(client)
    r = client.post("/applications/1/status", data={"status": "offer", "next": "https://evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_edit_and_delete(client):
    create(client)
    client.post("/applications/1", data={"title": "Senior Engineer", "company": "Acme", "notes": "referral"})
    assert "Senior Engineer" in client.get("/").text
    client.post("/applications/1/delete")
    assert client.get("/applications/1").status_code == 404


def test_cross_site_form_posts_are_blocked(client):
    r = client.post("/applications", data={"title": "x", "company": "y"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_import_export_and_stats(client):
    sheet = "Job Title,Company,Date Applied\nData Analyst,KBR,9/2/2026\nQA Analyst,Cognizant,9/3/2026\n"
    r = client.post("/import", files={"file": ("jobs.csv", sheet.encode(), "text/csv")})
    assert "Imported 2 applications" in r.text

    export = client.get("/export.csv")
    assert export.headers["content-type"].startswith("text/csv")
    assert "KBR" in export.text

    stats = client.get("/stats")
    assert stats.status_code == 200
    assert "Response rate" in stats.text


def test_import_reports_bad_sheet(client):
    r = client.post("/import", files={"file": ("x.csv", b"Name,When\na,b\n", "text/csv")})
    assert r.status_code == 400
    assert "job title column" in r.text


def test_sorting_and_filtering_pages_render(client):
    create(client, title="B job", company="Zeta")
    create(client, title="A job", company="Alpha", applied_on="2026-09-02")
    page = client.get("/", params={"sort": "company", "dir": "asc"}).text
    table = page[page.index("<tbody>"):]  # the follow-up banner above also lists these jobs
    assert table.index("Alpha") < table.index("Zeta")
    assert "No applications match" in client.get("/", params={"status": "rejected"}).text
