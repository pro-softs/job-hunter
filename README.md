# QA Job Hunter

A small program that runs every morning at **8:30 AM IST** on GitHub's computers (free), so your Mac can be off.

Each run:

1. Searches for jobs (not companies) in several places:
   - **Google Jobs**, through SerpApi. This covers LinkedIn, Naukri, Indeed, foundit and company career sites.
   - **Adzuna** and **Jooble**, two job search engines.
   - **Company career boards** you list in `config.yaml`. Bosch is already added.
2. Keeps only jobs that are:
   - posted in the **last 15 days**, with a known posting date. Undated listings are usually old reposts, so they're left out.
   - a **QA / testing** role, with automation-only, junior, hardware and other unrelated titles removed
   - in **Bengaluru**, or **remote in India**
   - asking for a **minimum of 4 to 7 years** of experience, so "4+", "5–8" and "6+" pass but "2+", "3–5" and "8+" are dropped. Jobs that don't state any experience are left out.
3. Gives each job a **match score (0–100)** for how well the post fits the resume. It rewards manual, functional, UAT and regression work, Postman, JIRA and SQL, the insurance, payments, fintech and e-commerce domains, and lead duties. It lowers the score for automation-heavy roles that need Selenium, Java or SDET skills. The score is not a hiring chance.
4. Skips anything it already showed you, then posts the new jobs with **direct links** in GitHub:
   - as a **GitHub issue** each morning. GitHub notifies you by email and in the GitHub mobile app.
   - in **`data/latest.md`**, a clickable list you can open in the repository.
   - in **`data/all_matches.csv`**, every match so far.

---

## One-time setup (about 10 minutes)

You need: a GitHub account and one free API key (SerpApi). No Gmail setup is needed.

### 1. Get the free API keys

| Key | Where | Notes |
|---|---|---|
| `SERPAPI_KEY` | serpapi.com → Sign up → Dashboard → API key | **Most important.** One run uses 4 searches, about 120 a month. Check your monthly limit on SerpApi's plan page. If it's lower, delete a query in `config.yaml`. |
| `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` | developer.adzuna.com → Register | If the first run shows "Adzuna: FAILED", Adzuna doesn't cover India for your key. Just leave it; the other sources still run. |
| `JOOBLE_KEY` | in.jooble.org/api/about → fill in the form | Free keys may have a limited number of requests in total. The config uses 2 per day. |

You can start with only SerpApi. Any source without a key is skipped.

### 2. Put the files on GitHub

1. github.com → **New repository** → name it `job-hunter` → choose **Private** → Create.
2. Click **"uploading an existing file"**, drag in `job_hunter.py`, `config.yaml`, `requirements.txt` and `README.md` → **Commit changes**.
3. Click **Add file → Create new file**, type the name `.github/workflows/daily.yml`, paste in the contents of `daily.yml` from this folder → **Commit changes**.

### 3. Add your keys as secrets

Repository → **Settings → Secrets and variables → Actions → New repository secret**. Add each of these:

| Name | Value |
|---|---|
| `SERPAPI_KEY` | your SerpApi key |
| `ADZUNA_APP_ID` | Adzuna app id (optional) |
| `ADZUNA_APP_KEY` | Adzuna key (optional) |
| `JOOBLE_KEY` | Jooble key (optional) |

### 4. Run it once now

Repository → **Actions** tab → enable workflows if asked → **Daily QA job search** → **Run workflow**.
After about 1 minute, open the run: the job list is shown on the run page, and a new issue appears in the **Issues** tab. From then on it runs by itself every day at 8:30 AM IST.

---

## Changing what it looks for

Edit `config.yaml` on GitHub (click the file → pencil icon):

- `max_age_days`: how recent a job must be (default 15).
- `min_years_from` / `min_years_to`: the range the job's minimum experience must fall in (default 4–7).
- `keep_unstated_experience`: set to `true` to also see jobs that don't mention years of experience.
- `keep_unknown_dates`: set to `true` to also see jobs with no posting date. These are often old.
- `min_score`: raise it to get fewer, better matches; lower it to get more.
- `serpapi.queries`: the search phrases, for example add `"insurance QA analyst Bengaluru"`.
- `locations`: add `"hyderabad"` or `"pune"` to include those cities.
- `company_boards`: add companies whose career sites run on Greenhouse, Lever, SmartRecruiters or Ashby. The careers page address tells you which, for example `jobs.lever.co/<name>`.
- `weights`: words that raise or lower the score.

## Where results are kept

- `data/latest.md`: today's new matches as a clickable list.
- `data/matches_YYYY-MM-DD.csv`: each day's new matches.
- `data/all_matches.csv`: every match so far. You can download it, or import it into your tracker sheet.
- `data/seen.json`: jobs already sent, so you never get the same one twice.

## Notes

- LinkedIn and Naukri are reached through Google Jobs, not scraped directly. Scraping those sites breaks their rules and gets blocked quickly.
- If a source fails one day, the others still run, and the day's list says which one failed.
- To get the GitHub notifications on your phone, install the GitHub app and sign in. To get them by email instead, check github.com → Settings → Notifications.
- Email through Gmail is optional. To use it later, add `SMTP_USER`, `SMTP_PASSWORD` (a Gmail app password) and `EMAIL_TO` as secrets.
- GitHub gives private repos 2,000 free Actions minutes a month. This uses about 1 minute a day.
