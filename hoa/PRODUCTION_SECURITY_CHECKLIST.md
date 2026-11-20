# Production Security Checklist

Audit date: 2026-11-20. Use this to set up and verify the production site. It is a checklist, not a build plan: items
marked **(code change)** need the fixes listed in `SECURITY_REVIEW.md` (which wait for your approval). Items marked
**✔ already true** were verified in this audit.

Environments: **development** (your PC: SQLite, `local.env`, `start-site.bat`), **staging** (a second Render service +
its own small database and bucket, fake or test-mode integrations), **production** (the real site). They must never
share a database, bucket, secret key or API key.

---

## A. Before the first deploy

### A1. Source control
- [ ] ✔ `local.env`, `.secret_key`, `db.sqlite3`, `media/` are git-ignored and were never committed (history scanned).
- [ ] Turn on GitHub secret scanning / push protection for the repository.
- [ ] Protect the working branch (no force-push); deploy only from a reviewed branch.
- [ ] Dependencies pinned to exact versions in `requirements.txt` (code change).
- [ ] CI runs the test suite on SQLite **and** PostgreSQL on every push (code change, free).

### A2. Environment variables (Render → Environment; mark secrets as secret)
"Exists" = the application already reads it; "New" = needs the code change.

| Variable | Value for production | Exists |
|---|---|---|
| `DJANGO_DEBUG` | `0` (and the default should become off) | Yes |
| `SECRET_KEY` | 50+ random characters, unique to production | Yes |
| `ALLOWED_HOSTS` | only the real domain(s); no `*`, no `testserver` | Yes |
| `CSRF_TRUSTED_ORIGINS` | `https://your-domain` | Yes |
| `SITE_URL` | `https://your-domain` (used in every link in every email) | Yes |
| `DATABASE_URL` | Render **internal** database URL (secret) | New |
| `PYTHON_VERSION` | 3.13.x (Django 5.2's supported range; your PC's 3.15 is newer than that) | Render |
| `MFA_REQUIRED_ROLES` | **leave unset** (default = manager, board, committee). Never `none` | Yes |
| `STAFF_ADMIN_ACCESS` | decide deliberately (default on) | Yes |
| `SESSION_HOURS` | `12` (staff). Owners keep 30 days | Yes |
| `SECURE_SSL_REDIRECT` | leave default (on) | Yes |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | the transactional provider; a From address on the HOA's own domain | Yes |
| `LOB_API_KEY` | **test** key until the first printed sample is checked, then live | Yes |
| `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `ZOOM_HOST_EMAIL` | production Zoom app | Yes |
| `VANTACA_BASE_URL` (must be `https://`), `VANTACA_USER`, `VANTACA_PASSWORD`, `VANTACA_OWNERS_PATH` | only after Vantaca confirms the API | Yes |
| `ANTHROPIC_API_KEY` | optional; blank hides the "Ask AI" button | Yes |
| `HOA_*` letterhead, return address, phone, `ATTORNEY_EMAIL` | HOA values only (no management-company name) | Yes |
| `STORAGE_BUCKET`, `STORAGE_ENDPOINT_URL`, `STORAGE_REGION`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY` | app key with **no delete** (`DATA_STORAGE_PLAN.md`) | New |
| `BACKUP_PUBLIC_KEY`, backup bucket keys | for the backup job only | New |

- [ ] None of these values appear in git, in screenshots, in chat, or in the Render build log.
- [ ] The staging environment uses different values for **every** secret above.

### A3. Django settings to confirm (code change where noted)
- [ ] `DEBUG` is off by default; startup refuses to continue when a required variable is missing (SEC-02).
- [ ] `python manage.py check --deploy` with production variables reports **no** warnings (run it in staging).
- [ ] ✔ `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, SSL redirect and `SECURE_PROXY_SSL_HEADER` (Render sends
      `X-Forwarded-Proto`) are set when debug is off.
