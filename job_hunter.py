#!/usr/bin/env python3
"""Daily job hunter: finds recent manual-QA jobs that fit Supriya's resume.

Pulls job posts from job-search APIs and company career boards, keeps only
recent Bengaluru / remote-India QA roles whose experience range fits, scores
them against the resume, drops ones already sent, then writes a CSV and
emails a digest with direct links.

Keys come from environment variables (GitHub Actions secrets):
  SERPAPI_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY, JOOBLE_KEY,
  SMTP_USER, SMTP_PASSWORD, EMAIL_TO  (all optional; missing ones are skipped)
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
ALL_CSV = DATA / "all_matches.csv"
TIMEOUT = 25
UA = {"User-Agent": "job-hunter/1.0 (personal job search)"}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(dt.timezone.utc)
SOURCE_WARNINGS: list[str] = []   # partial failures to show in the report
RUN_STATS: dict = {}


def log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ helpers
def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_age_days(value) -> float | None:
    """Turn '3 days ago', '22 hours ago', ISO dates or epoch ms into age in days."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):  # epoch milliseconds (Lever)
        ts = dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
        return (NOW - ts).total_seconds() / 86400
    s = str(value).strip().lower()
    if s in ("today", "just now", "just posted") or "minute" in s or "moment" in s:
        return 0.0
    if s == "yesterday":
        return 1.0
    m = re.search(r"(\d+)\+?\s*(hour|hr|day|week|month|year)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return n * {"hour": 1 / 24, "hr": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[unit]
    try:
        iso = s.replace("z", "+00:00")
        ts = dt.datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (NOW - ts).total_seconds() / 86400)
    except ValueError:
        return None


EXP_RE = re.compile(
    r"(\d{1,2}(?:\.\d)?)\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2}(?:\.\d)?))?\s*\+?\s*(?:years|year|yrs|yr)",
    re.I,
)


def parse_experience(text: str) -> tuple[float | None, float | None]:
    """Find the overall experience requirement.

    Job posts often list several ('6+ years overall, 2+ years in API testing'),
    so take the requirement with the largest minimum, which is the overall one.
    'Minimum 5 years and maximum of 8 years' is read as 5-8.
    """
    text = text or ""
    found = []
    for m in EXP_RE.finditer(text):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else None
        if lo > 25 or (hi is not None and (hi > 30 or hi < lo)):
            continue
        before = text[max(0, m.start() - 20): m.start()].lower()
        after = text[m.end(): m.end() + 35].lower()
        # skip company-age phrases like "over 40 years of industry expertise"
        if re.search(r"founded|since", before) or re.search(
                r"^\W*(of )?(industry|expertise|history|in business|legacy|track record|serving|excellence|old\b)", after):
            continue
        # "maximum of 8 years" / "up to 8 years" is the top of the range, not a minimum
        if re.search(r"(max(imum)?( of)?|up ?to)\s*$", before):
            if found and found[-1][1] is None and found[-1][0] <= lo:
                found[-1] = (found[-1][0], lo)
            continue
        found.append((lo, hi))
    if not found:
        return None, None
    return max(found, key=lambda r: r[0])


def exp_label(lo, hi) -> str:
    if lo is None:
        return "Not stated"
    if hi is None:
        return f"{lo:g}+ yrs"
    return f"{lo:g}–{hi:g} yrs"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def job_key(job: dict) -> str:
    base = f"{norm(job['company'])}|{norm(job['title'])}|{norm(job['location'])[:30]}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def make_job(**kw) -> dict:
    job = {
        "title": kw.get("title", "").strip(),
        "company": (kw.get("company") or "").strip(),
        "location": (kw.get("location") or "").strip(),
        "link": kw.get("link") or "",
        "posted_raw": kw.get("posted_raw") or "",
        "age_days": kw.get("age_days"),
        "description": strip_html(kw.get("description"))[:6000],
        "source": kw.get("source", ""),
        "remote": bool(kw.get("remote")),
    }
    if job["age_days"] is None:
        job["age_days"] = parse_age_days(job["posted_raw"])
    return job


# ------------------------------------------------------------------ sources
def get_json(method: str, url: str, timeout: int = TIMEOUT, retries: int = 2, **kw):
    """Request JSON, retrying slow or temporarily failing answers."""
    import time
    headers = {**UA, **kw.pop("headers", {})}
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, timeout=timeout, headers=headers, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= retries:
                raise
            time.sleep(5 * (attempt + 1))


