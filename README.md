# Microsoft Jobs → LinkedIn Post Automation

Every day at 7 AM ET, a GitHub Action fetches Microsoft roles posted in the last 24 hours (with salary for US postings), formats a copy-paste-ready LinkedIn post, and delivers it two ways:

1. **GitHub Issue** in this repo (you get GitHub's email notification automatically)
2. **Email to your inbox** via Gmail SMTP

You copy the post, add your image, and post to LinkedIn manually (auto-posting violates LinkedIn ToS).

## Setup (one time, ~5 minutes)

### 1. Push this code

```bash
cd Linkedln
git init
git add .
git commit -m "Daily Microsoft jobs LinkedIn post automation"
git branch -M main
git remote add origin https://github.com/DandaAkhilReddy/Linkedln.git
git push -u origin main
```

### 2. Add email secrets (repo → Settings → Secrets and variables → Actions → New repository secret)

| Secret | Value |
|---|---|
| `GMAIL_USERNAME` | your Gmail address |
| `GMAIL_APP_PASSWORD` | app password from https://myaccount.google.com/apppasswords (requires 2FA on) |
| `MAIL_TO` | where to receive the post, e.g. you@example.com |

Skip this step if the GitHub Issue notification is enough — the workflow still works (email step is `continue-on-error`).

### 3. Test it

Actions tab → **Daily Microsoft Jobs LinkedIn Post** → **Run workflow**. Within ~2 minutes you should see a new Issue with the formatted post (and an email if secrets are set).

## Configuration

Edit the `env:` block in `.github/workflows/daily-post.yml`:

- `FILTER_COUNTRY` — `United States`, `India`, or `''` for all
- `MAX_JOBS_TOTAL` — jobs covered per day (default 50), `JOBS_PER_POST` — jobs per LinkedIn post (default 1 = one complete post per job)
- `LOOKBACK_HOURS` — posting window (default 24)
- `FILTER_TITLE_KEYWORDS` — e.g. `software engineer,senior` to only include matching titles

Schedule is the `cron` line (`0 11 * * *` = 11:00 UTC = 7 AM EDT). Salary (💰) appears only for US postings — pay-range disclosure law.

## Notes

- Uses the same unauthenticated public API as jobs.careers.microsoft.com — no key needed.
- If a run finds no new jobs, no issue/email is sent that day.
- Each run also uploads `post.txt` as a workflow artifact (backup copy).

## Azure deployment (currently live)

The production automation runs in Azure (resource group `automated-email`):

- `func-linkedin-jobs-26418` — two timers: **7 AM ET** first 50 jobs (software engineers first), **10 AM PT** overflow batch if more than 50
- Posts saved to blob container `linkedin-posts`, emailed via Gmail SMTP
- Code for the function is in `azure/` + `ms_jobs_pipeline.py`

## Set up your own copy (for forks/clones)

1. Fork or clone this repo — there are no secrets in it; everything below is yours to configure.
2. **GitHub Actions path (simplest):** add repo secrets `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD` (from myaccount.google.com/apppasswords, needs 2FA), and `MAIL_TO`. The workflow in `.github/workflows/daily-post.yml` then opens a daily GitHub Issue with the posts and emails them.
3. **Azure path (what powers the original):** deploy `azure/` + `ms_jobs_pipeline.py` to a Python 3.11 Linux consumption Function App, and set app settings `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`, `MAIL_TO` (plus optional `MAX_JOBS_TOTAL`, `JOBS_PER_POST`, `FILTER_COUNTRY`). Four timers send at 7 AM / 12 PM / 5 PM / 9 PM ET.
4. Run tests with `pip install -r requirements.txt pytest azure-functions azure-storage-blob && pytest tests/`.
5. Point it at any Eightfold-powered careers site by changing `BASE` and `DOMAIN` in `ms_jobs_pipeline.py`.