- [ ] HSTS starts at 1 hour, then 1 week, then 1 year once HTTPS is proven everywhere.
- [ ] Static files served (WhiteNoise + `collectstatic`) — otherwise the admin has no styling.
- [ ] A `/healthz` URL exists for Render's health check and is exempt from the HTTPS redirect.
- [ ] Client IP read from `X-Forwarded-For` for the sign-in throttle and django-axes (SEC-03).
- [ ] Database cache table created (`createcachetable`) so throttles are shared (SEC-03).
- [ ] No gunicorn access log that prints full paths (tokens are in some URLs, SEC-27).
- [ ] `/admin/` moved off the default path, delete permission removed from Violation (SEC-09).

### A4. Database (Render PostgreSQL)
- [ ] Paid plan (free databases expire and have no backups).
- [ ] Same region as the web service; the web service uses the **internal** URL.
- [ ] If external access is ever needed: allowed-IP list restricted to named addresses and `sslmode=require`.
- [ ] `migrate` runs as the deploy pre-command, **not** at application start.
- [ ] Take a manual dump before every deploy that contains a migration (`BACKUP_AND_RECOVERY.md` R5).
- [ ] ✔ All 37 migrations are additive; the full test suite passes on PostgreSQL 16.
- [ ] `Event.text` changed to `TextField` and long-note test passes on PostgreSQL (SEC-04).
- [ ] Development commands refuse to run in production; no `demo-password-123` logins exist (SEC-13).

### A5. Storage
- [ ] Private bucket; no public access; versioning on; app key cannot delete.
- [ ] Photo extension comes from detected type and photos are served with an image content type + sandbox CSP (SEC-01).
- [ ] `pdf.py` reads photo bytes instead of `.path` (SEC-06).
- [ ] Upload test: add a photo, redeploy, open it as Board and as the owner.
- [ ] Prove the app key cannot delete an object.

---

## B. Accounts and access

### B1. First staff accounts
- [ ] Create the manager/Board logins in the admin; each person signs in with their password **and enrols the
      authenticator app in the same sitting** (do not leave an unenrolled staff account sitting idle).
- [ ] Write the lost-phone procedure: a second Board member confirms identity by phone, then the manager deletes the
      old authenticator in the admin and the person re-enrols.
- [ ] Delete or deactivate any test/demo accounts. ✔ Homeowner passwords: none required; the emailed link is the default.
- [ ] Decide who is Board vs. manager (they are identical by design) and keep the list on the recovery sheet.

### B2. Owners and email changes
- [ ] ✔ Owners cannot edit their own name, address or email (read-only portal).
- [ ] Email changes go through the staff "Change sign-in email" action (SEC-05): audit entry, old address notified,
      sessions and links revoked, first link sent to the new address.
- [ ] Until that action exists: do **not** edit an owner's email in the admin; change it in Vantaca and run the sync.

### B3. Smoke tests (do these in staging, repeat in production before announcing)
- [ ] Owner requests a link: email arrives within a minute, one click signs in, second click on the same link fails.
- [ ] A link older than 15 minutes fails. An unknown address gets the same page and no email.
- [ ] Owner logs out; the Back button does not show their case; the old session does not work.
- [ ] Owner A cannot open owner B's case, photo, notice PDF or hearing by editing the number in the address bar.
- [ ] Committee member sees only Board-approved/meeting cases; cannot open the Board queue or the roster editor.
- [ ] Board member must pass the authenticator step; five bad passwords lock the account for an hour.
- [ ] Deactivated user's open session stops working.

---

## C. Integrations (all have **never run live**; do these one at a time in staging)

- [ ] **Email:** SPF, DKIM and DMARC set on the sending domain; send a link, a notice and a reminder to three real
      mailboxes (including one that is strict about spam); check the From name shows the HOA.
