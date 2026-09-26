import json
import re

import httpx
import pytest

from tracker.extract import ExtractError, fetch_job, parse_page, source_for


def page(ld=None, head=""):
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld is not None else ""
    return f"<html><head>{head}{script}</head><body><h1>Job</h1></body></html>"


GREENHOUSE_STYLE = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Software Engineer I",
    "datePosted": "2026-09-01T12:00:00-05:00",
    "employmentType": ["FULL_TIME"],
    "hiringOrganization": {"@type": "Organization", "name": "Acme Robotics"},
    "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Houston", "addressRegion": "TX"}}],
    "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "USD",
        "value": {"@type": "QuantitativeValue", "minValue": 85000, "maxValue": 110000, "unitText": "YEAR"},
    },
}


def test_reads_json_ld_job_posting():
    job = parse_page(page(GREENHOUSE_STYLE), "https://boards.greenhouse.io/acme/jobs/123")
    assert job["title"] == "Software Engineer I"
    assert job["company"] == "Acme Robotics"
    assert job["location"] == "Houston, TX"
    assert job["salary"] == "$85,000–$110,000 / year"
    assert job["employment_type"] == "Full Time"
    assert job["date_posted"] == "2026-09-01"
    assert job["source"] == "Greenhouse"
    assert job["found_via"] == "the job data embedded in the page"


def test_finds_posting_inside_graph_and_marks_remote():
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "Careers"},
        {"@type": "JobPosting", "title": "QA Analyst", "hiringOrganization": "Beta Corp",
         "jobLocationType": "TELECOMMUTE", "baseSalary": {"currency": "USD", "value": {"value": 30, "unitText": "HOUR"}}},
    ]}
    job = parse_page(page(ld), "https://jobs.lever.co/beta/abc")
    assert (job["title"], job["company"], job["location"]) == ("QA Analyst", "Beta Corp", "Remote")
    assert job["salary"] == "$30 / hour"
    assert job["source"] == "Lever"


def test_decodes_html_entities_and_whitespace():
    ld = dict(GREENHOUSE_STYLE, title="Data  Analyst &amp; Reporting", hiringOrganization={"name": "R&amp;D Labs"})
    job = parse_page(page(ld))
    assert job["title"] == "Data Analyst & Reporting"
    assert job["company"] == "R&D Labs"


def test_ignores_broken_json_ld_and_uses_page_title():
    html = page(head="<title>Junior Developer at Gamma Inc</title>").replace(
        "</head>", '<script type="application/ld+json">{not json</script></head>')
    job = parse_page(html)
    assert (job["title"], job["company"], job["found_via"]) == ("Junior Developer", "Gamma Inc", "the page title")


@pytest.mark.parametrize("og_title, site, expected", [
    ("Backend Engineer - Delta", "", ("Backend Engineer", "Delta")),
    ("Delta - Backend Engineer", "Delta", ("Backend Engineer", "Delta")),
    ("Job Application for Programmer Analyst at KBR", "", ("Programmer Analyst", "KBR")),
])
def test_opengraph_title_patterns(og_title, site, expected):
    head = f'<meta property="og:title" content="{og_title}"><meta property="og:site_name" content="{site}">'
    job = parse_page(page(head=head))
    assert (job["title"], job["company"]) == expected


def test_source_names():
    assert source_for("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123") == "Workday"
    assert source_for("https://www.linkedin.com/jobs/view/1") == "LinkedIn"
    assert source_for("https://careers.example.com/job/1") == "careers.example.com"


def client_returning(status=200, body="", content_type="text/html"):
    def handler(request):
        return httpx.Response(status, text=body, headers={"content-type": content_type})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_job_success():
    job = fetch_job("https://boards.greenhouse.io/acme/jobs/123", client=client_returning(body=page(GREENHOUSE_STYLE)))
    assert job["title"] == "Software Engineer I"
    assert job["url"] == "https://boards.greenhouse.io/acme/jobs/123"


@pytest.mark.parametrize("url, client, message", [
    ("ftp://example.com/job", None, "doesn't look like a web link"),
    ("https://www.linkedin.com/jobs/view/123", None, "requires a login"),
    ("https://example.com/job", client_returning(status=404), "error (404)"),
    ("https://example.com/job.pdf", client_returning(content_type="application/pdf"), "isn't a web page"),
    ("https://example.com/job", client_returning(body="<html><body>nothing</body></html>"), "Couldn't find"),
])
def test_fetch_job_errors_are_friendly(url, client, message):
    with pytest.raises(ExtractError, match=re.escape(message)):
        fetch_job(url, client=client)


GREENHOUSE_API = {"title": "Commercial Policy Lead", "company_name": "Discord",
                  "location": {"name": "San Francisco Bay Area"}, "first_published": "2026-09-15T12:52:11-04:00"}


def greenhouse_client(api_status=200):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(api_status, json=GREENHOUSE_API if api_status == 200 else {})
        return httpx.Response(200, text="<html><head><title>Commercial Policy Lead</title></head></html>",
                              headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


@pytest.mark.parametrize("url", [
    "https://job-boards.greenhouse.io/discord/jobs/8806482002",
    "https://boards.greenhouse.io/discord/jobs/8806482002?gh_src=abc",
    "https://boards.greenhouse.io/embed/job_app?for=discord&token=8806482002",
])
def test_greenhouse_links_use_the_public_api(url):
    client, seen = greenhouse_client()
    job = fetch_job(url, client=client)
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/discord/jobs/8806482002"]
    assert (job["title"], job["company"], job["location"], job["date_posted"]) == (
        "Commercial Policy Lead", "Discord", "San Francisco Bay Area", "2026-09-15")
    assert job["url"] == url


def test_greenhouse_falls_back_to_the_page_when_api_fails():
    client, seen = greenhouse_client(api_status=404)
    job = fetch_job("https://job-boards.greenhouse.io/discord/jobs/1", client=client)
    assert len(seen) == 2 and job["title"] == "Commercial Policy Lead"
