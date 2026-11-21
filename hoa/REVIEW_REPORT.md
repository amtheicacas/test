# Stabilisation and cleanup pass: report

Date: 2026-11-20. Branch `claude/brave-galileo-mddme9`. No new features. Letters/PDFs, migrations and documentation history were not touched.

## 1. Before and after
| | Before | After |
|---|---|---|
| Commit | `c12298b` | last code commit `ab8d945` (this report is committed after it) |
| Tests, SQLite | 713 passed | **836 passed** |
| Tests, PostgreSQL 16 | 713 passed | **836 passed** |
| `manage.py check` | clean | clean |
| `makemigrations --check` | no changes | no changes (no migration was added) |
| `check --deploy` (production-style env) | 3 known deliberate warnings | same 3 (HSTS subdomains, HSTS preload, X_FRAME_OPTIONS SAMEORIGIN) |
| Production-style boot (gunicorn, fresh PostgreSQL, migrate, collectstatic) | passed earlier | passed again with Render's start command and Render's logging variable simulated: `/healthz` ok for any host, plain http redirects to https, bad host 400, branded 404, static files served, **zero** request lines in the server log (a sign-in address with a secret was requested to prove it) |

No test was weakened, skipped or deleted. Every fix below has a test that FAILED before the fix and passes after (shown by running it against the old code).

## 2. Findings
Severity is my judgement. "Fixed" means a commit plus a test; nothing is claimed fixed without one.

