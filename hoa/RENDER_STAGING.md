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
- Start Command: `gunicorn -c gunicorn.conf.py --access-logfile /dev/null config.wsgi:application`  (the two extra options switch the web-server access log OFF; Render sets its own logging option that would otherwise turn it on and write every sign-in link into the log)
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


## Found on the first real deploy (2026-11-20)
- The site came up: build, static files, all 43 migrations and the health check passed on the first full attempt once ALLOWED_HOSTS / SITE_URL / CSRF_TRUSTED_ORIGINS were added (the first attempt failed ON PURPOSE, only for those two).
- The web-server access log was ON in Render's log even though `gunicorn.conf.py` turns it off: Render's own settings override the file. Fixed by putting `--access-logfile /dev/null` in the start command (verified in the sandbox with Render's variable simulated: without the flag the secret in a sign-in address was logged, with it nothing was).

## Added 2026-11-21: load the violation list first, and a trial run of the certified-mail screens
- A fresh database has NO violation types (no migration creates them). After the first deploy and before creating test violations, open the **Shell** and run `python manage.py load_standards` (it prints "N created, M updated" twice, once per list; safe to run again).
- Trial run on fake data (Pat Testowner, 101 Staging Test Lane): New violation with the **Point-in-time** box ticked and a photo (it goes straight to the Board queue; the fine box is pre-filled with 100.00) -> Board queue: tick the row, tick the majority box, "Record decisions" -> Committee meetings: create a meeting about 3 weeks ahead (untick "Create the Zoom meeting for me", type a location) -> "Assign selected" -> open the case: "Mailing log and print copies" lists the certified and first-class letters to mail. Then follow the checklist "Certified mail and first-class mail for the Notice of Hearing" at the end of TESTING.md. Emails are not sent on staging: they are printed in the **Logs** tab.

## 10. Lob TEST key on staging (added 2026-11-21; nothing is mailed and nothing is charged)
Do this only when you are ready; no Lob account is needed before that. Lob has never been run from this app, so this is the first real check of it.
1. Create a free account at lob.com (Lob says its Developer plan has no monthly fee).
2. In Lob's dashboard, under **Settings**, find the API keys and copy the **Test** secret key. It starts with `test_`. Never paste it into chat or GitHub.
3. In Render, on the `hoa-staging` page, open **Environment** and add these (mark the key as secret):
   - `MAIL_PROVIDER` = `lob`
   - `LOB_API_KEY` = the test key
   - `HOA_RETURN_NAME` = `Heron Landing HOA` (or the name you want above the return address on the envelope; Lob allows 40 characters)
   - `HOA_RETURN_LINE1` = any made-up street address for now (later: your virtual mailbox address, including the box or suite number)
   - `HOA_RETURN_ZIP` = for example `34236` (city and state default to Sarasota, FL)
4. Save and let it redeploy. A site marked STAGING refuses to start or to send with a live key (`live_...`); the live site refuses a test key.
5. In the Shell run `python manage.py check_lob --ping`. Expected: "a TEST key ... NOTHING IS MAILED", "Lob answered HTTP 200: it accepts the key", "All checks passed". It sends no letter; the ping is a free read-only question.
6. Run the trial again (send a fake case's hearing notice to a meeting). On the case page, **Mailing log** should now list a certified and a first-class letter per address, each handed to Lob, or show Lob's own reason if Lob refused it. The test letters appear in Lob's dashboard and are not mailed.
7. Test mode has NO USPS tracking events, so "returned" and "not delivered" cannot be proven here; they need a few real certified letters later, each about $8, and only with the owner's explicit approval.

**Note 2026-11-22:** held mailed copies now go out only from `daily_jobs` (or the Mail now button on the case), never by opening a page, so scheduling `daily_jobs` matters more than it did. Until it is scheduled the dashboard shows "N mailed copies are past the wait and have not gone out yet" (nothing is mailed on staging in any case).

**Note 2026-11-23:** the letters for the print service are now queued and handed over just after the page answers (so "Send to committee" is quick even when Lob is slow). A letter still waiting after 15 minutes shows on the dashboard ("waiting more than 15 minutes to go to the print service"); the **Mail now** button on the case, or the next daily job, sends it. This update needs `migrate` (the pre-deploy command above does it for you).

## 11. Check that email works (added 2026-11-23; nothing is sent unless you give an address)
1. In the Render Shell run `python manage.py check_email`. It shows how emails are sent (never a password or user name, only whether they are set), connects to the mail server and logs in, and says "Nothing was sent". On staging you will normally see "Emails are only PRINTED, not sent (this is a staging site)": that is correct and safe.
2. When a real mail service (Postmark) is set up, run `python manage.py check_email --to you@example.com` with your own address. It sends ONE test message. "The mail server ACCEPTED the message" means the service took it; it does not prove it reached your inbox, so look in the inbox and the spam folder, and if it never arrives look at the mail service's own activity page.
3. If it fails, the message says why in plain words (wrong login, From address not verified with the service, address refused, server not reachable, wrong port or secure-connection setting). Fix the setting named and run it again.


**Note 2026-11-25 (read this before deploying):** this guide is a PLAN. Nothing in this file proves that a Render service exists or has these settings; the assistant cannot see your Render account. Before pressing Deploy, open the service's Settings in Render and check: **Branch** is `claude/brave-galileo-mddme9`, **Root Directory** is `hoa`, the **Pre-Deploy Command** is `python manage.py migrate --no-input` (without it the new database change, migration 0046 for the committee reminder, and 0045 before it, are never applied and the site shows an error page on the first page that needs them), and **Auto-Deploy** is No. A chat reply on 2026-11-23/24 said Render was "already set to this branch"; that was an overstatement, corrected here.
