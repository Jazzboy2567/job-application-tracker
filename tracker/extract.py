"""Read job details from a posting URL.

Most job boards (Greenhouse, Lever, Ashby, Workable, many company career
pages) embed a schema.org JobPosting as JSON-LD for Google Jobs. That is the
most reliable source, so it is tried first; OpenGraph / <title> tags are the
fallback. Sites that block automated reading (LinkedIn, Indeed) return an
error and the user fills the form in by hand.
"""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BYTES = 3_000_000

# Hosts whose pages need a login or block automated requests
BLOCKED_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com")

SITE_NAMES = {
    "greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "ashbyhq.com": "Ashby",
    "myworkdayjobs.com": "Workday",
    "workable.com": "Workable",
    "smartrecruiters.com": "SmartRecruiters",
    "icims.com": "iCIMS",
    "linkedin.com": "LinkedIn",
    "indeed.com": "Indeed",
    "glassdoor.com": "Glassdoor",
    "ziprecruiter.com": "ZipRecruiter",
    "wellfound.com": "Wellfound",
    "usajobs.gov": "USAJOBS",
}


class ExtractError(Exception):
    pass


def source_for(url):
    """Human name for the job site a link points at, e.g. 'Greenhouse'."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for domain, name in SITE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.json_ld = []
        self.meta = {}
        self.title = ""
        self._in_ld = False
        self._in_title = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.json_ld.append("".join(self._buf))
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._in_title:
            self.title += data


def _job_postings(blobs):
    """Yield every JobPosting object in the page's JSON-LD blocks."""
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds:
                yield node
            for key in ("@graph", "mainEntity", "itemListElement"):
                if key in node:
                    yield from walk(node[key])

    for blob in blobs:
        try:
            data = json.loads(blob.strip())
        except json.JSONDecodeError:
            continue
        yield from walk(data)