def best_apply_link(item: dict) -> str:
    """Prefer the employer's own page or a known job board over aggregators."""
    opts = item.get("apply_options") or []
    preferred = ["careers", "linkedin", "naukri", "indeed", "foundit", "instahyre", "glassdoor"]
    for word in preferred:
        for o in opts:
            if word in (o.get("link", "") + o.get("title", "")).lower():
                return o["link"]
    if opts:
        return opts[0].get("link", "")
    return item.get("share_link", "")


def src_serpapi(cfg: dict) -> list[dict]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        log("  SerpApi: skipped (no SERPAPI_KEY)")
        return []
    c = cfg["serpapi"]
    jobs, failed = [], []
    for q in c["queries"]:
        token = None
        for _ in range(max(1, int(c.get("pages_per_query", 1)))):
            params = {"engine": "google_jobs", "q": q, "location": c["location"], "gl": "in", "hl": "en", "api_key": key}
            if token:
                params["next_page_token"] = token
            try:  # Google Jobs searches can take 30-60 s; one slow query must not stop the rest
                data = get_json("GET", "https://serpapi.com/search.json", timeout=90, params=params)
            except Exception as e:
                failed.append(f"'{q}': {type(e).__name__}")
                log(f"  SerpApi query '{q}' failed: {e}")
                break
            if data.get("error"):
                if "hasn't returned any results" not in data["error"]:
                    failed.append(f"'{q}': {data['error']}")
                break
            for it in data.get("jobs_results", []):
                ext = it.get("detected_extensions") or {}
                jobs.append(make_job(
                    title=it.get("title", ""), company=it.get("company_name"), location=it.get("location"),
                    link=best_apply_link(it), posted_raw=ext.get("posted_at") or next(
                        (e for e in it.get("extensions", []) if re.search(r"ago|today|yesterday|just", str(e), re.I)), None),
                    description=it.get("description"), source=f"Google Jobs ({it.get('via', '').replace('via ', '')})",
                    remote=ext.get("work_from_home"),
                ))
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                break
    log(f"  SerpApi: {len(jobs)} jobs")
    if failed and not jobs:
        raise RuntimeError("all searches failed – " + "; ".join(failed))
    if failed:
        SOURCE_WARNINGS.append("SerpApi: " + "; ".join(failed))
    return jobs


def src_adzuna(cfg: dict) -> list[dict]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("  Adzuna: skipped (no ADZUNA_APP_ID / ADZUNA_APP_KEY)")
        return []
    c = cfg["adzuna"]
    jobs = []
    for q in c["queries"]:
        url = f"https://api.adzuna.com/v1/api/jobs/{c['country']}/search/1"
        params = {"app_id": app_id, "app_key": app_key, "what": q, "where": c["where"],
                  "max_days_old": cfg["max_age_days"], "results_per_page": c["results_per_page"],
                  "sort_by": "date", "content-type": "application/json"}
        data = get_json("GET", url, params=params)
        for it in data.get("results", []):
            jobs.append(make_job(
                title=it.get("title", ""), company=(it.get("company") or {}).get("display_name"),
                location=(it.get("location") or {}).get("display_name"), link=it.get("redirect_url"),
                posted_raw=it.get("created"), description=it.get("description"), source="Adzuna",
            ))
    log(f"  Adzuna: {len(jobs)} jobs")
    return jobs


def src_jooble(cfg: dict) -> list[dict]:
    key = os.getenv("JOOBLE_KEY")
    if not key:
        log("  Jooble: skipped (no JOOBLE_KEY)")
        return []
    c = cfg["jooble"]
    jobs = []
    for q in c["queries"]:
        data = get_json("POST", f"https://jooble.org/api/{key}", json={"keywords": q, "location": c["location"], "page": "1"})
        for it in data.get("jobs", []):
            jobs.append(make_job(
                title=it.get("title", ""), company=it.get("company"), location=it.get("location"),
                link=it.get("link"), posted_raw=it.get("updated"), description=it.get("snippet"),
                source=f"Jooble ({it.get('source', '')})".replace(" ()", ""),
            ))
    log(f"  Jooble: {len(jobs)} jobs")
    return jobs


