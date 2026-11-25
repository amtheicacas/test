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

---
## Decisions recorded and carried out (owner answers)
| # | Finding | Decision | Done |
|---|---|---|---|
| 1 | F-01b old `committee_vote` route | Restrict to Board/manager | `committee_vote` now `@staff_only` (committee members use the meeting page); abstention check kept; tests updated (`test_stabilisation.py`, `test_portal.py`, `test_access_matrix.py` now lists it as STAFF_ONLY). **User-visible change by the owner's decision.** |
| 2 | F-08 double ledger posting | Fix now | `finalize_imposition`: one transaction that locks the case row, re-reads status and posting state, posts, and saves the posting together with the new status BEFORE any letter is sent; `board_review_item` saves through `_save_locked` so a stale second click cannot write an old status back. Two-thread test on PostgreSQL (`test_two_simultaneous_approvals_post_the_fine_to_the_ledger_only_once`) FAILS on the old code (both approvals went through) and passes now. SQLite cannot show the race (the test skips there). Residual: if the database itself fails in the instant after Vantaca accepted the post, the saved reference could roll back; a retry would then post again. |
| 3 | F-05 unreadable photo | Warn on case | `services._warn_if_photo_unreadable` writes a "WARNING: ... built WITHOUT its photo" event on the case when a Notice of Violation, Second Notice or Notice of Hearing is built (and when a held mailing is released). `pdf.py` untouched; the letter is still produced and sent exactly as before. |
| 4 | F-11 three unused settings | Keep until the attorney's letters arrive | Unchanged. Revisit with Part B. |
| 5 | F-13 pin dependencies | Yes | `requirements-production.lock.txt` (exact versions from a clean Python 3.13 install; whole suite run against them; pip-audit: no known vulnerabilities), `requirements-dev.txt` (pytest, pytest-django), `README_DEPENDENCIES.md`; `build.sh` now installs the lock file (affects the NEXT Render deploy: if that build fails, say so). |
| 6 | F-07 apostrophe in CSV cells | Accept | Unchanged. |


---
# Second pass: stabilisation, security and "what have we not thought of" (2026-11-21)

Branch `claude/brave-galileo-mddme9`, started from commit `65bc0f0`. **No new features.** The letters and PDFs (`pdf.py`), the existing migrations and every document's history were left alone (documents are only appended to). Nothing real was sent anywhere: no call to Lob, Zoom, Vantaca, Postmark, Backblaze, Cloudflare, Render or USPS, and no email or mail went out. Everything ran on mocks, a scratch PostgreSQL 16 on this machine and scratch copies of the site (the only outside lookups were the Python package index and its vulnerability data, for the package list, the audit and the scanning tools). No secret was printed, committed or pasted.

