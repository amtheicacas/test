# Render staging site: step by step

Staging is a second, live-style copy of the site used ONLY for testing with FAKE data. It shows a red STAGING banner, prints its emails in
Render's log instead of sending them, and never has the live bucket, live keys or real owners. Production comes later and is separate.

**Status:** nothing here has been done yet. I could not read Render's documentation from my environment, so the click-by-click steps
below are from general knowledge and Render's screens may be worded differently. Send a screenshot whenever one does not match.

## What it costs (confirm on Render's pricing page; see RUNNING_COSTS.md)
About $7 a month for the web service (Starter) plus about $6 a month for the smallest database, roughly $13. You can delete both when
testing is finished. Render asks for a payment method before it creates paid services: use the Association's card or virtual card.
(A free database is deleted after 30 days and has no backups, so it is not used for anything we want to keep.)

## 1. Account
1. Sign up at render.com with the Association's shared Board email (not a personal one). Turn on two-step login.
2. Add the Association's payment method when Render asks.
3. Connect GitHub when asked, and allow access to the repository `amtheicacas/test` only.

## 2. The database
New + -> **PostgreSQL**. Name `hoa-staging-db`, region the same as the website will use (pick the same one both times; US East is closest
to Sarasota), PostgreSQL version 16, the smallest paid plan. Create it. Then copy the **Internal Database URL** (not the External one): it
starts `postgresql://` and works only inside Render. Keep it in Notepad for step 4.

## 3. The website
New + -> **Web Service** -> the repository `amtheicacas/test`.
- Name: `hoa-staging`
- Branch: `claude/brave-galileo-mddme9`
- Root Directory: `hoa`
- Runtime: Python 3
- Build Command: `./build.sh`
- Start Command: `gunicorn config.wsgi:application`
- Instance type: Starter, same region as the database
- Health Check Path: `/healthz`
- Pre-Deploy Command: `python manage.py migrate --no-input` (this runs the database changes before each new version goes live)
- Auto-Deploy: **No** (you press Deploy when you want a new version; nothing changes under you)

## 4. Environment variables (Environment tab; mark secrets as secret)
| Name | Value |
|---|---|
| `PYTHON_VERSION` | `3.13.5` (any 3.13.x; the site is not tested on 3.15) |
| `SECRET_KEY` | a new random value: on your PC run `python -c "import secrets; print(secrets.token_urlsafe(50))"` and paste the result straight into Render. Never reuse your PC's key and never paste it in chat |
| `DATABASE_URL` | the Internal Database URL from step 2 |
| `ALLOWED_HOSTS` | the site's address without https, e.g. `hoa-staging.onrender.com` (shown at the top of the service page) |
| `SITE_URL` | `https://hoa-staging.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://hoa-staging.onrender.com` |
| `STAGING` | `1` |
| `EMAIL_TO_LOG` | `1` (emails are printed in the Logs tab, nothing is sent) |
| `DEFAULT_FROM_EMAIL` | `violations@heronlanding.org` (not used while emails are only logged) |
| `ADMIN_URL` | something not guessable, e.g. `staging-tools-7k2/` |
| `STORAGE_BUCKET`, `STORAGE_ENDPOINT_URL`, `STORAGE_REGION`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY` | the **TEST** Backblaze bucket (`heron-landing-test-files`) and its key: never the live bucket |
Leave UNSET on staging: `MFA_REQUIRED_ROLES` (two-step stays on), `LOB_API_KEY`, `ZOOM_*`, `VANTACA_*`, `ANTHROPIC_API_KEY`, `BACKUP_*`,
`EMAIL_HOST`. Staging has no way to send a real email, mail a real letter or reach real owners.

## 5. First deploy
Create the service (it starts deploying). Watch the **Logs** tab: the build installs, collects static files, runs the migration, then
starts. If the site refuses to start, the log lists exactly which setting is missing. When it says live, open
`https://hoa-staging.onrender.com/healthz`: it should show just `ok`.

## 6. The first staff login
On the service page open **Shell** (a paid-plan feature) and run:
`python manage.py createsuperuser`  (username, email, a long password). Sign in at `/accounts/login/`; the site makes you set up the
authenticator app on the spot. This login acts as a manager. Then, from the staff menu, create the other staff logins in the admin at your
`ADMIN_URL` and enrol each one in the same sitting.

## 7. Fake owners
Staff menu -> **Import owners** -> upload `staging-test-owners.csv` (six clearly fake owners on "Staging Test Lane"). To test an owner's
sign-in: open `/accounts/get-access/`, ask for a link for `test.owner1@example.com`, then open the **Logs** tab and copy the link that was
printed. Create a few fake violations and push them through the whole process, including committee meetings and minutes (use fake
Zoom/location text).

## 8. What to check on staging (from PRODUCTION_SECURITY_CHECKLIST.md section B3)
Owner link works once and a second click fails; an owner cannot open another owner's case or photo by changing the number; the committee
cannot reach the Board queue; two-step is demanded for staff; sign out then Back shows nothing; upload a photo, redeploy, the photo is
still there; `/healthz` is `ok`; a bad address shows the branded page; `python manage.py check --deploy` has no new warnings
(three known, deliberate ones: HSTS subdomains, HSTS preload, and `X_FRAME_OPTIONS` which is SAMEORIGIN so letters can show in the page).

## 9. Not done yet (later steps)
Scheduled jobs (`daily_jobs`, `backup_files`, `backup_database` as Render cron jobs with a heartbeat), a separate staging backup bucket, a
`render.yaml` blueprint (so the whole setup can be recreated from one file), Postmark, the real domain, monitoring, and the production
service. Everything on staging uses test data and the TEST bucket only.