def src_greenhouse(name: str) -> list[dict]:
    data = get_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{name}/jobs", params={"content": "true"})
    return [make_job(title=j.get("title", ""), company=name, location=(j.get("location") or {}).get("name"),
                     link=j.get("absolute_url"), posted_raw=j.get("updated_at"), description=j.get("content"),
                     source="Greenhouse") for j in data.get("jobs", [])]


def src_lever(name: str) -> list[dict]:
    data = get_json("GET", f"https://api.lever.co/v0/postings/{name}", params={"mode": "json"})
    return [make_job(title=j.get("text", ""), company=name, location=(j.get("categories") or {}).get("location"),
                     link=j.get("hostedUrl"), age_days=parse_age_days(j.get("createdAt")),
                     description=j.get("descriptionPlain"), source="Lever",
                     remote=(j.get("workplaceType") == "remote")) for j in data]


def src_smartrecruiters(name: str) -> list[dict]:
    data = get_json("GET", f"https://api.smartrecruiters.com/v1/companies/{name}/postings",
                    params={"country": "in", "limit": 100, "q": "test"})
    out = []
    qa_title = re.compile(r"\b(qa|quality|test|tester|testing|uat)\b", re.I)
    for j in data.get("content", []):
        loc = j.get("location") or {}
        desc = (j.get("function") or {}).get("label", "")
        if qa_title.search(j.get("name", "")):  # list has no description: fetch it for QA-looking titles only
            try:
                detail = get_json("GET", f"https://api.smartrecruiters.com/v1/companies/{name}/postings/{j.get('id')}")
                sections = ((detail.get("jobAd") or {}).get("sections") or {})
                desc = " ".join((sections.get(k) or {}).get("text", "") for k in
                                ("jobDescription", "qualifications", "additionalInformation"))
            except Exception:
                pass
        out.append(make_job(title=j.get("name", ""), company=(j.get("company") or {}).get("name") or name,
                            location=", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                            link=f"https://jobs.smartrecruiters.com/{name}/{j.get('id')}",
                            posted_raw=j.get("releasedDate"), description=desc,
                            source="SmartRecruiters", remote=loc.get("remote")))
    return out


def src_ashby(name: str) -> list[dict]:
    data = get_json("GET", f"https://api.ashbyhq.com/posting-api/job-board/{name}")
    return [make_job(title=j.get("title", ""), company=name, location=j.get("location"), link=j.get("jobUrl"),
                     posted_raw=j.get("publishedAt"), description=j.get("descriptionPlain"), source="Ashby",
                     remote=j.get("isRemote")) for j in data.get("jobs", [])]


BOARD_FETCHERS = {"greenhouse": src_greenhouse, "lever": src_lever,
                  "smartrecruiters": src_smartrecruiters, "ashby": src_ashby}


def collect(cfg: dict) -> tuple[list[dict], list[str]]:
    jobs, errors = [], []
    for name, fn in [("SerpApi", src_serpapi), ("Adzuna", src_adzuna), ("Jooble", src_jooble)]:
        try:
            jobs += fn(cfg)
        except Exception as e:  # keep going if one source fails
            errors.append(f"{name}: {e}")
            log(f"  {name}: FAILED – {e}")
    for kind, names in (cfg.get("company_boards") or {}).items():
        for name in names or []:
            try:
                got = BOARD_FETCHERS[kind](name)
                jobs += got
                log(f"  {kind}/{name}: {len(got)} jobs")
            except Exception as e:
                errors.append(f"{kind}/{name}: {e}")
                log(f"  {kind}/{name}: FAILED – {e}")
    return jobs, errors


# ------------------------------------------------------------------ filtering + scoring
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere in india|pan india)\b", re.I)


def location_ok(job: dict, cfg: dict) -> bool:
    loc = (job["location"] or "").lower()
    if any(p in loc for p in cfg["locations"]):
        return True
    if cfg.get("allow_remote_india"):
        remote = job["remote"] or REMOTE_RE.search(loc) or REMOTE_RE.search(job["title"])
        if remote and ("india" in loc or loc.strip() in ("", "remote", "anywhere") or job["source"].startswith(("Adzuna", "Jooble", "Google"))):
            return True
    return False


