# Security Review — HOA Violation Manager

Audit date: 2026-11-20. Scope: the application source in `hoa/` (Django 5.2.19, 37 migrations, 393 tests), reviewed
before moving to production on Render + Render PostgreSQL + private S3-compatible storage.

**Nothing in the application was changed and nothing was deployed.** This document, `DATA_STORAGE_PLAN.md`,
`BACKUP_AND_RECOVERY.md` and `PRODUCTION_SECURITY_CHECKLIST.md` are the only things added.

Companion to the earlier production-readiness audit (settings, Python version, hosting); this review goes deeper on
security, permissions, files, audit history and recovery.

---

## 1. How this was checked (and what was not)

| Method | What it covered |
|---|---|
| Reading the source | `accounts.py`, `security_views.py`, `middleware.py`, `signals.py`, every view's access check in `views.py`, `services.py` (workflow + `log()`), `models.py`, `admin.py`, `pdf.py`, `mailing.py`, `zoom.py`, `vantaca.py`, `geocoding.py`, `standards_search.py`, `config/settings.py`, all 37 migrations, all management commands |
| Live probes in the sandbox (throwaway tests outside the repo, SQLite) | Photo upload served back (P1), logout + session replay (P2), link-request throttle behind one shared IP (P3), email change via the admin path (P4), a **role × URL sweep** of every route as anonymous / owner / committee / board with GET and POST (P5), disabled-account session, password-reset enumeration, lockout shape (P6) |
| PostgreSQL 16 in the sandbox | All 37 migrations apply; 393/393 tests pass; length enforcement probe; a dump → encrypt → decrypt → restore → `migrate --check` rehearsal |
| Secret scan | Tracked files and git history searched for credentials, `local.env`, `.secret_key`, `db.sqlite3`: none ever committed |

**Not done / cannot be done here:** no live test of Lob, Zoom, Vantaca, SMTP delivery or the Census geocoder (they have
never been run live); no penetration test with an automated scanner; no load test; no test on Render itself; Render and
storage-vendor plan details are from general knowledge and must be confirmed on their current pages.

---

## 2. What is already working (do not rebuild)

| Area | Evidence |
|---|---|
| Sign-in links are strong | `secrets.token_urlsafe(32)` (256-bit), only the SHA-256 hash is stored, 15-minute expiry, consumed by a **POST** so email scanners cannot use them up, single-use (verified: second POST → 410), refused for disabled users |
| No account discovery on the link page | Same page and same message for known/unknown/throttled emails (existing test + P-checks); only emails already on an active owner record get a link; staff-only logins are given nothing |
| Link goes only to the address on file | Link is emailed to `user.email`; after a Vantaca email change the old address stops getting links (verified) |
| Link requests throttled | 3 per email per hour, 10 per IP per hour (but see SEC-03) |
| Sessions | `HttpOnly`, `SameSite=Lax`; logout is POST-only and the server-side session is destroyed (verified: replaying the old cookie after logout → redirect to login); disabling an account ends its session immediately (verified); staff session 12 h, owner 30 days |
| Passwords | Minimum 10 characters + Django validators; password reset does **not** reveal whether an address exists (verified: identical redirect); reset links expire in 24 h and die once the password changes |
| Lockout | django-axes: 5 failed passwords → locked for 1 hour, per IP + username (existing test) |
| Two-factor | Required (TOTP) for manager, board, committee (and the Django admin, because admin users are managers); homeowners are not forced; owners who also hold a committee/Board role are asked for the code only when they open that role's pages; django-otp throttles guesses and rejects reused codes; open-redirect on the verify page is blocked |
| Permissions | Role × URL sweep (P5): anonymous users are redirected to sign-in on every private URL; owners get 403/404 on every staff, committee and other-owner URL (cases, notices, PDFs, photos, hearings, state JSON); committee gets 403 on all staff-only pages; no URL is reachable by a role that should not have it |
| IDOR | Every per-record view re-checks the record against the user (`owns()` / `can_view()` / token); owners reaching a neighbour's case or photo by changing the number get 403/404 (existing tests + P5). Drafts are invisible to owners |
| Committee scope | Committee sees a case only after the Board has voted on it or it is on a meeting; cannot edit the roster; abstains on their own household's cases |
| SQL injection | No raw SQL, `.extra()`, `.raw()` or cursor use in application code. The one `__iregex` lookup passes a `re.escape`d value as a parameter |
| CSRF | CSRF middleware on; **no** `csrf_exempt` anywhere; every state-changing view requires POST; JSON endpoints are GET-only and read-only |
| XSS (output) | Django autoescaping everywhere; the only `|safe` is the server-generated TOTP QR SVG; data to scripts goes through `json_script`; PDFs escape all text with `xml.sax.saxutils.escape` |
| Clickjacking | `X-Frame-Options: SAMEORIGIN` (verified) |
| Other headers | `X-Content-Type-Options: nosniff` and `Referrer-Policy: strict-origin-when-cross-origin` (verified) |
| Files are not publicly addressable | `config/urls.py` has no `/media/` route; photos and minutes are only reachable through login-checked views; stored names are random (`photos/<32 hex>.ext`) |
| Evidence handling | Photos cannot be edited; the only deletion path is for a **draft** violation, and it is logged |
| Audit trail | 43 `log()` call sites record workflow steps (who, when, what) on each case; sign-in successes/failures are kept by django-axes; Django's admin keeps its own change log |
| Secrets | Read from environment / untracked `local.env`; none in git history; `SECRET_KEY` is mandatory when `DJANGO_DEBUG=0`; sign-in-link emails are not copied into `EmailLog` |
| Letters | No management-company branding on letters; no passwords or tokens in letters |
| Migrations | 37 migrations are all additive (no `RemoveField`, `DeleteModel`, `RunSQL`, `Rename*`); the 3 `RunPython` data steps are safe on an empty or populated database |