| ID | Sev. | Where | What was wrong / how it could hurt | Status |
|---|---|---|---|---|
| F-01 | medium-high | `views.committee_vote` (route `committee/cases/<id>/vote/`) | (a) The old vote page had no abstention check: a committee member who is the case's owner, or shares the household on the roster, could vote on their own case and set their own fine to "rejected" (which also sends the decision notice). (b) Any committee member who can see a case can finalise it (3-0 approve) or reject it (letter sent at once) from this page, skipping the meeting-level "submit" and the Board's administrative review. No screen links to it; tests use it as a shortcut. | (a) **Fixed** `fb76f8d`. (b) **Needs a human decision** (see 3) |
| F-02 | low | `views.violation_action` (`ext_approve`, `ext_decline`, `board`) | `redirect(request.POST["next"])` = open redirect after a staff form (CSRF-protected, staff only). | **Fixed** `38cd6b1` (`_safe_next`) |
| F-03 | medium | `services._require` (every workflow transition) | The status check used the copy of the case loaded earlier. A double click or two staff acting together both passed it: e.g. the first Notice of Violation issued twice (duplicate email and mailed letter, cure deadline reset). | **Fixed** `b0db85b`: re-reads the status from the database and locks the row inside transactions. Residual: see F-08 |
| F-04 | medium | `services.send_notice` | `subject`/`body` were set inside the `try` after the PDF was built. If the first recipient's letter failed: crash (`UnboundLocalError`) after earlier emails were already sent. If a later recipient failed: the failure was logged with the PREVIOUS recipient's email text (wrong evidence record). | **Fixed** `d926748` |
| F-05 | medium | `pdf._photo` (pdf.py:101) | `except Exception: return []`: a photo that cannot be read from storage is silently left OUT of a letter (no log, no warning). | **Not changed** (pdf.py is frozen). Needs a human decision |
| F-06 | low | `mailing.refresh_tracking` (mailing.py:133) | `except Exception: return False` hides Lob tracking errors; staff just see "0 updates". | Not changed (integration never run live); recommend logging |
| F-07 | medium-low | `casefile._csv` | Owner-typed text (messages, captions, notes) went verbatim into the case-file CSVs; a cell starting `=`, `+`, `@` or `-x` runs as a formula when staff open it in Excel. | **Fixed** `83ade68`: leading apostrophe on such cells only; ordinary text and "- list" unchanged; the exact letter PDFs/originals in the zip are untouched. Such cells now show a leading apostrophe |
| F-08 | medium | `services.finalize_imposition` + `ledger.post_fine` | Not atomic at the top. Two truly simultaneous approvals both see "not yet posted" and could **post the fine to the Vantaca ledger twice**. (The sequential double click is now refused by F-03.) | **Not changed**: the ledger API has never run live and the correct fix (claim-then-post with an idempotency reference) needs design. Needs a human decision before go-live |
| F-09 | low | `middleware.RequireTwoStep` allows `/accounts/` | A staff member who passed only the password step can reach Django's `/accounts/password_change/` (needs the old password; no data exposure; could be used to lock a victim out). | Left; documented |
| F-14 | low | `views.import_owners` | No upload size limit (`f.read()` whole file; staff only). | **Fixed** `fdbd84b`: 10 MB limit with a plain message |
| F-15 | medium | `management/commands/daily_jobs` | The daily steps ran in a chain: one exception (say in `send_due_mailings`) stopped every later step every day, including `advance_expired` (cases not referred to the Board) and the reminders. | **Fixed** `abeffab`: each step is isolated, failures are printed, and the job ends with an error naming them (so a scheduler alerts) after the others ran |
| F-10 | info | repo root: `icac/` (32 files), `tests/test_flow.py`, `hoa/tools/` | `icac/` and the root `tests/` look like an unrelated older prototype, not part of `hoa/`. | Not removed. Needs a human decision |
| F-11 | info | `config/settings.py` | `FINE_LIEN_THRESHOLD`, `HOA_MAILING_ADDRESS`, `MANAGER_NAME` are defined but never read anywhere (every other unread name is a Django framework setting). Possibly placeholders for the Part B letters. | Not removed |
| F-12 | info | `pdf._what`; `vantaca.preview_csv` | `_what` has no reference anywhere (pdf.py frozen, left). `preview_csv` is referenced only by tests. | Left |
| F-13 | info | dependencies | `pip-audit` on `requirements-production.txt`: **no known vulnerabilities**. But most packages are unpinned (Pillow, reportlab, django-otp, django-axes, qrcode, anthropic, openpyxl, boto3): a future major release could break a deploy. `pytest`/`pytest-django` are in no requirements file. | Not changed (no major upgrades without asking). Recommend a pinned lock file |
| INFO-1 | info | `date.today()` used about 35 times | Not a bug: Django sets the process timezone to America/New_York at start-up, so `date.today()` is the Eastern date even on a UTC host (verified with `TZ=UTC`). It relies on that side effect; `timezone.localdate()` would be explicit. | Left |
| INFO-2 | info | `/import/` | The parsed owner list (names, emails, addresses) is kept in the staff member's session row until it expires (12 h). Staff only. | Left |
| HK-1 | housekeeping | repo | A `.coverage` data file was accidentally committed by this pass. | Removed and git-ignored `ab8d945` |

### Reviewed and found fine
- **Access control:** a new test (`test_access_matrix.py`, 98 cases) requests every route as an anonymous visitor, the case's owner, a different owner, a committee member, a Board member and the manager, with GET and POST. No route crashes for any of them; staff pages are closed to owners, visitors and committee; committee pages are closed to owners even for their own case; another owner's photo answers 404 like a missing one; the test also fails if a NEW route is added without being classified, so a future page cannot slip through unchecked.
- **Injection and output:** no raw SQL, `.extra`, `eval`, `exec` or `pickle`; `|safe` appears once (the QR code image the app generates itself); header injection is blocked by Django (a failure is recorded as a failed send); the only `next` redirects are now validated.
- **Secrets:** a scan of every tracked file and of the history for key/token/password patterns found only documentation placeholders; no `local.env`, key or database file was ever committed.
- **Outgoing requests:** all go to fixed hosts (Lob, Zoom, Census) or to addresses the Board sets (Vantaca, heartbeat monitors); Vantaca is https-only.
- **Privacy:** owner names and addresses go to Lob (needed to mail), email, and the Census geocoder (addresses only); "Ask AI" sends only what staff type (warning added earlier). All expected.
- **Dead code:** every template, static file and management command is referenced; the only removals are two always-true/always-false conditions and one unused parameter (below).