def experience_ok(lo, hi, cfg: dict) -> bool:
    """Keep a job only if its minimum required years is between min_years_from and min_years_to."""
    if lo is None:
        return bool(cfg.get("keep_unstated_experience", False))
    return cfg["min_years_from"] <= lo <= cfg["min_years_to"]


def score(job: dict, cfg: dict) -> tuple[int, list[str]]:
    text = f"{job['title']} {job['title']} {job['description']}".lower()
    total, why = 40, []
    for name, w in cfg["weights"].items():
        hits = [word for word in w["words"] if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text)]
        if not hits:
            continue
        pts = len(hits) * w["points"]
        pts = max(pts, w["cap"]) if w["points"] < 0 else min(pts, w["cap"])
        total += pts
        why.append(f"{name} {pts:+d} ({', '.join(hits[:4])})")
    if "manual" in job["title"].lower() or "functional" in job["title"].lower() or "uat" in job["title"].lower():
        total += 8
        why.append("manual in title +8")
    return max(0, min(100, total)), why


def evaluate(jobs: list[dict], cfg: dict) -> list[dict]:
    must = re.compile(cfg["title_must_match"], re.I)
    excl = re.compile(cfg["title_exclude"], re.I)
    kept, dropped = [], {}

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    for j in jobs:
        if not j["title"] or not j["link"]:
            drop("no title/link"); continue
        if not must.search(j["title"]):
            drop("not a QA title"); continue
        if excl.search(j["title"]):
            drop("excluded title"); continue
        if j["age_days"] is None and not cfg.get("keep_unknown_dates", False):
            drop("no posting date"); continue
        if j["age_days"] is not None and j["age_days"] > cfg["max_age_days"]:
            drop("too old"); continue
        if not location_ok(j, cfg):
            drop("location"); continue
        lo, hi = parse_experience(f"{j['title']} {j['description']}")
        if not experience_ok(lo, hi, cfg):
            drop("experience range"); continue
        s, why = score(j, cfg)
        if s < cfg["min_score"]:
            drop("score below minimum"); continue
        j.update(score=s, why="; ".join(why), exp=exp_label(lo, hi), key=job_key(j))
        kept.append(j)
    log(f"  Dropped: {dropped}")
    RUN_STATS.update(collected=len(jobs), dropped=dropped)
    # de-duplicate within this run (same job from several sources): keep best score
    best = {}
    for j in kept:
        if j["key"] not in best or j["score"] > best[j["key"]]["score"]:
            best[j["key"]] = j
    return sorted(best.values(), key=lambda j: (-j["score"], j["age_days"] if j["age_days"] is not None else 99))


# ------------------------------------------------------------------ output
def posted_label(job: dict) -> str:
    a = job["age_days"]
    if a is None:
        return "Date unknown"
    if a < 1:
        return "Today"
    return f"{int(round(a))} day{'s' if round(a) != 1 else ''} ago"


FIELDS = ["found_on", "score", "title", "company", "location", "exp", "posted", "source", "link", "why"]


def write_csv(new: list[dict], today: str) -> Path:
    DATA.mkdir(exist_ok=True)
    rows = [{"found_on": today, "score": j["score"], "title": j["title"], "company": j["company"],
             "location": j["location"], "exp": j["exp"], "posted": posted_label(j), "source": j["source"],
             "link": j["link"], "why": j["why"]} for j in new]
    day_file = DATA / f"matches_{today}.csv"
    with day_file.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    write_header = not ALL_CSV.exists()
    with ALL_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(rows)
    return day_file