def _text(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _location(posting):
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        remote = "Remote"
    else:
        remote = ""
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    names = []
    for place in places:
        address = place.get("address", {}) if isinstance(place, dict) else {}
        if isinstance(address, str):
            names.append(_text(address))
            continue
        parts = [_text(address.get(k)) for k in ("addressLocality", "addressRegion")]
        name = ", ".join(p for p in parts if p) or _text(address.get("addressCountry"))
        if name and name not in names:
            names.append(name)
    joined = "; ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
    return " / ".join(p for p in (joined, remote) if p)


def _money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    return f"{n:,.0f}" if n >= 1000 or n == int(n) else f"{n:,.2f}"


def _salary(posting):
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return _text(base) if base else ""
    currency = base.get("currency", "")
    symbol = "$" if currency in ("USD", "") else f"{currency} "
    value = base.get("value", {})
    if not isinstance(value, dict):
        value = {"value": value}
    unit = str(value.get("unitText") or base.get("unitText") or "").lower()
    low, high, single = _money(value.get("minValue")), _money(value.get("maxValue")), _money(value.get("value"))
    if low and high and low != high:
        amount = f"{symbol}{low}–{symbol}{high}"
    elif low or high or single:
        amount = f"{symbol}{low or high or single}"
    else:
        return ""
    return f"{amount} / {unit}" if unit else amount


def _employment_type(posting):
    kind = posting.get("employmentType")
    kinds = kind if isinstance(kind, list) else [kind] if kind else []
    return ", ".join(str(k).replace("_", " ").title() for k in kinds)


def _split_title(title, site_name=""):
    """'Software Engineer at Acme' / 'Software Engineer - Acme' -> (title, company)."""
    title = re.sub(r"\s*[|·]\s*(LinkedIn|Indeed|Glassdoor|Careers?|Jobs?)\s*$", "", title, flags=re.I).strip()
    m = re.match(r"^(?:Job Application for )?(.+?) at (.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    for sep in (" - ", " – ", " | "):
        if sep in title:
            first, rest = title.split(sep, 1)
            # "Acme - Software Engineer" when the site name matches the first half
            if site_name and first.strip().lower() == site_name.strip().lower():
                return rest.strip(), first.strip()
            return first.strip(), rest.split(sep)[0].strip()
    return title.strip(), site_name.strip()


def parse_page(page_html, url=""):
    """Pull job fields out of a posting page's HTML. Pure function, easy to test."""
    parser = _PageParser()
    parser.feed(page_html)
    result = {"url": url, "source": source_for(url) if url else "", "title": "", "company": "",
              "location": "", "salary": "", "employment_type": "", "date_posted": "", "found_via": ""}

    posting = next(_job_postings(parser.json_ld), None)
    if posting:
        org = posting.get("hiringOrganization") or {}
        result.update(
            title=_text(posting.get("title")),
            company=_text(org),
            location=_location(posting),
            salary=_salary(posting),
            employment_type=_employment_type(posting),
            date_posted=str(posting.get("datePosted") or "")[:10],
            found_via="the job data embedded in the page",
        )

    if not result["title"] or not result["company"]:
        meta_title = _text(parser.meta.get("og:title") or parser.meta.get("twitter:title") or parser.title)
        site_name = _text(parser.meta.get("og:site_name"))
        title, company = _split_title(meta_title, site_name)
        result["title"] = result["title"] or title
        result["company"] = result["company"] or company
        result["found_via"] = result["found_via"] or ("the page title" if meta_title else "")

    return result


GREENHOUSE_PATH = re.compile(r"^/([^/]+)/jobs/(\d+)")


def _greenhouse_ids(parsed):
    """(board, job id) for Greenhouse links, else None. Greenhouse job pages are
    rendered with JavaScript and carry no JSON-LD, but its public job board API does."""
    host = parsed.hostname or ""
    if host != "greenhouse.io" and not host.endswith(".greenhouse.io"):
        return None
    m = GREENHOUSE_PATH.match(parsed.path)
    if m:
        return m.group(1), m.group(2)
    query = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
    if parsed.path.startswith("/embed/job_app") and query.get("for") and query.get("token"):
        return query["for"], query["token"]
    return None


def _from_greenhouse(client, board, job_id, url):
    resp = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    return {
        "url": url,
        "source": "Greenhouse",
        "title": _text(data.get("title")),
        "company": _text(data.get("company_name")) or board.replace("-", " ").title(),
        "location": _text(data.get("location")),
        "salary": "",
        "employment_type": "",
        "date_posted": str(data.get("first_published") or "")[:10],
        "found_via": "Greenhouse's public job board API",
    }


def fetch_job(url, client=None, timeout=15.0):
    """Download a posting and extract its fields. Raises ExtractError with a
    message suitable for showing to the user."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ExtractError("That doesn't look like a web link. It should start with https://")
    host = parsed.hostname.lower()
    if any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS):
        raise ExtractError(f"{source_for(url)} requires a login, so it can't be read automatically. Fill in the details below.")

    own_client = client is None
    client = client or httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout)
    try:
        greenhouse = _greenhouse_ids(parsed)
        if greenhouse:
            try:
                job = _from_greenhouse(client, *greenhouse, url)
                if job and job["title"]:
                    return job
            except (httpx.HTTPError, ValueError):
                pass  # fall back to reading the page itself
        resp = client.get(url)
    except httpx.HTTPError as err:
        raise ExtractError(f"Couldn't reach that page ({err.__class__.__name__}). Fill in the details below.") from err
    finally:
        if own_client:
            client.close()

    if resp.status_code >= 400:
        raise ExtractError(f"The site answered with an error ({resp.status_code}). Fill in the details below.")
    if "html" not in resp.headers.get("content-type", "html"):
        raise ExtractError("That link isn't a web page. Fill in the details below.")

    job = parse_page(resp.text[:MAX_BYTES], str(resp.url))
    job["url"] = url  # keep the link the user pasted, not a redirect target
    job["source"] = source_for(url)
    if not job["title"]:
        raise ExtractError("Couldn't find the job details on that page. Fill in the details below.")
    return job