## 3. Needs a human decision
1. **F-01(b): the old committee vote page.** Recommend: restrict it to Board/manager or remove the route and `services.committee_decide` (the supported path is the meeting page, then "submit", then the Board's review). It was left because removing it changes who can do what, and `test_portal.py` uses it as a shortcut.
2. **F-08: double ledger posting.** Recommend an atomic claim plus an idempotency reference in the ledger post before the Vantaca ledger is ever turned on.
3. **F-05: a letter built without a photo.** Recommend logging a warning on the case timeline whenever a photo cannot be read for a letter (no wording or layout change).
4. **F-10: `icac/` and the root `tests/`.** Keep or remove?
5. **F-11: three settings nobody reads.** Keep for Part B, or remove?
6. **F-13: pin the dependencies** (a lock file for production), and add a `requirements-dev.txt` for the test tools.
7. **F-07 wording:** in the case-file CSVs a cell that starts with `=`, `+`, `@` or `-x` now shows a leading apostrophe. Acceptable?

## 4. Removed (with proof)
| What | Proof it was unused or dead |
|---|---|
| `if role != "board" or True else ""` in `management/commands/make_login.py` | `or True` made it always true; the message is now printed unconditionally (identical output) |
| `icon="$" if False else b["icon"]` in `progress.py` | `if False` made it always `b["icon"]`; replaced by that |
| parameter `storage_names=None` of `backups.check_database` | vulture reported it unused; no caller passes it (searched code, tests, docs) |

Nothing else was removed. `vulture` at 60% confidence gave 66 function/class candidates; all but three are referenced by non-test code, and those three are `ViolationsConfig` (loaded implicitly by Django), `pdf._what` (frozen file) and `vantaca.preview_csv` (used by tests): all left.

## 5. Tools: what I could and could not do
- `/security-review` could not run: it diffs against the repository's default branch on GitHub, which is an old branch, so it would have reviewed all 245 files. I did the review by hand using your checklist instead.
- `/simplify`: its procedure launches review agents and edits files; you asked for suggestions only, so I did the four-angle review inline and made no edits. Suggestions: share one "must abstain" helper between `committee_vote`, `committee_case_record` and the site-visit check; share one validated-`next` helper between `views` and `security_views`; drop needless lambdas in `daily_jobs`; move the copy-pasted user/case set-up in about ten test files into `conftest.py`.

## 6. What I could NOT verify, and what I did not cover
- Live integrations (Lob, Zoom, Vantaca API/ledger, Census geocoder, SMTP/Postmark) have never run. Real buckets were used only on your PC earlier, not in this pass. Render was not touched.
- Concurrency was tested only by replaying a stale copy of a case; true simultaneous requests were not exercised.
- My reading was targeted, not every line of the roughly 9,000 lines of application code. I read the access control, workflow service functions, error handling, uploads/exports, scheduled jobs and settings. I did **not** do a systematic N+1/performance audit, did not read every template line by line, and did not deeply review `vantaca.py`, `letters.py`, `mailing.py` or `minutes.py` beyond their tests and error paths.
- Coverage (measured): 91% of `services.py` and `views.py`; the least-covered modules are `seed_demo`, `print_statuses`, `geocoding` and `zoom`.

## 7. Riskiest remaining areas
1. The Vantaca ledger poster (F-08) and the rest of the never-run integrations.
2. The old committee vote route (F-01b).
3. Letters built without a photo (F-05).
4. Unpinned dependencies.
5. A first staff account with no authenticator can still enrol one with only its password (known, SEC-14).

## 8. Suggested next steps (in order)
1. Decide items 1 to 3 in section 3.
2. Finish the staging sign-in and smoke tests on Render (flagged in PROJECT_STATUS.md).
3. Pin the dependencies.
4. Wait for the attorney's final letters (Part B), then fix F-05 and the suspension wording together.
5. Schedule the nightly jobs with heartbeats (the daily job now fails loudly, but nothing pings a monitor yet).

---
## Decision recorded: F-10 resolved
The owner decided to **remove** `icac/` and the root `tests/`. Done. Nothing in `hoa/` referenced them, and they remain in git history (commit c1e1345).