def email_html(new: list[dict], errors: list[str], today: str) -> str:
    rows = []
    for j in new:
        color = "#1a7f37" if j["score"] >= 75 else ("#9a6700" if j["score"] >= 65 else "#57606a")
        rows.append(
            f"<tr><td style='padding:8px;border-bottom:1px solid #eee;vertical-align:top'>"
            f"<a href='{html.escape(j['link'])}' style='font-weight:600;color:#0969da'>{html.escape(j['title'])}</a><br>"
            f"{html.escape(j['company'] or '—')} · {html.escape(j['location'] or '—')}<br>"
            f"<span style='color:#57606a;font-size:12px'>{html.escape(j['exp'])} · {posted_label(j)} · {html.escape(j['source'])}</span></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center;font-weight:700;color:{color}'>{j['score']}</td></tr>")
    body = "".join(rows) or "<tr><td style='padding:8px'>No new matching jobs today.</td></tr>"
    err = ""
    if errors:
        err = "<p style='color:#9a6700;font-size:12px'>Some sources failed today: " + html.escape("; ".join(errors)) + "</p>"
    return (f"<div style='font-family:Arial,sans-serif;max-width:640px'>"
            f"<h2 style='margin:0 0 4px'>QA job matches – {today}</h2>"
            f"<p style='color:#57606a;margin:0 0 12px'>{len(new)} new roles · manual-QA focus · Bengaluru or remote · "
            f"posted in the last 15 days, minimum experience 4–7 years. Score = how well the post matches the resume (not a hiring chance).</p>"
            f"<table style='border-collapse:collapse;width:100%'><tr><th align='left' style='padding:8px'>Role</th>"
            f"<th style='padding:8px'>Score</th></tr>{body}</table>{err}</div>")


def markdown_report(new: list[dict], errors: list[str], today: str) -> str:
    lines = [f"# QA job matches – {today}", "",
             f"**{len(new)} new roles** · manual-QA focus · Bengaluru or remote · minimum experience 4–7 years · posting date confirmed.",
             "Score = how well the post matches the resume (not a hiring chance).", ""]
    if new:
        lines += ["| Score | Role | Company | Location | Experience | Posted | Source |",
                  "|---|---|---|---|---|---|---|"]
        for j in new:
            cell = lambda s: (s or "—").replace("|", "/").replace("\n", " ")
            lines.append(f"| {j['score']} | [{cell(j['title'])}]({j['link']}) | {cell(j['company'])} | "
                         f"{cell(j['location'])} | {cell(j['exp'])} | {posted_label(j)} | {cell(j['source'])} |")
    else:
        lines.append("No new matching jobs today.")
    problems = errors + SOURCE_WARNINGS
    if problems:
        lines += ["", "_Some sources had problems today: " + "; ".join(problems) + "_"]
    if RUN_STATS:
        d = ", ".join(f"{k}: {v}" for k, v in sorted(RUN_STATS.get("dropped", {}).items(), key=lambda kv: -kv[1]))
        lines += ["", f"<sub>Checked {RUN_STATS.get('collected', 0)} job posts today. Left out: {d or 'none'}.</sub>"]
    return "\n".join(lines) + "\n"


def send_email(subject: str, html_body: str, attachment: Path | None) -> None:
    user, pwd, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pwd and to):
        log("  Email: skipped (SMTP_USER / SMTP_PASSWORD / EMAIL_TO not set)")
        return
    from email.mime.application import MIMEApplication
    msg = MIMEMultipart()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    if attachment and attachment.exists():
        part = MIMEApplication(attachment.read_bytes(), Name=attachment.name)
        part["Content-Disposition"] = f'attachment; filename="{attachment.name}"'
        msg.attach(part)
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, 465, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    log(f"  Email: sent to {to}")


# ------------------------------------------------------------------ main
def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = NOW.astimezone(IST).strftime("%Y-%m-%d")
    log(f"Job hunter run {today}")
    log("Collecting:")
    jobs, errors = collect(cfg)
    log(f"Collected {len(jobs)} jobs in total")
    matches = evaluate(jobs, cfg)

    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    new = [j for j in matches if j["key"] not in seen and j["link"] not in seen.values()]
    for j in new:
        seen[j["key"]] = j["link"]
    DATA.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=0))
    log(f"Matches: {len(matches)}  New (not sent before): {len(new)}")

    day_file = write_csv(new, today)
    for j in new[:15]:
        log(f"  [{j['score']}] {j['title']} – {j['company']} – {j['location']} – {posted_label(j)}\n        {j['link']}")
    # Readable list in the repo + on the GitHub Actions run page
    report = markdown_report(new, errors, today)
    (DATA / "latest.md").write_text(report, encoding="utf-8")
    (DATA / "new_count.txt").write_text(str(len(new)))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report)
    subject = f"{cfg.get('email_subject', 'QA job matches')}: {len(new)} new ({today})"
    send_email(subject, email_html(new, errors, today), day_file if new else None)
    if not jobs and errors:
        return 1  # every source failed: make the GitHub run show red
    return 0


if __name__ == "__main__":
    sys.exit(main())