## 1. Before and after
| | Before (`65bc0f0`) | After |
|---|---|---|
| Tests on SQLite (your PC's database) | 950 passed, 1 skipped | **1,275 passed, 4 skipped** (the 4 need PostgreSQL and pass there) |
| Tests on PostgreSQL 16 (the live database) | 951 passed | **1,279 passed** (everything, including the 4 real-thread tests and the page fuzz) |
| Tests added | | **328**, in 9 new files (`*_pass2.py`, `test_crawl_roles.py`, `test_get_fuzz.py`, `test_xss_canary.py`) |
| `manage.py check` | clean | clean |
| `makemigrations --check` | no changes | no changes (**no migration was added**: `migrate` is not needed for this pass) |
| `check --deploy` (live-style settings) | 3 known deliberate warnings | the same 3 (HSTS subdomains, HSTS preload, frame option SAMEORIGIN) |
| Test coverage of the application code | 93% | 94% |
| Security scanner (bandit) | 10 medium (all fixed https addresses), 8 low | the same: nothing new |
| Dependency audit (pip-audit, the pinned live packages) | none found | none found |
| Secret scanner on every tracked file | no real secret | no real secret (the one new hit is a made-up test key) |
| Live-style boot (gunicorn + fresh PostgreSQL, real HTTP client) | passed | passed again on the final code |

No test was weakened, skipped or deleted. One older test (`test_ordinary_text_is_not_changed_by_the_defusing`) was changed on purpose, see CSV-1 below. Every fix has a test that FAILED on the old code and passes now (shown by running it against the old code, and on PostgreSQL where the problem only exists there).

## 2. What was checked, and how
- **Who can open what:** a crawler follows every link as a visitor, owner A, owner B, a committee member, a Board member, the manager, an owner who is also on the Board and an owner who is also on the committee. No page crashes, no owner ever reaches another owner's name or address, and no owner page shows a staff-only screen. A "page fuzz" test throws awkward web-address values at every Board and committee page.
- **Pages with script-like text:** a "canary" string that would break out of HTML text, a quote or a script block is put in every free-text field of every record, then every page is fetched as six kinds of login. Then the same in a real browser (Chromium): every page, the map popups and the pickers are opened; no script ever ran.
- **Real live-style server:** gunicorn on a fresh PostgreSQL with Render's start command, tested with a real HTTP client: health check, plain http is redirected, a wrong host name is refused, the headers and cookie flags, the two-step sign-in (password alone is bounced, the right code works, the same code is refused a second time), forged forms are refused, odd and oversized requests, and the server log holds no private link, password, secret key or session id. Six unsafe settings each stop the site from starting, with a plain message.
- **Two people at once, and the same click twice:** real threads against PostgreSQL for the daily advance, releasing held mail, recording a letter's result, submitting a meeting's results and the fine posting. Every staff button was also pressed twice on a case in each of the 14 statuses.
- **Dates and money exactly at the rules' edges:** hearing notice of 12, 13, 14, 15 and 20 days, 11:59 PM and 12:01 AM local time, the two clock changes, the leap day, month and year ends, the $0 / $100 / $1,000 / $1,000.01 fine amounts, tied votes, weekends.
- **Awkward data:** accents, apostrophes, ampersands, emoji, right-to-left names, very long text, line breaks and control characters pasted from Word, HTML-looking text and spreadsheet formulas. Every kind of letter for every recipient builds a PDF; every notice emails and mails; the case-file download builds. 13 "property" tests give each text cleaner thousands of generated strings.
- **Imports:** the usual Excel file layouts and encodings, line breaks inside cells, a row nobody can read, the same home twice, a changed email, an owner who disappears, a partly read file.
- **At scale:** a scratch database with 5,000 owners and 20,000 violations (about ten times a real community), every main page timed and its queries counted.
- **Moving and restoring data:** the newest migration applied forward on a database that already holds data; a fresh database migrates and loads the standards; a backup was made, encrypted, decrypted with the right key (and refused with a wrong key) and restored into a new database: the row counts and the content checksums match.
- **Phone width (375 px) in a real browser** as manager, Board, committee and owner.
- **Reading the code:** `services.py` was read as a state machine (every status and move), then `mailing.py`, `vantaca.py`, `ledger.py`, the views and the settings. The automated `/security-review` check was run once, at the start, on everything changed between the first pass's starting point (`c12298b`) and `65bc0f0`: it reported one low-confidence item (CSV-1, fixed). The changes made during this pass were covered by the tests, the scanners and the live-style checks, not by a second automated review.

## 3. Findings (each fixed finding has a test that failed before)
Severity is my judgement. "Test" names are in `violations/tests/`.

### Money, letters and legal notices
| ID | Severity | What it was, in plain words | Status | Test |
|---|---|---|---|---|
| LB-1 | **High** | If one letter of a notice could not be built, the whole hand-over to Lob stopped after earlier addresses had already been handed over. The earlier letters were real but their records were rolled back, so the next attempt (on every dashboard load) sent them again. | Fixed: that address is recorded as an error and the others still go | `test_LB1_*` (2) |
| LB-2 | **High** | Two people loading the dashboard or Board queue at the same moment could both release the same held notice to Lob: duplicate certified mail and postage. Proved on PostgreSQL: 3 simultaneous page loads handed 6 letters over instead of 2. | Fixed: each held notice is claimed under a lock in its own short transaction; a second click or load is refused or skips it | `test_LB2_*` (stale click; real threads on PostgreSQL) |
| HDR-1 | **High** | An owner whose address contained a line break (an import can bring one in) was never emailed ANY notice: every email failed with "Header values can't contain newlines". | Fixed (subjects are put on one line; the import also cleans names and streets) | `test_awkward_data_pass2.py` (72), `test_import_pass2.py` |
| ADV-1 | Medium | The daily "move expired cases to the Board" step could overwrite an extension granted a moment earlier, and could crash a page load if someone had just lifted a hold. | Fixed (re-checked under a row lock; no longer one big transaction) | `test_ADV1_*` (2) |
| RM-1, RM-1b | Medium | A deadline or hold-ending reminder was recorded as "sent" even when every email failed (mail server down), and was never retried. | Fixed: recorded only if at least one email really went | `test_RM1_*`, `test_RM1b_*` (3) |
| EXT-1 | Medium | "Give more time" with an exact date EARLIER than the current deadline shortened the cure period and emailed the owner an "extension". | Fixed: refused with a plain message (a Second Notice may still reset the date) | `test_EXT1_*` (2) |
| NUM-1 | Medium | A committee vote of 4 for and -1 against passed the "3 votes" and "majority" checks; the decision letter would have printed it. | Fixed: whole numbers 0 to 99 | `test_NUM1_*` (7) |
| NUM-2 | Low-medium | A typed fine such as `abc` or `NaN` gave an error page, and `12.345` was accepted with a fraction of a cent. | Fixed: plain message | `test_NUM2_*` (14) |
| PIECE-1 | Low-medium | Recording a mailed letter's result (mailed, delivered, not delivered, handled) was not all-or-nothing and not locked: an old page could overwrite a delivery just recorded; two people recording the two letters of one notice could leave the notice never marked mailed. | Fixed: atomic, locked, re-read first | `test_PIECE1_*` (4, one with real threads) |
| RM-3 | Low | A double click on "owner says fixed / re-inspected" thanked the owner twice and logged twice. | Fixed | `test_RM3_*` (2) |
| RM-5 | Low | Two people pressing "Submit results" at once logged every case twice and could overwrite a case the Board had just processed. | Fixed | `test_RM5_*` (2, one with real threads) |
| MTG-1 | Low-medium | Moving a committee meeting changed the meeting and then the cases one by one; a failure half way left later cases on the old date. | Fixed: meeting + cases + history saved together, then notices sent one by one | `test_MTG1_*` (2) |
| LEDGER-1 | Medium | After a time-out or dropped connection the ledger posting said "Nothing was posted", but Vantaca may have accepted the charge, so a retry could charge the owner twice. | Fixed (message only, only for uncertain failures: look for the charge reference first) | `test_LEDGER1_*` (10) |
| IMP-1, IMP-2 | Medium | One unreadable row (for example a latitude of `abc`) crashed the whole import half-way; a hidden control character or NUL (PostgreSQL cannot store it) did the same; with "deactivate missing owners" an unreadable row would have made its owner look missing. | Fixed: each row on its own, failures counted and listed in the Audit log, nobody is deactivated after a failed row, the page and the scheduled command say so | `test_import_pass2.py` (33) |
| KEY-1 | Low | A Lob key with a stray space or line break passed the "test or live" check but was sent untrimmed (Lob would most likely refuse it). | Fixed | `test_KEY1_*` |
| LB-5 | Low | A failed USPS tracking check was swallowed; the daily job just said "0 updates". | Fixed: written to the server log and printed by the daily job (the job still never fails for it, as an existing test requires) | `test_LB5_*` |
| PERF-1 | Medium (only at scale) | At 5,000 homes with ~5,000 cases waiting, the Board queue took 35 s (8,400 queries) and the dashboard 564 queries. | Fixed: **35 s to 4.4 s**; dashboard 48 queries; a test keeps queries from growing with rows | `test_PERF1_*` (5) |

### Security
| ID | Severity | What it was | Status | Test |
|---|---|---|---|---|
| NUL-1 | Medium | A NUL character in a search box, form or link was an error page on the live database (SQLite accepts it, so no test noticed): `/violations/?q=a%00b`, a reply typed with one, even the public private-link addresses. | Fixed: a small filter removes NULs; a NUL in an address is "page not found". Proved on PostgreSQL. | `test_a_nul_character_*`, `test_the_nul_filter_*` |
| CSV-1 | Low | In the case-file CSVs a cell starting with a dash AND a space ("- 1+cmd|...") was left alone as "a list", but Excel ignores the blank and reads it as a formula. An owner's reply reaches that file. (Found by the automated security review, confidence 7 of 10.) | Fixed: also defused. **This reverses the earlier "dash then space is a list" exemption** (F-07); a bullet-style reply now shows a leading apostrophe in the CSV. | `test_CSV1_*` (12), `test_stabilisation.py` |
| NEXT-1/2/3 | Low-medium | A hand-edited "go back to" value such as `abc` or `0` passed Django's own check and then crashed the redirect (after a staff action and after the two-step code); a value with a control character was followed; and the page you wanted lost its search terms after the two-step code. Found by a generated-input (Hypothesis) test. | Fixed: one shared rule (`accounts.local_path`: a path on this site only, no control characters) and the address is quoted | `test_NEXT1_*`, `test_NEXT2_*`, `test_properties_pass2.py` |
| PRIV-1 | Low | The private owner link page (`/r/<token>/`, no login) had no "do not store" header, so a shared computer's browser could keep a case page. | Fixed: `Cache-Control: no-store, private` | `test_PRIV1_*` |
| RM-2 | Low | A hand-edited attachment key on the attorney email gave an error page. | Fixed | `test_RM2_*` (3) |

### Other
| ID | Severity | What it was | Status |
|---|---|---|---|
| Phone layout | Low | At phone width the dashboard, the case page and the map scrolled sideways. | Fixed (CSS only, inside the phone-only rules; desktop unchanged). Verified with a real-browser crawl (`tools/browser_checks/`). |
| TEST-1 | Test flaw | `test_privileged_roles_must_enrol_then_verify` failed about 1 run in 50 when the real clock crossed a 30-second one-time-code step. | Fixed in the test only (clock held inside one step); nothing weakened. |

## 4. Reviewed and found fine (no finding)
Every URL against every kind of login (access matrix and crawler); other people's data never shown to an owner; staff-only wording never on owner pages; script-like text in every field, in HTML and in a real browser; SQL-looking text; the 14-day hearing rule at 13/14/20 days, midnight and clock changes; the $1,000 cap edges; tied votes; business days over weekends, month ends, the year end and the leap day; letters build for every awkward string (24 strings x every kind x owner, property and tenant copies); tampered, cut-short and re-used sign-in links; a demoted, switched-off or password-changed login loses access at the next click; the old cookie after signing out; every staff button pressed twice in every status; live-style headers, cookies, CSRF, bad host, odd requests; no secret in the server log or in tracked files; dependency audit clean; the newest migration applied on a database with data; a fresh database migrates and loads the standards; encrypted backup and restore round trip.

## 5. Removed (with proof)
Nine unused imports (`io` in `backups.py` and `dbbackup.py`, `timezone` in `letters.py`, `progress` in `print_statuses.py`, `Profile` in `purge_archived_owners.py`, `CommitteeMeeting` and `Violation` in `minutes.py`, `date` in `progress.py`, `FileResponse` in `views.py`), one commit each. Proof: the linter reports them unused; a search finds no use of any of them through those modules in code, templates, tests or documents; the whole suite passes on SQLite and PostgreSQL without them. Nothing else is provably dead: every template and static file is referenced, there is no commented-out code and no TODO, and the five "unread settings" are library settings (django-axes, password reset). The earlier decision to keep three unused settings until the attorney's letters arrive is unchanged.

## 6. What I could NOT verify, and why
- **No integration has ever run live** (Lob, Zoom, Vantaca, Postmark, Backblaze, Cloudflare, USPS): not allowed in this pass, and no accounts exist for some. Everything about them is tested against mocks that follow their published rules.
- **The legal text** (Fla. Stat. 720.305 and your Declaration) was not available; every legal reference is from memory. Questions for the attorney are in `ATTORNEY_QUESTIONS.md` (20 to 23 are new).
- **Real Excel** was not opened. The spreadsheet-formula fix follows the standard guidance (prefix text starting with `= + - @`) and known Excel behaviour.
- **Real email, real print and mail, a real authenticator phone**: only the in-memory test outbox and computed one-time codes were used.
- **Browsers other than Chromium, and a real phone:** the phone-width check used Chromium at 375 px; Safari, Firefox and Edge were not tried.
- **Windows:** the checks ran on Linux. The `.bat` files and the Windows-only `strftime("%-d")` rule are covered by a guard test, but the batch files were not run.
- **Render itself** (its deploy settings, cron schedule and Shell) was not touched.
- **Real data volume:** scale tests used made-up rows (5,000 homes, 20,000 cases).

## 7. Needs a human decision
| # | Item | Recommendation | Best way to answer |
|---|---|---|---|
| 1 | **DEC-2** A Board member or manager who is also the OWNER of a violation (one login) can dismiss it, mark it cured, record the Board's vote and approve its own fine alone. Committee members are blocked by the abstention rule; Board and manager are not. (Confirmed by a probe.) | Block staff actions on your own household's case: another Board member or the manager must act. Also ask the attorney (question 20). | Reply "block it" or "leave it"; then I build it with tests. |
| 2 | **DEC-1** After a fine has been imposed and posted to the owner's ledger, the case can still be dismissed or marked cured ("closed, no fine") with no warning that the ledger charge and the unpaid fine still stand; and a payment cannot be recorded after referral to the attorney. | Show a warning and require a note; decide with the attorney what happens to a posted charge (question 21). | Reply "warn" or "leave"; the attorney's answer decides the rest. |
| 3 | **LB-4** If one letter of a notice fails at Lob (one address refused) but another goes, the notice is marked mailed and the failed address appears only in a small staff note and the case history. The red "Certified mail problems" box does not show it. | Add the failed address to the red box as "letter NOT sent" until someone handles it (same as an undelivered certified letter). Attorney question 23. | Reply "add it". |
| 4 | **Where the sending happens.** When the daily job has not run, the first page load of the day (dashboard or Board queue) hands held mail to Lob inside that page load. A slow Lob can hold that page up (the server stops a page at 60 s), and if it is stopped half way, letters already handed over could be handed over again on the next load (an idempotency key, item 5, would prevent that). | Let only the scheduled daily job send; the dashboard just shows "mail due". Do this together with item 5. | Reply "job only". |
| 5 | **LB-3** Lob supports an "idempotency key" so a retried request cannot create a second letter. Not added because Lob's exact behaviour is unverified and the app has never talked to Lob. | Decide after the first Lob TEST-key run, with the real answers. | Tell me when the Lob test account exists. |
| 6 | **Letters say something that may be untrue** (frozen, report only): the decision letter and the extension letter print "Copy also sent to the tenant/owner" whenever a tenant is on record, even though paper copies now go to the owner only and a tenant without an email gets no copy. | Wait for the attorney's final Word letters (Part B) and fix it there. Attorney question 22. | Nothing now; included in the letters work. |
| 7 | **F-07 reversed.** Case-file CSV cells that start with "- " (a bullet-style reply) now show a leading apostrophe. | Keep: it closes a real spreadsheet hole. | Reply "keep" or "restore the old behaviour". |
| 8 | **HSTS lifetime is 1 hour** (Render environment `SECURE_HSTS_SECONDS`). | Raise it to a year (31536000) after a week of stable running. | Do it in Render's Environment page; I can give exact steps. |
| 9 | **Login heading reads "Welcome to the My Heron Landing Application"** ("the My ...", because the app's name starts with "My"). | Change to "Welcome to My Heron Landing Application". | Reply with the wording you want. |
| 10 | **Violation list and map data are not paged.** Fine for a few hundred homes; at 20,000 cases the list is 4.3 MB and takes 3.5 s. | No action now; page the list if it passes ~2,000 cases. | Nothing now. |
| 11 | **No Content-Security-Policy header** (a second wall against injected scripts). The pages use inline scripts, so it needs rework. | Optional, later. | Nothing now. |
| 12 | **Excel "Unicode text" (UTF-16) owner lists** are refused with "doesn't look like a CSV or Excel file". | Add support if your manager ever hits it (ordinary CSV and .xlsx work). | Tell me if it happens. |
| 13 | **The pinned package list has no hashes** (a supply-chain hardening). I checked the two unusual names in it (`httpx2`, `httpcore2`): they are the genuine successors of httpx, made by the same author and maintained by Pydantic, and `anthropic` 2.12.0 requires them. | Add hashes the next time the list is regenerated. | Nothing now. |
| 14 | **Sending a decided case to another meeting.** The service allows re-assigning a case that already has a recorded committee decision (the screen never offers it). | Refuse it (a few lines). | Reply "refuse it" or ignore. |

Not acted on, for the record: **NOTE-1** the $1,000 aggregate counts only fines currently "approved" (attorney question 11); **FIN-1** the fine-posting step re-reads only the posting fields under its lock (the second person is told the case was already processed, so I left it); **MIG-1** the 0044 migration's backfill uses UTC dates, so an evening letter would get a mailed date one day late (no print-service letters exist yet, and applied migrations are not edited).

## 8. Suggested next steps (in order)
1. Answer decisions 1 to 4 and 7 (short replies are enough).
2. Send the new attorney questions 20 to 23 with the earlier list.
3. Finish the staging checks on Render (nothing in this pass changes the steps; `migrate` is not needed).
4. Provide the virtual-mailbox return address, then the Lob account and TEST key, then do the first Lob test-key run (decision 5).
5. When the attorney's final Word letters arrive: Part B (letters), including decision 6.
6. Schedule the daily job with a heartbeat monitor, so a failed run is noticed.

## Answers recorded 2026-11-22 (the owner's replies to section 7 above)
The owner answered decisions 1, 2, 3, 4, 7 and 9. Each change was built with tests that failed first (78 new tests, `test_decisions_pass3.py`). No migration was added (**`migrate` is not needed**), `violations/pdf.py` and every existing migration are untouched, nothing real was sent and no live service was called. Sections 1 to 8 above are left as they were; where they say a decision is open, this section supersedes them for the decisions listed here.

| # | Decision | The owner's answer | What was built (commit) |
|---|---|---|---|
| 1 | **DEC-2** own-household actions | "Not relevant for a manager. A Board member can't waive their own, unless it is only moving it from one stage to another." | `e303b8f`. See "How decision 1 works" below. |
| 2 | **DEC-1** closing after a fine was posted | "warn" | `a3f5c1c`. When a case whose fine is already posted to the owner's Vantaca ledger is dismissed, or marked corrected so that it closes with no fine: the case page shows a "Check the ledger first" warning and asks for confirmation; **a note saying why is required** (the service refuses without one); a WARNING line goes into the case history; and a message after closing says again that the charge is still on the ledger. A case with no posted fine, and a case whose imposed fine keeps it open (corrected but unpaid), behave as before. Not changed: a payment still cannot be recorded once the case is with the attorney, and the app cannot reverse a ledger charge (that is done in Vantaca). Attorney question 21 stays open. |
| 3 | **LB-4** a failed address in the red box | "add it" | `698abad`. Every letter the print service did not take (refused, would not build, or an incomplete address) is listed as **"Letter NOT sent"** (a status on the existing letter record: no schema change). It shows in the red "Certified mail problems" box ("N letters were NOT sent: the print service did not take them", with certified / first class and the reason), in the menu tooltip, the "to do" count and the case page, until someone uses "Mark handled" with a note. A second try updates the same entry (no duplicates) and a later success closes it. Owners never see it and it never counts as mailed. Three older tests asserted the old behaviour (a refused letter leaves no record) and were changed on purpose. Attorney question 23 stays open. |
| 4 | **Where sending happens** | "job only" | `e611d67`. `advance_expired` (run on every dashboard and Board-queue load) no longer calls `send_due_mailings`. Held mail is handed to the print service **only** by the daily job (its own `send_due_mailings` step, unchanged) and by the **Mail now** button. The dashboard shows "N mailed copies are past the wait and have not gone out yet. The daily job sends them." so a job that is not running cannot go unnoticed. The old two-page-loads concurrency test now runs two job steps at once (still passes on PostgreSQL). |
| 7 | **F-07 reversed** | "keep" | No code change; the reason is written into `casefile._safe_cell`. |
| 9 | **Login heading** | "proper rewording" (read as: use the recommended wording) | `86f5b46`. The heading now reads "Welcome to My Heron Landing Application". Two branding tests were updated. |

### How decision 1 works (and how I read the answer)
The rule follows **the home**, not the job title: it applies when the signed-in person's login is linked to the owner record of the case (`services.own_household`). So a Board member, or a manager, who owns a home here is covered, and a manager who owns none is never affected (the owner said it is not relevant for a manager; Board and manager stay identical in access). I read "waive" for the typed "wait", and "unless it is moving it from one stage to another" as "a step that only carries the case forward is fine".
- **Blocked on your own household's case** (someone else must do it): dismiss; mark corrected; verify a re-inspection (either result); give more time, or answer (approve or decline) a request for more time; put on hold; a Board vote to dismiss; the committee's decision (the case-page form now has the abstention rule that the other two forms already had); and in the Board's review of committee results: lower the fine, waive it, send it back, or approve a waiver or rejection of the fine.
- **Still allowed (stage moves and records):** issue the notice, refer to the Board early or to the attorney, record the Board's vote to go ahead with a fine, send to a committee meeting, submit a meeting's results, approve a fine the committee confirmed (review page or case page), mark for re-inspection, lift a hold, record a mailing or a payment, replies, photos.
- The case page, the Board queue and the review page show a banner / note and hide the blocked buttons; the service layer refuses the same actions even if a page is forged.
- **If the line is drawn in the wrong place** (for example the Board's vote to go ahead should also be blocked, or approving a fine should be): each is a one-line change in `services.py` (`_not_your_own`). Recording a payment was left open on purpose (it is bookkeeping, and a case cannot close without being corrected, which is blocked).

### Left as it was, on purpose
- The plain email "Hold ended" to the owner is still sent when a hold's date arrives (on the first page load or by the daily job): it is quick and does not go through Lob. Say if it should also move to the daily job only.
- The standalone `advance_violations` command no longer sends held mail (it only moves cases); `daily_jobs` is the one that does.
- **Consequence of decision 4:** until `daily_jobs` is scheduled (on staging it is not yet), held mailed copies stay held. Nothing mails on staging anyway; on the live site scheduling the daily job with a heartbeat is now required, not optional.
- Other sentences still say "the My Heron Landing Application" (for example in notice emails and a few messages); only the login heading was reworded. The same change can be made there if wanted.

### Still open (no answer needed yet)
5 (Lob idempotency key: after the first Lob TEST-key run), 6 (the decision and extension letters' tenant-copy sentence: with the attorney's Word letters), 8 (raise HSTS to a year after a week of stable running), 10 to 14 (optional, later). Attorney questions 20 to 23 are still to be sent.

### Checks after this round
SQLite 1,353 passed + 4 PostgreSQL-only skipped; PostgreSQL 16 1,357 passed (was 1,275 / 1,279). `check`, `makemigrations --check` (no changes), `check --deploy` (the 3 known warnings), the security scanner (10 medium, unchanged) and the secret scan (none) are as before. The changed screens were looked at in real Chromium at desktop and phone width (no sideways scrolling).

### Suggested next steps (replaces section 8)
1. On Render: Manual Deploy > Deploy latest commit, then run the new checks in TESTING.md section 7.
2. Send attorney questions 20 to 23 with the earlier list.
3. Schedule `daily_jobs` (now also what sends held mail) with a heartbeat monitor.
4. Provide the virtual-mailbox return address, then the Lob account and TEST key, then the first Lob test-key run (decision 5).
5. When the attorney's final Word letters arrive: Part B (letters), including decision 6.


### Update 2026-11-23: follow-up to decision 4 (where sending happens)
Decision 4 stopped PAGE LOADS from sending held mail. The same review noted that the explicit actions (send to a meeting, approve a decision letter, grant more time, an urgent notice, move a meeting) still called Lob during the page, inside the database transaction. That is now fixed (PROJECT_STATUS.md section 38): those actions only QUEUE the letters; a background thread hands them over just after the page answers; the daily job and the dashboard are the safety net; each letter is recorded the moment Lob accepts it; a hand-over that stops half way resumes without sending a letter twice (proven on a real server with a kill -9). **Migration 0045, so `migrate` is needed.** Decision 5 (Lob's idempotency key) stays open: it would also protect the single letter in flight at the instant of a crash. Also added: `manage.py check_email`.


---

# Independent review, pass 4 (2026-11-23): a full, fresh look at the whole application before anything new is built

Asked for by the owner after the hand-over of letters to Lob and `check_email` were built: pause all new work, review and test the whole application as if I had never seen it, re-check that the two earlier passes still hold, and look hard for what they missed. Everything since commit 3993755 (10 commits, 36 files, about 2,100 lines) had never had an independent review, so that part got the closest reading. **No new feature was started** (the committee notification emails are still waiting for your "go"). Letters (`violations/pdf.py`) were not touched; nothing was sent to any real service (Lob, Zoom, Vantaca, Postmark, Backblaze, Cloudflare, Render, USPS); no existing migration was edited and **no new migration was needed** (migration 0045 from the last round is still the newest).

## 1. Before and after
| | Before (start of the review, commit c00c505) | After (this report) |
|---|---|---|
| Tests on SQLite (your PC) | 1,392 passed, 6 skipped | **1,522 passed, 12 skipped** (the 12 skips are the real-thread tests, which need PostgreSQL) |
| Tests on PostgreSQL 16 (the live database) | 1,398 passed | **1,534 passed** (baseline was 1,398; the 12 real-thread tests run here) |
| Test coverage of the code (SQLite, tests included) | 97% (568 of 16,550 lines not run) | 96% (17,573 lines, 702 not run: the 12 PostgreSQL-only tests are skipped on SQLite, so their own lines show as not run; the production code alone is at 94%) |
| `manage.py check` / `makemigrations --check` | clean / no changes | clean / no changes |
| `check --deploy` (live-style settings) | the same 3 known warnings | the same 3 known warnings |
| Security scanner (bandit) | 10 medium (all the fixed https calls), 8 low | unchanged: 10 medium, 8 low, 0 high |
| Known-vulnerable packages (pip-audit, the live lock file) | none | none |
| Secrets in the repository (detect-secrets, every tracked file) | 21 hits, all placeholders | 22 hits, all placeholders (the 22nd is a made-up password in a new test) |
| Production-mode server (gunicorn, PostgreSQL, https header) | not run this round | run: headers, cookies, sign-in, owner isolation, two-step, logs all as they should be (section 2) |

## 2. What I did, and how
1. **Baseline** (numbers above), then read **every module** of the application with fresh eyes, highest risk first: `services.py` as a state machine (every change of stage, what it checks first, what a second press does, what is locked, what happens if the process dies half way), `mailing.py` and the new queue / claim / background-thread code, `views.py` and `urls.py` route by route (who may call it, which ids it trusts), `accounts.py`, `security_views.py`, the middleware and the throttle, the Vantaca import, the ledger and Zoom code, uploads and downloads, the case file, backups, the admin, the settings and the live-site checks, and the pages an owner can open.
2. **An independent security review** (the `/security-review` skill, run by a separate reader over the 10 unreviewed commits and the code around them, with a temporary local `origin/HEAD` that I deleted afterwards): **nothing at or above its confidence bar**. It also pointed at two things I had already found myself (section 3: R4-07 and the admin item in section 6).
3. **By hand, the same questions from outside:** every route as every kind of login (the existing access matrix and role crawler were re-read and found complete: every route is classified, every id in an address or form is tied to its own case), a **production-mode server** against a real PostgreSQL (below), a **kill -9 during a letter**, **real threads** on PostgreSQL for the double-press and race fixes, a **randomized stress test** of the hand-over (eight notices, six workers at once, a print service that refuses 15% of letters; twelve different random seeds), **everything-at-its-limit** data on PostgreSQL, dates and money at their edges, hostile uploads, memory, and the migration forward and back on a database holding data.
4. **Production-mode server (gunicorn, `DJANGO_DEBUG=0`, scratch PostgreSQL 16, the host's https header simulated):** `Strict-Transport-Security` (1 hour, as chosen), `X-Content-Type-Options: nosniff`, `Referrer-Policy` (`no-referrer` on the private-link pages), `X-Frame-Options: SAMEORIGIN`, `Cross-Origin-Opener-Policy: same-origin`; `no-store` on every signed-in page; cookies `Secure` + `SameSite=Lax` (+ `HttpOnly` on the session; owners 30 days, staff 12 hours); plain http gets a 301 to https; a wrong Host gets 400; the moved admin address is a 404; the 404, 403 and CSRF pages show nothing internal; a sign-in link works once (a second use is 410); one owner cannot open another owner's case (404) or a staff page (403); a manager must pass the two-step code (a wrong code stays on the page, and the library's own throttle refuses an immediate retry); the server log held no token, link, key or password.
5. **Migration 0045** applied backward to 0044 and forward again on a PostgreSQL database holding 20,002 notices: fine, the three columns are empty-allowed, no data touched.

## 3. Found and fixed (each has a test that failed on the old code; the old code is commit c00c505)
Severity is my own judgment of the effect on the live site, not a score. "Where" is the code as it stood before the fix.

| ID | Severity | Where (before) | What happened (plain words) | Fixed in |
|---|---|---|---|---|
| R4-01 | **High** (a double send) | `services.assign_to_meeting` :1376 | Pressing **Send to committee** twice (a double click) sent the Notice of Hearing twice: two emails, two certified and two first-class letters. | `13257ae` |
| R4-02 | High (a double send) | `services.reschedule_meeting` :1306 | **Move meeting** to the time it already has (or a double click) re-sent every owner's Notice of Hearing. | `41a1972` |
| R4-03 | **High** (a double send and a wrong date) | `services.grant_extension` :1013, `decline_extension` :1051, `views.violation_action` (extension, board), `board_bulk` | A double click on **Set new deadline & notify** (or the Board's "give more time") added the days **twice** and sent two letters; a "second notice" did the same; two Board members answering one owner request could both grant it, or one grant and one decline. | `d6d83b8` |
| R4-07 | Medium (a lost record) | `mailing.apply_tracking` :199, `services.mark_mailed` :452, `_sync_notice_from_pieces` :498 | These wrote back the WHOLE notice they had loaded earlier. On the live site the letters are handed over by a background thread that starts at once for the fine letter, a moved meeting's notices and a rejected fine; its stale copy blanked "emailed on / to / error" on the notice (and could undo a claim). | `28353a5` |
| R4-08 | Medium (locked out) | `throttle.hit` | The "too many tries" window started afresh on **every** attempt, even refused ones: someone who kept trying was never let in again, and anyone who knew an owner's email address could keep that owner from being sent a sign-in link with one request an hour. | `ada606d` |
| R4-11 | **High on the live database** (a crash after the emails went out) | `services.send_notice` :218-226, `_email_owner` :1005, `assign_to_meeting` :1385, `views.committee_members` :1078 | PostgreSQL refuses text longer than its column; SQLite (your PC and the tests) accepts it. Sending a notice to a household with about 7 or more email addresses raised a server error AFTER the emails had gone out (the notice's "emailed to" note holds 254 characters, the email log's 500); a long Zoom link plus place broke "Send to committee" (250); a long owner name broke "Add to the committee roster" (120). Reproduced on PostgreSQL 16 ("value too long for type character varying(N)"). | `b771410` |
| R4-15 | Medium (a silent gap) | `services.finalize_imposition` :1626, `board_review_item` | The decision letter is created after the fine is imposed and posted. If creating it failed for any reason: a server error page, a history line that said "letter sent", and no sign that the owner had NOT been sent it. Now the decision stands, the history gets a WARNING, and the person is told plainly. | `5c2799b` |
| R4-22 | Low (a phone) | `Notice.delivery_lines` (the "Print/mail provider" line) | Found by the real-browser phone-width check: the ids of a notice's letters were listed with commas and no spaces, so on a phone the staff case page was wider than the screen and scrolled sideways. | `3b7dc4b` |
| R4-16 | Medium (could stop the host) | `casefile.build_case_file`, `views.case_file`, `views.legal_email` | The case file held every photo in memory several times over: 40 photos of 9 MB needed **816 MB** (an owner can add 30 through the private link). Built on disk one file at a time now: **33 MB** for the same case. | `2d1ce1f` |
| R4-18 | Medium (a case stranded) | `services.committee_record` :1446, `board_review_item` ("return") | A committee member saving a decision at the same moment another pressed **Submit results** could write the case's old stage back over the new one: the meeting said "submitted" and the case was back where nobody could reach it. Reproduced with real threads on PostgreSQL. Also "send back to the committee" moved the case and the meeting in two separate saves. | `3950dc3` |
| R4-10 | Low-medium (wrong emails) | `accounts.sync_account` :256, `vantaca.preview_rows` | Looking at the owner-import **preview** emailed the owner's OLD address "your sign-in email was changed" (the preview runs the real import inside a transaction it undoes, but the email was already gone); a row that failed later did the same. | `e213eca` |
| R4-17 | Low (a secret) | `check_email.explain` | The command promises never to print the mail login, but the mail server's own error words can repeat it (with some services the "user name" IS the secret key): now blanked. | `e6e8b08` |
| R4-04 | Low (staff-only) | `services.issue_first_notice` :237 | The notice timing came straight from the posted form: any text was saved (a long one crashed on PostgreSQL). | `6d7f979` |
| R4-05 | Low (staff-only) | `services.committee_record` :1488 | A superscript digit (or thousands of nines) in the "abstain" box crashed the page. | `6d7f979` |
| R4-06 | Low (confusing) | `views.results_review` :1317 | After a refused second approval the review screen showed the red reason AND a green "done" line. | `7de70bf` |
| R4-09 | Low (staff-only) | `vantaca.apply_mapping` / `mapping_problems`, `views.import_owners` :1665 | A hand-made column match was saved as the "remembered matching" and then crashed the import (HTTP 500); the check ran only for one of the buttons. | `fd1786d` |
| R4-13 | Low | `views.committee_members` :1059 | A double click on **Add** put the same person on the roster twice; a hand-made year (huge or negative) was saved as it was (a server error on PostgreSQL). | `7197862` |

Tests added: **135 new tests (plus one for the phone-width fix, R4-22)** in `violations/tests/test_review4_*.py` (the shared helpers are in `review4_support.py`). Six of the new tests use **real threads on PostgreSQL** (two people at once, and the stress test; they skip on SQLite). The numbers R4-12, 14, 19, 20 and 21 are test-only guards, not fixes. The checks below found nothing and are kept as guards: `…_m_everything_at_its_limit` (every text column filled to its limit, then the whole workflow and every staff page, on PostgreSQL), `…_s_hand_over_stress` (the randomized hand-over stress test), `…_t_hostile_uploads` (HTML, SVG, PHP, PDF, empty, truncated and 100,000 x 100,000-pixel "pictures" on every way in) and `…_u_dates_and_money` (the 14-day rule by Florida calendar day, a monthly series across both daylight-saving changes, the 5th weekday, business days over weekends and year ends, the fine cap to the cent).

## 4. Checked and found fine (no finding)
- **Who can do what:** every route is classified in the access matrix; every id in an address or form is tied to its own case, owner or meeting (a photo, notice, mail piece or response from another case is refused); owners reach only their own household; a Board member who is also an owner cannot reach the staff pages from the owner pages without the two-step code; the own-household rule is enforced in the service layer (the pages only hide the buttons).
- **The new hand-over (migration 0045):** a claim is one `UPDATE`; no Lob call is inside a transaction; each letter is recorded the moment Lob accepts it; a hand-over that stopped half way resumes without sending anything twice; a randomized stress test with six concurrent workers and 15% refusals, run with twelve different seeds, never sent a letter twice and never lost one without listing it. The only gap is the one in section 6 (a process killed while a letter is "in the air").
- **Sign-in:** a sign-in link works once, expires, is stored only as a hash, needs a click on a button (so a mail scanner cannot use it up) and cannot be forged or guessed; an old link is cancelled when a new one is sent or the address changes; the "get access" page answers the same whether or not the address is on file; password-reset links use `SITE_URL` on the live site; a deactivated login or a removed role stops working at once; "sign out everywhere" ends every session.
- **Files:** a picture is accepted only if it really is one; stored names are made up by the server; served only as a picture with a fixed type, no sniffing and a policy that forbids running anything; minutes only as PDF or Word without scripts or macros; the saved copy of every letter is add-only and fingerprinted.
- **Spreadsheet formulas** in the case-file CSVs get the apostrophe (as decided); **XSS**: the only `|safe` in the application is the QR code; the one inline script value uses `escapejs`; the XSS canary (HTML and a real browser) was re-run (section 5).
- **Dates and money** (guards in `…_u_dates_and_money`): all correct, including the 14-day rule at 11:59 PM and 12:01 AM Florida time. **Windows traps:** no `%-d`-style date flag, no Unix-only call anywhere in the code.
- **Own-household rule and recording a payment:** still as decided (it is bookkeeping, and a case cannot close without being corrected, which is blocked). The only effect of a Board member marking their own fine paid is to hide the "fine overdue" flag; say if that should also be blocked.
- **Letters:** `pdf.py` untouched. With normal text a violation description or cure of 16,000 characters still builds (it wraps across pages); only a single unbroken "word" of thousands of characters cannot be laid out, and then the app records "the letter could not be built" and lists the letter in the red box instead of failing silently. (No letter problem to report beyond the earlier ones.)

## 5. Re-verified after the fixes
- **SQLite** (your PC): 1,522 passed, 12 skipped. **PostgreSQL 16** (the live database): 1,534 passed. `manage.py check`: no issues. `makemigrations --check`: no changes (**no new migration this round**). `check --deploy` with live-style settings: the same 3 known warnings (HSTS subdomains, HSTS preload, X-Frame-Options SAMEORIGIN, all chosen on purpose). Bandit: 10 medium (the fixed https calls), 8 low, 0 high, unchanged. pip-audit on the live lock file: none. Secrets scan: 22 placeholders, no real secret.
- **Production-mode server again on the final code** (gunicorn, PostgreSQL, https header): the same results as section 2 point 4, plus: the streamed case-file download works under gunicorn (HTTP 200, a valid zip, `Cache-Control: no-store`), and static files are served with the right type and nothing can be read with `/static/../`.
- **Real Chromium** against a scratch copy poisoned with script-like text in 28 kinds of field (owner and tenant names, addresses, descriptions, notes, history lines, messages, letter problem notes, email errors and bodies, meeting places and guests, standard replies, audit entries): **no script ran anywhere** (manager 60 pages, Board 60, committee 13, owner 8, including every popup and picker). At **phone width (375 px)**: 107 pages as manager, Board, committee and owner: **no sideways scrolling and no script errors** (after the R4-22 fix).
- **Backup and restore round trip** (PostgreSQL): a new key pair, an encrypted dump (1.9 MB; the encrypted file does not start like a dump), decrypted with the private key file, restored into an empty database: the row counts of 8 key tables are identical (20,003 cases, 20,002 notices, 63,162 history lines, 76 migrations including 0045) and the app opens the restored database.
- **Speed** with 20,003 cases (far more than a real HOA): dashboard 0.23 s, a case page 0.12 s, search 0.11 s, committee roster 0.1 s; the full case list (not paged) 4.1 s and the Board queue 3.8 s with thousands of cases waiting; the map data 2.6 s. At a few hundred cases all of these are instant. If the lists ever grow to thousands of cases, adding pages to the list is the next job.
- **Windows traps:** no `%-d`-style date flag and no Unix-only call anywhere in the code; `start-site.bat` unchanged.

## 6. Needs a human decision (each with my recommendation and the one word to answer)
1. **A letter can go twice if the site is restarted at the wrong second (Lob's idempotency key; open since decision 5).** I killed the web server (kill -9) while one letter was "in the air" at a fake print service that logs on receipt: the print service received **5 letters for 4 distinct ones**; the letter in flight was sent again after the restart. A restart (a deploy, or the host's own) during the few seconds a letter is being handed over can therefore repeat that ONE letter. Lob documents an `Idempotency-Key` header for creating letters ([Lob's own announcement](https://lob.com/blog/product-update-idempotent-requests-us-zip-lookups/)); I could not confirm from here how long it is remembered or what Lob does with a key after a refusal, and a wrong choice could stop a retry from ever working. *Recommendation:* add it right after the first run with a Lob TEST key (check both questions there). Until then: do not deploy while letters are being sent (the dashboard shows when). **Answer: `idempotency`** (when you say "go").
2. **Django admin lets the Board and the manager change anything, including a case on their own household** (`STAFF_ADMIN_ACCESS` is on by default; it makes every Board member and the manager a full administrator; every change is written to the audit log, and the admin needs the two-step code). That bypasses the own-household rule and the whole workflow. The independent reader raised it too. *Recommendation:* set `STAFF_ADMIN_ACCESS=0` on the live site so that only one technical administrator account (created once with `createsuperuser`) can use the admin. **Answer: `lock-admin`** (or `leave`).
3. **A missed decision letter cannot be sent again.** If the letter for an imposed fine or a rejection could not be created (R4-15 now says so and warns), there is still no button to create it again; today only a developer can. *Recommendation:* a small "Send the decision letter again" button on an imposed or rejected case that has none, plus a line on the home screen listing such cases (a small new feature: only after your "go"). **Answer: `resend-button`**.
4. **The own-household rule covers only the home your login is attached to.** A Board member who owns a SECOND home here (the same email address on two owner records) can still decide on that second home's cases (I confirmed it with a test). The portal cannot sign such an owner in by link either, because the address is on two homes. *Recommendation:* treat any case whose owner record lists the signed-in person's own email address as their own. **Answer: `second-home`**.
5. **A manager can change a Board member's sign-in email and reset their two-step code** (both need a reason and are audited, and the old address is told). Together they would let a manager take over a Board member's login. That is staff trusting staff, but the manager is employed by the management company. *Recommendation:* only a Board member may do either to another Board member's login. **Answer: `board-only`** (or `leave`).
6. **Cloudflare in front of Render:** the sign-in and reply limits and the lockout use the address the host's proxy appended (`TRUSTED_PROXY_COUNT`, 1 on Render). With Cloudflare in front, that address would be Cloudflare's, so everybody would share a few counters. I cannot check the live header chain from here. *Recommendation:* do not put Cloudflare in front until the live header has been looked at; then set the count to match. **Answer: `verify-proxy`**.
7. **A typed exact new deadline has no upper limit** (a typo such as 2062 is accepted; the "days to add" box is limited to 365). *Recommendation:* refuse a date more than one year away. **Answer: `limit-deadline`** (or `leave`).
8. **An owner who is signed in can set a password without giving a current one** (they sign in by emailed link, so most have none). Low risk (it needs a signed-in session). *Recommendation:* leave. **Answer: `leave`**.
9. **No Content-Security-Policy header** (the pages use inline scripts and styles; adding one safely means rewriting them). Escaping is correct everywhere and the XSS canary passes, so this is defence in depth only. *Recommendation:* later. **Answer: `later`**.
10. **Photo limits:** now that the case file no longer needs hundreds of megabytes, nothing else is needed; if you still want a cap on what an owner can add to one case (today: 30 photos of up to 10 MB), say so. **Answer: `cap-photos`** (or `leave`).

## 7. Could not verify (and why)
- **Every outside service has still never been run for real from here:** Lob (all calls, including its handling of a repeated key), Zoom, Vantaca (the owner API and posting a fine to the ledger), Postmark (decided, not set up), Backblaze and Cloudflare R2 (backups ran against fake storage only), Render (its proxy headers, restart signals, memory size), USPS tracking text. Each is written from the vendor's documentation and is covered by tests against local fakes; the first real run of each should be on a TEST account with fake data.
- **Real phones, real Excel, real mail inboxes:** the browser checks use Chromium at phone width; spreadsheet formula protection was verified by reading the CSV bytes, not by opening Excel; "accepted by the mail server" is not "delivered".
- **Legal text:** whether the notices say what Florida law needs is for the attorney (questions 1-23 are still open).
- **The host's memory size and number of workers:** the case-file fix removes the known memory problem; the live host's real limits were not visible from here.

## 8. Suggested next steps (in order)
1. Update the live site (PowerShell: `git pull`, then `start-site.bat` runs `migrate`; on Render: Manual Deploy > Deploy latest commit, whose pre-deploy step runs `migrate`). **No new migration this round:** only migration 0045 from last time is needed if you have not applied it yet.
2. Answer the decisions in section 6 (the quickest ones first: `lock-admin`, `second-home`, `board-only`).
3. Do the first Lob TEST-key run, and check Lob's idempotency key then (decision 1).
4. Say "go" for the committee notification emails and for the resend-button.


---

# Follow-up 2026-11-25 (after pass 4): the owner's answers, what was built, and corrections to pass 4
Written after the owner answered the ten decisions of "Independent review, pass 4" (its section 6) and said "go" for the committee notification emails and the resend button. Nothing above was deleted or rewritten. **To avoid mixing up the two lists, always cite a section of that pass as "pass 4, section N"** (it restarts its numbering at 1: "pass 4, section 6" = its decisions, "pass 4, section 7" = what could not be verified; "REVIEW_REPORT.md section 7" without the pass name means the older decisions of pass 2).

## F-1. The answers, and what was done with each
| # (pass 4, section 6) | Answer | What was done |
|---|---|---|
| 1 Lob idempotency key | `idempotency` | **Built** (below). Not verified against the real Lob. |
| 2 Django admin bypasses the own-household rule | `leave` | Nothing changed. `STAFF_ADMIN_ACCESS` stays on. |
| 3 Resend a missed decision letter | `resend-button` (+ "go") | **Built** (below). |
| 4 Second home | `second-home` | **Built** (below). |
| 5 Manager can change a Board member's login email / reset their two-step code | `leave` | Nothing changed. |
| 6 Cloudflare in front of Render | `verify-proxy` | No code. A step-by-step check is in PRODUCTION_SECURITY_CHECKLIST.md (addendum 2026-11-25): **do not put Cloudflare in front until it has been done.** |
| 7 Typed deadline with no upper limit | `limit-deadline` | **Built** (below). |
| 8 Owner can set a password without the current one | `leave` | Nothing changed. |
| 9 No Content-Security-Policy header | `later` | Nothing changed. |
| 10 Photo cap | "cap photos to 4" | **Built** (below): an owner can add at most **4** photos to one case. |

## F-2. What was built (each with tests that failed first; one commit each; the letters, `pdf.py`, are untouched)
- **Second home** (`754983a`): `services.lists_login_email` / `own_household`. A person whose own email address (login email or username) is on a home's owner record (main address, a ';' extra or the co-owner's, any capitals, not the tenant's) is deciding on their OWN household's case there too: the same blocked decisions as before, the committee's automatic abstention (`conflicted_members`) and the site-visit block. Moving a case along stays open. 6 tests.
- **One-year deadline limit** (`886309f`): `services.grant_extension` refuses a typed exact date more than 365 days from today (case page, an owner's request, the Board's vote, the second-notice reset all go through it). "Days to add" is unchanged. **Not changed:** the hold's "until" date and the committee's "no fine if cured by" date, which also accept any future date (say so if you want the same limit there). 5 tests.
- **Photo cap** (`499e6d8`): `views.MAX_RESIDENT_PHOTOS_PER_CASE = 4` (was 30); the message names the number; the Association's own photos are not counted; the 10-per-submission limit is unchanged. 4 tests (one older test now sets the cap itself).
- **Lob idempotency key** (`603a6d5`): every letter request carries an `Idempotency-Key` header (`mailing.letter_key`: an opaque hash of the case's private token, the notice, the address label, the service and the street address: the same letter always has the same key, a corrected address or another letter another key). What Lob documents (docs.lob.com, read 2026-11-25 through a web search, not through any call to Lob): header or `idempotency_key` query (we send the header only: both together are refused), up to 256 characters, remembered **24 hours** per mode and resource type, a repeat returns the original letter. 6 tests, including "a hand-over killed at the third letter resends the letter in the air with the SAME key".
  - **What this does NOT prove, and the first Lob TEST-key run must check:** (a) that Lob really answers a repeated key with the original letter (and with the same shape of answer); (b) that a letter Lob REFUSED can be sent again under the same key (the documentation speaks of an "already created resource", so a refusal should leave nothing behind, and a test pins that the retry uses the same key; if Lob remembers refusals, a retried letter would be silently answered with the old refusal and show again in the red box: watch for it); (c) a hand-over resumed more than 24 hours after the crash can still duplicate that one letter (the daily job normally resumes it within a day).
- **Send the decision letter again** (`48f5325`): `services.decision_letter_missing` / `decision_letters_missing` / `resend_decision_letter`. An imposed or rejected case whose Notice of committee decision was never emailed, queued, held, mailed or listed (an empty half-made notice does not count) shows a red box on the case ("The owner has NOT been sent the decision letter", button **Send the decision letter again**) and a line **Decision letters not sent** on the Board / manager home screen. Pressing it locks and re-checks the case (a second press, or a second person, is refused: "already has its Notice of committee decision"); if it fails again the same WARNING is written and the same plain message shown, and nothing half-made is kept. It is a stage move, not a decision, so it also works on your own household's case. Not built (not asked for): a count in the menu's "N to do". 30 tests.
- **A photo whose file is missing from storage** (`a057744`; found by the real-browser check on a scratch copy, not by a decision): `/photos/<id>/` answered with a server error (HTTP 500); it now logs a warning and answers 404 like any photo that is not there, and the case page still opens. 2 tests. (If this ever shows in the live server log, a file was lost from storage or a restore did not include the media files: check the backups.)
- **Committee notification emails** (see F-3).

## F-3. Committee notification emails (`migrate` IS needed: migration 0046 adds `CommitteeMeeting.committee_reminded_on`)
`violations/committee_mail.py`, `settings.EMAIL_TO_COMMITTEE` (default on) and `COMMITTEE_REMINDER_DAYS` (default 7; 0 = no reminder). Recipients: the active members on the roster for the meeting's year who have an email (their login's address, else their owner record's), each address once, each person emailed on their own, addressed by name. **When:** a meeting is created (ONE email for a whole repeating series, listing every date), moved (old and new time; sent even when the owners are not told), cancelled; and one reminder, once per meeting, when it is within 7 days and has at least one case still waiting for the committee (the daily job's new step "committee reminder(s)"; if the job missed days it still goes once). **What they say:** dates, places, the Zoom link, ID and passcode, a link to the meeting page and the COUNT of cases: never an owner's name or address and never a case detail (tested). A change is announced only once it is saved for good (`transaction.on_commit`: an undone change sends nothing), and a mail problem is logged and never stops the meeting from being saved. Every send writes an audit line (counts only). 20 tests.
- **Not decided for you (assumptions, easy to change):** one reminder, a week before; no email when a single case is added to a meeting (the count in the reminder covers it); a committee member with a second home is still emailed the dates (the email shows no case). Say if you want a second reminder (for example the day before) or an email each time a case is added.
- **Real mail never tried:** like all email in this app, these were tested against Django's in-memory outbox; the live mail service is not set up yet (`check_email` first).

## F-4. Corrections to pass 4 (found by the `/code-review` of 2026-11-24, all in these documents)
1. **R4-08 (the "too many tries" window) is reduced, not removed.** The window now ends an hour after the FIRST try (before, every try renewed it). It still does not stop a stranger who knows an owner's email address from asking for sign-in links for that address three times at the start of every hour: the owner's own requests are then refused until the hour ends (the page looks the same either way, by design). It costs the owner at most a wait or a phone call to the office; no account can be entered this way. **New decision 11, `link-limit`:** *Recommendation: `leave`* (the office can always help; the Board and the manager use passwords and the two-step code). The alternative, `ip-too`, would count each visitor address as well as each email address and still leaves a determined attacker with many addresses.
2. **CLAUDE.md's test counts were out of date** (950, then 844). A "superseded" line with the current numbers was added under its "Run / test" bullet (nothing deleted).
3. **Render was described as set up; it is not verified.** RENDER_STAGING.md is the PLAN (branch `claude/brave-galileo-mddme9`, Root Directory `hoa`, Pre-Deploy Command `python manage.py migrate --no-input`, Auto-Deploy No), and CLAUDE.md says staging is "prepared, not yet created"; no `render.yaml` exists. **The update step for Render therefore reads: check in Render's own settings that the service's Branch is `claude/brave-galileo-mddme9` and that the Pre-Deploy Command above is set; then Manual Deploy > Deploy latest commit.** Without that command nobody runs `migrate` and migration 0046 (and 0045) is missing: the site then errors on the first page that reads a meeting. (This was also stated wrongly in the chat reply that followed pass 4; corrected there too.)
4. **Section numbering** (see the top of this follow-up): cite "pass 4, section N".
5. **The idempotency recommendation depended on two answers that could not be checked** (how long Lob remembers a key; what it does after a refusal). Both are listed in F-2 as things the first TEST-key run must check, and Lob's TEST keys may not exercise idempotency at all (CLAUDE.md: test keys do not mail), so the first LIVE letters are the real test: send the first real letters one at a time and look at Lob's dashboard.

## F-5. Could not verify (in addition to pass 4, section 7)
Lob's real handling of a repeated key (F-2); real mail delivery of the committee emails; that the live Render service has the settings RENDER_STAGING.md describes; the real header chain before Cloudflare (PRODUCTION_SECURITY_CHECKLIST.md, addendum 2026-11-25).

## F-6. Suggested next steps (replaces pass 4, section 8)
1. Update: PowerShell `git pull`, then `start-site.bat` (it runs `migrate`: **needed**, migration 0046). Render: confirm Branch and Pre-Deploy Command (F-4 item 3), then Manual Deploy > Deploy latest commit.
2. Answer decision 11 (`link-limit`: `leave` or `ip-too`) and say whether the hold date and the committee's cure-by date should get the one-year limit too (`limit-all` or `leave`).
3. First run with a Lob TEST key (RENDER_STAGING.md section 10), checking F-2's three points; then the first real letters one at a time.
4. Set up the real mail service (Postmark), run `check_email --to <your address>`, then try a committee meeting with the roster loaded and see the committee emails arrive.

## F-7. Checks after this round (2026-11-25)
SQLite **1,595 passed + 12 skipped** (was 1,522 + 12); PostgreSQL 16 **1,607 passed** (was 1,534). 73 new tests in `violations/tests/test_followup5_*.py`, the ones for the changes failed first. `manage.py check` clean; `makemigrations --check` no changes after the one new migration (0046); `violations/pdf.py` and every older migration untouched. Real Chromium at phone width, as manager, Board, committee and homeowner: no sideways scrolling, no script errors, no error pages (the new red "decision letter" box and the home-screen line were looked at in a screenshot). **No independent reviewer read this round's code** (pass 4 had one): it was checked by the tests, my own read and the browser check; run `/security-review` or `/code-review` on commits `754983a` to `a057744` if you want that again. Nothing was sent anywhere and no outside service (Lob, Zoom, Vantaca, Postmark, Backblaze, Cloudflare, Render, USPS) was contacted; the only web access was a search of Lob's public documentation.
