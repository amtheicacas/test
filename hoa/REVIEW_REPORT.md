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