---

## 3. Findings

Priority key: **High** = fix before real owners use the site. **Medium** = fix soon after, or before launch if time
allows. **Low** = tidy-ups. Complexity: **S** under half a day, **M** about 1 day, **L** several days.

### HIGH

#### SEC-01 — An uploaded photo can be served back as a web page (stored XSS)
- **What:** `photo_path()` (`models.py:15`) keeps the extension the uploader typed. `_save_photos` checks the *content* is an image but not the extension. A file named `evil.html` that is a valid GIF with `<script>…</script>` appended passes the check and is saved as `photos/<hex>.html`. When opened, `FileResponse` guesses the type from the name and sends `Content-Type: text/html`. **Verified (P1):** uploaded with no login through `/r/<token>/`; served back as `text/html` (`nosniff` does not help when the declared type is HTML).
- **Why it matters:** the script runs on the app's own address, so it can act as whoever opens the photo directly (for example a Board member opening a photo in a new tab).
- **Impact:** account takeover of a staff session in the worst case. Who can plant it: any owner on their own case, or anyone holding an emailed `/r/<token>/` link.
- **Fix:** choose the stored extension from the format Pillow detects (jpg/png/gif/webp), never from the filename; serve photos with an explicit image `Content-Type`, `Content-Disposition: inline` and `Content-Security-Policy: default-src 'none'; sandbox`. Keep the original bytes (evidence), keep EXIF in the file and `taken_at` in the database.
- **Complexity:** S. **Priority:** High.

#### SEC-02 — Production settings fail open
- **What:** `DEBUG` defaults **on** (`DJANGO_DEBUG` unset = debug). Other silent-failure paths: `MFA_REQUIRED_ROLES=none` (used by `start-site.bat`) disables two-factor for everyone with no warning; with no `EMAIL_HOST` the site uses the console email backend, so in production **sign-in links would be written to the log and nobody would receive them**; `ALLOWED_HOSTS` defaults include `testserver`; `SITE_URL` and `DEFAULT_FROM_EMAIL` default to localhost / `example.org`.
- **Why / impact:** one forgotten variable on Render exposes tracebacks and settings, removes 2FA, or leaks sign-in links into logs.
- **Fix:** default `DEBUG` to off (turn on only in `start-site.bat` / `local.env`); when not in debug, refuse to start if `SECRET_KEY`, `ALLOWED_HOSTS`, `SITE_URL`, `DATABASE_URL`, `EMAIL_HOST`, `DEFAULT_FROM_EMAIL` are missing, if `MFA_REQUIRED_ROLES` is empty, or if `ALLOWED_HOSTS` contains `*`. Add `SECURE_REFERRER_POLICY`, `X_FRAME_OPTIONS` review, and start HSTS short (1 hour) then raise.
- **Complexity:** S. **Priority:** High.