- [ ] **Lob:** test key → one sample letter checked by a Board member (letterhead has no management company; "On Behalf
      of the Board of Directors") → live key. Confirm certified-mail tracking arrives through `daily_jobs`.
- [ ] **Zoom:** create a test hearing; waiting room on, passcode present; the link in the owner's notice works.
- [ ] **Vantaca:** `VANTACA_BASE_URL` is `https://`; run the first sync on staging with a **copy** of the owner list; check
      counts, and that the "partial or empty feed" guard stops a bad file.
- [ ] **Anthropic (optional):** tell users not to type owner names or addresses into the Ask-AI box.
- [ ] **Census geocoder / map:** run once; only addresses (no names) are sent.

---

## D. Backups (see `BACKUP_AND_RECOVERY.md`)

- [ ] Nightly encrypted database dump to the second vendor.
- [ ] Private backup key stored in two places; **a restore using the stored key was performed before launch**.
- [ ] Nightly file copy (copy, never sync) to the second vendor.
- [ ] Failure alerts and a heartbeat monitor are active and have been tested by deliberately breaking one backup.
- [ ] `daily_jobs` scheduled as a Render cron job with its own heartbeat.
- [ ] Recovery sheet written and stored with a second Board officer.

---

## E. Go-live

- [ ] Staging fully passed sections A–D.
- [ ] Production database created empty → `migrate` → `load_standards` → owner import from Vantaca → staff logins.
- [ ] HTTPS certificate valid; `http://` redirects to `https://`; HSTS short.
- [ ] Soft launch: Board only for a week, then committee, then owners in groups.
- [ ] First week: watch the error log daily; confirm each daily job ran.

---

## F. Ongoing

| When | What |
|---|---|
| Daily (automatic) | `daily_jobs`, nightly dump, nightly file copy, backup heartbeat |
| Weekly | Skim error log; confirm the last backup date |
| Monthly | Review who has Board/manager/committee access; remove anyone who left; check storage and email costs; update dependencies with a security fix; confirm the automated "backup is readable" check is green |
| Quarterly | Restore test (scratch database + 5 photos); rotate integration keys if anyone with access left; check the audit log for unexpected role or email changes |
| Yearly | Full rebuild drill; renew domain; review retention periods with the attorney; update the committee roster (the app switches committee logins on and off with the roster); re-confirm lost-phone procedure |
| After any incident | Rotate `SECRET_KEY` (signs everyone out), integration keys and storage keys; restore from backup if data was altered; write down what happened |

---

## G. Known open items carried from the audit

High: SEC-01, SEC-02, SEC-03, SEC-04, SEC-05, SEC-06, SEC-07, SEC-08. Medium: SEC-09 – SEC-18. Low: SEC-19 – SEC-28.
Full detail, impact, fix and complexity for each are in `SECURITY_REVIEW.md`.


---

## Addendum (Part A)
- [ ] Set `STORAGE_BUCKET` (+ `STORAGE_ENDPOINT_URL`, `STORAGE_REGION`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`) or `MEDIA_ROOT` (persistent disk): the live site will not start without one. The saved copy of every letter lives there.
- [ ] Upload test in staging: send a test notice, open its saved copy, redeploy, open it again (fingerprint must still match).
- [ ] `SUSPENSION_ENABLED` is not set (suspension is hidden).
- [ ] The saved-letters prefix is in the nightly file copy to the second vendor.

- [ ] Site-visit observations: after the first staging deploy, add one with a photo, redeploy, open the photo again (fingerprint must still match); confirm an owner login sees nothing about it anywhere; confirm `site-visits/` is in the nightly file copy.

---

## Addendum: email sending service = Postmark (decided; not yet set up)
- [ ] Postmark account created; the SMTP settings go in `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` (Render environment / `hoa/local.env`). The token is never in chat, git, screenshots or the build log. Staging uses a different Postmark server/token from production.
- [ ] Confirm Postmark's current prices, limits and SMTP details on their site before setup (not verified).
- [ ] Sending domain chosen and its DNS controlled by the Association (Cloudflare likely). Postmark's SPF and DKIM records added and shown as verified; DMARC added. Free Cloudflare Email Routing set up for replies.
- [ ] Postmark has never run live with this app. Before any real notice: test sign-in link, notice and reminder to several real mailboxes, including a strict one; check the From name shows the HOA.
- [ ] Prove a refused or failed send shows as a failure in the app (for example a wrong token or a rejected address) and does not appear as sent.
- [ ] The record of what was sent is the app's saved letter copies and email records, not Postmark's delivery log (limited retention, not ours).
- [ ] Postmark and DNS logins added to the recovery sheet.

- [ ] Minutes uploads: after the first staging deploy upload a PDF and a .docx, redeploy, download both again (fingerprint must still match); confirm `minutes/` is in the nightly file copy; confirm a homeowner login cannot open the download address.
- [ ] `ARCHIVE_RETENTION_YEARS` is 7 (the default); nothing is purged by hand before then.
