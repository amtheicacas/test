# CLAUDE.md: Heron Landing HOA Violation Manager

Django 5.2 app (folder `hoa/`) for Heron Landing HOA (Sarasota FL): violation notices, owner portal, Board queue, Compliance Committee hearings/minutes, fines, certified mail, Zoom. **Read `PROJECT_STATUS.md` first** (architecture, schema, decisions, open items, where we left off). `README.md` has feature docs (some sections predate later changes; see PROJECT_STATUS.md "Doc drift"). `STATUSES.md` is the regenerated status/label table; `TESTING.md` is the manual test checklist.

## Run / test (Linux sandbox)
- venv: `/tmp/claude-0/venv/bin/python` (recreate if gone: `pip install -r requirements.txt pytest pytest-django`)
- Tests: `/tmp/claude-0/venv/bin/python -m pytest -q` (~70 s, 462 tests, must all pass before every commit; they run on `config.test_settings`, i.e. as a development site). Checks: `manage.py check`, `manage.py makemigrations --check --dry-run`.
- Local: `start-site.bat` (Windows, user's PC; runs `migrate`, sets `DJANGO_DEBUG=1` and `MFA_REQUIRED_ROLES=none`). **A site is LIVE unless `DJANGO_DEBUG=1`**: a live site refuses to start without real SECRET_KEY / ALLOWED_HOSTS / SITE_URL (https) / DATABASE_URL / EMAIL_HOST / DEFAULT_FROM_EMAIL and with two-step login on (`config/deploy.py`). On the user's PC, commands run in a terminal need `DJANGO_DEBUG=1` in `local.env`. Live installs use `requirements-production.txt`. Secrets go in untracked `hoa/local.env` (see `local.env.example`).
- Git: develop on branch `claude/brave-galileo-mddme9`, commit + `git push -q origin <branch>`; end commit messages with the Co-Authored-By / Claude-Session trailers from the system reminder. Never create a PR unless asked. **Chain commands with `&&`, not `;`, so a failing test never gets pushed.**

## Architecture (all in `violations/`)
`models.py` data; `services.py` ALL workflow transitions and rules (raise `WorkflowError`; views stay thin); `views.py`+`urls.py`; `progress.py` owner/staff trackers + sentences; `pdf.py` letters (reportlab); `minutes.py` committee minutes; `portal.py` owner tile registry; `accounts.py` roles/logins/magic links; `mailing.py` Lob; `zoom.py`; `vantaca.py` import/sync; `ledger.py`; `geocoding.py`; `standards_search.py`. Templates in `violations/templates/violations/`. Tests in `violations/tests/`.

## Rules to keep
- **Roles:** manager and board are IDENTICAL (same access, incl. admin). `committee` = roster-driven. `homeowner` = owner login. One person can be owner + committee/board on ONE login (`Profile.owner` set; `accounts.owner_of`); owner pages must check `owns(user, v)`, never `can_view`.
- **Owners never edit their own info** (name/email/address come from Vantaca; read-only portal). Letters never show the management company (HOA letterhead, "On Behalf of the Board of Directors").
- **Windows:** the user runs this on Windows. Never use `strftime("%-d")`; use `short_date()` / `f"{d:%b} {d.day}"`. Keep paths/encodings portable.
- **Never send anything automatically to outside parties that wasn't asked for:** attorney email is a reviewed draft; owner emails/letters follow the workflow only.
- Fines: Declaration 8.1 and FS 720.305: $100/violation, continuing violation $100/day up to $1,000; suspension max 60 days (not offered; no amenities); fine due 30 days after delivery (+5 mail buffer); hearing needs 14 days' notice (warn under 20).
- Change behavior in `services.py`, add/adjust tests, regenerate `STATUSES.md` with `manage.py print_statuses` if statuses/labels change. All user-visible wording belongs in one place (`progress.py`, `Violation.PORTAL_LABELS`, status choices).
- New model fields need a migration (`makemigrations`); data fixes go in migrations with `RunPython`.
- Integrations (Lob, Zoom, Vantaca API/ledger, Census geocoder) are written to documented shapes but **never run live**: say so when touching them.
- Don't modify docs by deleting; append or mark superseded.

## Naming
This app is called **My Heron Landing** (`settings.APP_NAME`; use it, never hard-code it, in templates, emails and letters). **"Homeowner Portal" is Vantaca's site** (governing documents live there): never use "portal" for this app in owner-facing text. The header shows My Heron Landing with the association name beneath it.

## Known gotchas
Cache-backed throttles are cleared per test (`conftest.py`). `Violation.OPEN` includes `referred_legal`; `OWNER_CLOSED` (owner list grouping) is different. `/` routes by role; staff dashboard is `/workspace/`. `load_standards` overwrites violation-type rows.

## Working with the user
Non-technical Board member on Windows; screenshots and short wording/layout requests, often mid-task. Plain-language instructions with exact commands; say when `migrate` is needed; be candid about untested integrations. Manager = Board. Current position and next steps: `PROJECT_STATUS.md` sections 9, 12 and 13.