#### SEC-03 — Rate limits and lockouts will not see real visitor addresses behind Render
- **What:** the link-request throttle uses `REMOTE_ADDR`; django-axes uses the same. Behind Render's proxy, every visitor looks like one address. **Verified (P3):** with one shared address, only **10** link requests per hour succeed for the whole HOA; the 11th owner sees "check your email" and nothing is sent. The throttle also lives in per-process memory (resets on restart, multiplies per worker).
- **Why / impact:** owners silently unable to sign in during busy periods (a hearing-notice day); lockouts and logs cannot identify an attacker's address; a stranger can lock a named Board member's password login for an hour by guessing (lockout becomes effectively per-username).
- **Fix:** read the client address from `X-Forwarded-For` correctly (Render appends it; configure django-axes' proxy setting and use the same helper in the throttle); use the database cache (`createcachetable`, no new service) so throttles are shared across workers and restarts.
- **Complexity:** S. **Priority:** High.

#### SEC-04 — Long notes and some fields crash on PostgreSQL
- **What:** `Event.text` is `CharField(500)`. SQLite accepts longer text; PostgreSQL rejects it. **Verified:** 600 characters → `DataError: value too long for type character varying(500)`; also `Owner.state` ("Florida"), `Owner.phone` (> 50). `services.log()` writes user-typed notes (dismiss, hold, decline extension, board return, vote notes) into it. `hold_reason` is limited to 300.
- **Impact:** a long note produces an error page and the action is lost, in the legal workflow.
- **Fix:** change `Event.text` to `TextField` (additive migration, no data change); add matching `maxlength` / validation on note inputs and a defensive truncation in `log()` for system-built text.
- **Complexity:** S. **Priority:** High.

#### SEC-05 — Changing an owner's email address is not controlled, verified or audited
- **What:** there is no in-app email-change process. The only manual route is editing the owner in the Django admin. **Verified (P4):** after an admin edit of `Owner.email` — the old address correctly stops receiving new links, **but** (a) the login's email is not updated (no `sync_account` call), so the household ends up with **two active logins**; (b) a link already sent to the old address stays valid for its 15 minutes; (c) existing sessions are not revoked; (d) nothing is verified — a typo sends the next link to a stranger, who gets the household's records; (e) the only trace is Django's generic admin log. Vantaca-driven changes do swap the login email (good) but also leave sessions and outstanding links alone and write no audit entry.
- **Why / impact:** this is the one path by which access to private records changes hands.
- **Fix:** a staff-only "Change sign-in email" action in `services.py` that, in one transaction: validates the address and that nobody else uses it; updates the owner and login together; revokes outstanding `LoginLink`s and the user's other sessions; writes an audit entry (who, when, old → new, reason); emails the **old** address a "your sign-in email was changed" notice; and sends the first link to the **new** address (opening it is the verification). Make the admin Owner form call the same function (or make email read-only in admin).
- **Complexity:** M. **Priority:** High.

#### SEC-06 — Uploaded files would not survive a redeploy and one PDF path breaks on cloud storage
- **What:** photos and minutes are written to the web server's disk (`MEDIA_ROOT`), which is wiped on every Render deploy. `pdf.py:96` uses `ph.image.path`, which does not exist on cloud storage.
- **Fix:** see `DATA_STORAGE_PLAN.md` (private S3-compatible bucket through `django-storages`; read bytes with `.open()` in `pdf.py`).
- **Complexity:** M. **Priority:** High.

#### SEC-07 — No backup or restore plan
- See `BACKUP_AND_RECOVERY.md`. **Complexity:** M. **Priority:** High.

#### SEC-08 — Database connection and credentials not yet wired for PostgreSQL
- **What:** `DATABASES` is hard-coded to SQLite; no `DATABASE_URL`, no TLS option, no `psycopg`.
- **Fix:** read `DATABASE_URL` from a Render secret (never in git); use Render's internal URL between the web service and database; if the external URL is ever used, require `sslmode=require` and restrict allowed IPs; run `migrate` as the deploy pre-command, not at app start; keep the local `.sqlite3` and `local.env` untracked (already true). Tests already pass on PostgreSQL 16.
- **Complexity:** S. **Priority:** High.

### MEDIUM

#### SEC-09 — Admin can destroy case evidence and rewrite accounts without a proper trail
- **What:** managers and Board members are superusers (`STAFF_ADMIN_ACCESS`). In `/admin/` they can delete a Violation, which cascades to its history (`Event`), letters, email copies and photo records; edit `Owner` (name, address, email), `User` and `Profile` (roles); and delete a TOTP device. Admin edits produce Django's change log, but case deletion removes the case's own history.
- **Impact:** a mistaken or malicious click loses a legal record; role/permission changes are unaudited in the app's own history.
- **Fix:** in production remove delete permission for Violation (and any model holding records), make role/email fields change through the audited actions only, put `/admin/` on a non-default path, keep two-factor on. The admin delete exists for test-data cleanup, which should finish before go-live.
- **Complexity:** S–M. **Priority:** Medium.

#### SEC-10 — Audit history covers the case workflow, not accounts or records
- **What:** `Event` is attached to a violation. Not recorded in-app: owner record changes (including Vantaca sync updates to name/address/email), user/role changes, login-email changes, roster changes, who ran an import, meeting create/edit/delete, minutes upload or **replacement**, canned replies. `Event.user` is `SET_NULL`, so a deleted user's actions lose their name. Hearing-date changes, committee decisions, board votes, photo removal and extension decisions *are* logged (checked).
- **Fix:** one small append-only `AuditLog` table (actor name snapshot, time, action, object, old/new values of non-secret fields), written from `services.py` and the sync; no edit/delete in admin; store the actor's username as text as well as the link. Never write passwords, tokens or API keys (current code does not).
- **Complexity:** M. **Priority:** Medium.

#### SEC-11 — Sign-in link lifecycle loose ends
- **Verified (P6):** asking three times leaves three valid links; links already sent are not cancelled when the login's email changes (P4) (a disabled login is refused, which is correct); the confirm step marks the link used *after* reading it (two simultaneous clicks could both sign in); `LoginLink` and `django_session` rows are never cleaned up.
- **Fix:** when issuing a link, mark earlier unused ones used; make the use step atomic (`UPDATE … WHERE used_at IS NULL`, check row count); add cleanup of expired links and `clearsessions` to `daily_jobs`.
- **Complexity:** S. **Priority:** Medium.

#### SEC-12 — The emailed private case link (`/r/<token>/`) has no abuse limits
- **What:** by design anyone holding the link (no login) can read the case summary, post messages and upload photos during the cure period. The token is unguessable (256-bit) and stops working after the cure period. But there is no limit on number of files per request or per day, no total size limit, and no throttle on posts; the link may be forwarded, and stays valid for the whole cure period.
- **Fix:** cap files per request (e.g. 10) and total size, throttle posts per token/IP, tell the user when a file was rejected (currently skipped silently), and keep the page free of third-party resources.
- **Complexity:** S. **Priority:** Medium.

#### SEC-13 — Development shortcuts are present in production
- **What:** `make_login`, `make_test_owner`, `owner_login` and `seed_demo` default to the password `demo-password-123` and have no production guard; `clear_test_data` (needs `--yes`) and `purge_archived_owners` delete records with no "this is production" guard.
- **Fix:** refuse to run when not in debug (unless an explicit `ALLOW_DEV_COMMANDS=1`), require an explicit password with no default, and require typing the environment name for destructive commands.
- **Complexity:** S. **Priority:** Medium.

#### SEC-14 — Two-factor: recovery and enrolment gaps
- **What:** no recovery codes and no documented lost-phone procedure (today: delete the device in the admin); a staff account that has not yet enrolled lets whoever knows its password enrol their own authenticator; `MFA_REQUIRED_ROLES` can be emptied (see SEC-02).
- **Fix:** document the lost-phone procedure (identity check by a second Board member, then reset) in the checklist; create staff accounts and enrol them in the same sitting; optionally require a fresh emailed link before the first enrolment. Homeowners stay on the simple email-link flow (no change).
- **Complexity:** S (process) / M (code). **Priority:** Medium.

#### SEC-15 — Minutes uploads are checked by extension only
- **What:** `.pdf .doc .docx .txt .png .jpg .jpeg`, 10 MB. Content is not checked, the original filename is kept in the storage path, and uploading again replaces the file with no record (the old file stays on disk but the link is lost).
- **Fix:** verify content matches the type (PDF header, zip structure for `.docx`, `Image.verify()` for images), random storage names, log each upload/replacement, and keep the previous file reference in the audit entry.
- **Complexity:** S. **Priority:** Medium.

#### SEC-16 — Vantaca endpoint is not forced to HTTPS
- **What:** `VANTACA_BASE_URL` comes from the environment and is used with HTTP Basic credentials as given. (Lob, Zoom and the Census geocoder are hard-coded to `https://`.)
- **Fix:** refuse a non-`https://` base URL. **Complexity:** S. **Priority:** Medium.

#### SEC-17 — Session hygiene
- **What:** owner sessions last 30 days and cannot be ended centrally; authenticated pages are sent without `Cache-Control: no-store` (verified), so on a shared computer the Back button after logout can show a cached page; no "sign out everywhere".
- **Fix:** `never_cache` on private views (a decorator, not new middleware), a staff action to delete a user's sessions (used by SEC-05), keep 30 days for owners (your stated experience).
- **Complexity:** S. **Priority:** Medium.

#### SEC-18 — Email delivery settings need hardening
- **What:** only STARTTLS on port 587 is supported (no SSL/465 option); no `EMAIL_TIMEOUT` (a stalled mail server blocks a worker); `EmailLog` stores full bodies of owner emails, which include the `/r/<token>/` link (staff-visible only).
- **Fix:** add `EMAIL_TIMEOUT=10` and an SSL option; use a transactional provider with SPF/DKIM/DMARC on the HOA's domain (sign-in links depend on delivery); decide how long to keep email bodies.
- **Complexity:** S. **Priority:** Medium.

### LOW

| ID | Issue | Fix | Cx |
|---|---|---|---|
| SEC-19 | **Timing hint on the link page.** Same message for all emails, but a known email sends mail synchronously and answers more slowly than an unknown one. | Send after the response / in a thread, or add a short fixed delay. | S |
| SEC-20 | **Links built from the request's Host header** (`send_signin_link` uses `request.build_absolute_uri`). Safe only while `ALLOWED_HOSTS` is strict. | Always use `SITE_URL`. | S |
| SEC-21 | **Password-reset requests are not rate limited** (email bombing a known address). | Reuse the link throttle. | S |
| SEC-22 | **Malformed input gives a 500 instead of a message** (staff only): non-numeric `id`/`year` on the roster and replies pages; POST to a case with no meeting on `committee_case_record` / `results_review` → `NoReverseMatch` (verified P5). | Validate and redirect with a message. | S |
| SEC-23 | **Leaflet loaded from cdnjs without an integrity hash** (map page, staff only). Map tiles are fetched from a third-party tile server and reveal only map coordinates viewed. | Add `integrity` + `crossorigin`, or host Leaflet locally. | S |
| SEC-24 | **No custom 403/404/500 pages.** Django's defaults leak nothing with debug off. `/photos/<id>/` returns 403 vs 404 depending on whether the id exists. | Simple branded pages; return 404 for both. | S |
| SEC-25 | **Anthropic "Ask AI" cost/privacy:** sends only the rule catalogue and the staff member's typed description (≤1000 chars); staff-only; not rate limited. | Hint under the box: "don't type names or addresses"; optional daily cap. | S |
| SEC-26 | **Lob form fields are built by string concatenation**; an owner name containing quotes/line breaks could corrupt the request. | Strip control characters from address fields. | S |
| SEC-27 | **Tokens appear in request paths** (`/accounts/link/<token>/`, `/r/<token>/`), so they would be in any web-server access log. | Do not enable gunicorn access logs; check Render's log settings; never log full URLs. | S |
| SEC-28 | **Duplicate-household edge case:** `household_for_email` uses `.first()` with no ordering if the same email is on two active owners. | Detect and flag for staff instead of picking one. | S |

---

## 4. Integrations (item 8)

| Integration | How it is protected today | Gaps |
|---|---|---|
| SMTP | Credentials only from environment; TLS on 587 | SEC-18; sign-in links must not fall back to console (SEC-02) |
| Lob | `https://api.lob.com`; key from environment (use a **test** key first); sends name, mailing address and the letter PDF only — necessary data | Never run live; SEC-26 |
| Zoom (Server-to-Server OAuth) | HTTPS; secret in environment/`local.env`; token cached in memory only; waiting room and passcode on by default | Never run live; keep OAuth scopes to meeting create/update/delete only |
| Vantaca | Read-only into the app; HTTP Basic from environment; refuses empty/partial feeds before deactivating anyone | SEC-16; API shape unconfirmed; never run live |
| Anthropic | Key from environment; off when blank; only rule list + typed description sent | SEC-25 |
| US Census geocoder | HTTPS; sends street/city/state/ZIP only — no names | None |
| Maps | Leaflet from cdnjs + a tile server; staff only | SEC-23 |

Do not disturb working integrations: none of the fixes above touches Lob, Zoom or Vantaca request logic except the
https check for Vantaca.

---

## 5. Recommended implementation sequence

Phases are ordered so each can be tested and pushed on its own, with the existing 393 tests (plus the new tests in
section 6) green at every step. **Nothing starts until you approve.**

| Phase | Contents | Effort |
|---|---|---|
| 1 — Safe defaults | SEC-02, SEC-08, SEC-04, SEC-13, static files, `DATABASE_URL`, WhiteNoise/gunicorn requirements | ~1 day |
| 2 — Close the XSS and sign-in issues | SEC-01, SEC-03, SEC-11, SEC-12, SEC-20, SEC-21 | ~1 day |
| 3 — Files | SEC-06, SEC-15 (see `DATA_STORAGE_PLAN.md`) | ~1 day |
| 4 — Accounts and audit | SEC-05, SEC-10, SEC-09, SEC-14, SEC-17 | ~2 days |
| 5 — Integrations and polish | SEC-16, SEC-18, SEC-19, SEC-22 – SEC-28 | ~½ day |
| 6 — Render staging + backups | `BACKUP_AND_RECOVERY.md`, the checklist, restore rehearsal | ~1 day |
| 7 — Soft launch | Board only, then committee, then owners | — |

About 6–7 working days in total, spread over the phases, plus waiting on provider choices.

**Decisions I need from you** (none block the audit): which storage vendor (recommended in `DATA_STORAGE_PLAN.md`);
which email provider; whether production starts with a fresh database (recommended) or carries test data; who holds
the backup decryption key and the lost-phone approval role; how long email bodies and photos must be kept (ask the
association's attorney).

---

## 6. Recommended additional tests (keep all existing tests)

Existing coverage already includes: privileged roles must enrol 2FA, owners not forced, open-redirect, lockout,
identical get-access answers, throttle, deactivated users, expired and used links, one owner cannot open another's
notice or a draft, households isolated, residents cannot change their info, private link only during cure.

New tests to add, grouped by area (run on SQLite and on PostgreSQL where marked **PG**):

| Area | Test |
|---|---|
| Files | Upload named `x.html` with image content: stored extension is the detected image type and the response `Content-Type` is an image (SEC-01); file-count and total-size caps; rejected file produces a visible message; minutes content/type mismatch refused |
| Authentication | Asking for a second link cancels the first; two simultaneous uses of one link sign in once; link for an email changed afterwards stops working; throttle uses the forwarded address and is shared across workers |
| Sessions | Logout invalidates the session (a replay test); disabling a user ends their session; "revoke sessions" empties them; private pages send `no-store` |
| Permissions | Parametrised **role × URL sweep** (the P5 probe turned into a permanent test) so a newly added view with no guard fails the build; committee cannot open a case before the Board vote; committee cannot post to the roster |
| Email change | Only staff can run it; writes an audit entry; revokes links and sessions; rejects a duplicate address; old address receives the notice |
| Database | **PG:** a 600-character note saves (SEC-04); the full suite on PostgreSQL in CI; migration applies on a copy containing data; deleting an owner with violations is refused |
| Settings | Starting with `DJANGO_DEBUG=0` and a missing required variable fails loudly (SEC-02) |
| Audit | Role change, owner email change, roster change, upload/replace each add an audit row; no edit/delete in admin |
| Backup | A scripted dump → restore into a scratch database → `migrate --check` → row-count comparison (rehearsed once here; see `BACKUP_AND_RECOVERY.md`) |

A GitHub Actions workflow that runs the suite against a PostgreSQL service container is free and is the simplest way to
keep "tests also pass on PostgreSQL" true.

---

## 7. Evidence log (for reference)

- PostgreSQL 16: 37/37 migrations applied; 393 passed (84 s); `Event.text` 600 chars → `DataError`.
- P1: unauthenticated upload of `evil.html` (GIF + script) accepted, stored as `photos/<hex>.html`, served `text/html`.
- P2: cookie `HttpOnly`, `SameSite=Lax`; `GET /accounts/logout/` → 405; old session cookie after logout → 302 to login.
- P3: 14 owners behind one address → 10 links sent.
- P4: after admin email change → old address no link; new address link; 2 active logins in the household; link to old address still valid.
- P5: full URL × role sweep, no unexpected 200s; two staff-only 500s (SEC-22).
- P6: disabled account's session → 302; password-reset responses identical for known/unknown; three requests → three valid links.
- Backup rehearsal: `pg_dump -Fc` → `age` and `gpg` encrypt/decrypt round-trips → `pg_restore` → `migrate --check` clean.
