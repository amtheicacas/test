# HOA Violation Manager (Django)

Tracks HOA violation notices from first notice through board vote, fining-committee hearing and fine.

## Run
```
cd hoa
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser      # superusers act as managers
python manage.py runserver            # http://localhost:8000
python manage.py seed_demo            # optional demo data + one login per role (password demo-password-123; never in production)
```
Add **Owners** (or sync them from Vantaca) and **Violation types** in `/admin/`. Tests: `pytest`.

## Who can do what
| Role | Sees | Can do |
|---|---|---|
| Manager (Sunstate staff; also superusers) | everything + admin | all actions |
| Board member | everything | all case actions: board votes, meetings, hold, legal |
| Compliance Committee | only cases sent to a committee meeting, for those meetings | read the case file (photos, letters, owner messages) and record the hearing vote. No access to other cases, the dashboard, the map or owner contact details |
| Homeowner | only their own violations | see stage, photos and letters; message the association; upload cure photos |

Create manager, board and committee logins in `/admin/` under Users (add a **Profile** with the role; they then set up two-step login on first sign-in). Homeowner logins are created by invitation (Admin → Owners → *Send portal invitation*), by the Vantaca sync with `--invite`, or by the owner themselves on the *Get access* page.

Configuration is by environment variables: `HOA_NAME`, `MANAGER_*`, `OFFICE_PHONE`, `SITE_URL` (used in links), `SECRET_KEY` and `DJANGO_DEBUG=0` for production, `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD` for SMTP (without `EMAIL_HOST`, emails print to the console). Run `python manage.py advance_violations` daily from cron; the dashboard and board queue also do it when opened.

## Extensions and reminders
- **Approving more time adds the days to the current deadline** (not to today), so the case ages as if the cure period had always been that long: a 15-day notice with 10 approved days is a 25-day cure period. The case card shows "15 days + 10 extended".
- When an owner asks for more time (in the portal, with the number of days), the board queue and the case show **Approve**, which you can **edit** (change the days, or set an exact date), or **Decline** with a reason the owner sees and is emailed. Staff can also grant time on their own. Approving sends the owner an extension letter (email plus mail) or, if ticked, a full second Notice of Violation.
- **Reminder emails** go to the owner (and tenant) as the deadline nears: by default 7 and 2 days before (`REMINDER_DAYS="7,2"`). Each reminder is sent once; if the job missed a day, only the most urgent one is sent; none are sent for cases on hold, past the deadline, or when the owner has said it's fixed and is waiting for reinspection. An extension restarts the reminders for the new deadline. Run `python manage.py daily_jobs` once a day (it sends reminders and refers expired cases to the board).

## Picking a violation from the Community Standards
`python manage.py load_standards` loads 81 violation types built from the Seventh Edition (October 2026) Community Standards: Article 3 sections plus the Article 4 landscaping rules (`seed_demo` runs it too). Each has a category, the **verbatim rule text**, a drafted "what was observed", a drafted cure, and the cure period the standards themselves specify (7 days for light bulbs, 14 for house numbers, 30 for failing plants, 45 for repainting, and so on). Several busy sections are split (garbage cans: visible vs. out too early; parking: street, sidewalk, unlicensed, grass).
On **+ New**, describe what you saw, or browse by category, and click a rule. It fills in the section, observation, cure and days for you to edit. Anything left in **[square brackets]** must be replaced before the notice can be sent. Search is local keyword search (title and everyday words weigh most). Setting `ANTHROPIC_API_KEY` adds an **Ask AI** button that uses Claude (`AI_MODEL`, default `claude-opus-5-5`) to pick the best matches; only the typed description and the public rule titles are sent, never owner names or addresses. If the AI is unavailable it quietly falls back to keyword search. To regenerate the list after the standards are revised, see `tools/build_standards_catalog.py`.

## The letter
Notices follow the Association's courtesy-notice layout: HOA letterhead, date and owner on the left with the violation photo and its timestamp (from the photo's own date when available) on the right, the RE: line, **VIOLATION** and **COVENANTS** blocks quoting the rule, the observation and cure, a **"On Behalf of the Board of Directors"** sign-off, and a second-page **Violation Correction Response Form**. No management company appears. Set the letterhead with `HOA_ADDRESS_LINE1`, `HOA_ADDRESS_CITY`, `HOA_ADDRESS_STATE`, `HOA_ADDRESS_ZIP`, `HOA_PHONE`, `HOA_WEBSITE` and `HOA_CONTACT_EMAIL` (where owners send the response form); anything left blank is simply omitted. Hearing, extension and fine notices use the same letterhead and sign-off; the hearing notice attaches the original Notice of Violation.

## Signing in and security
- **Homeowners** don't need an administrator: they go to *Get access* (linked from the sign-in page), enter the email Vantaca has on file, and get a link to set a password (valid 3 days). Unknown emails get the identical reply, and requests are throttled.
- **Vantaca keeps logins current.** `python manage.py sync_vantaca --invite --deactivate-missing` (run daily): new owners with an email are invited; if an owner's email changes in Vantaca their login follows; owners who disappear from a *full* feed are marked inactive and lose access (and return if they reappear). It refuses to deactivate anyone if the feed is empty or would remove over 20% of owners (`--force` overrides).
- **Two-step login (authenticator app)** is required for manager, board and committee (`MFA_REQUIRED_ROLES`), optional for homeowners (Two-step login in their menu). Lost phone: in `/admin/` delete the person's TOTP device under *Totp devices*, and they re-enrol at next sign-in. If the only manager is locked out, an administrator can do the same from `python manage.py shell`.
- **Lockout:** 5 wrong passwords locks that account+address for 1 hour. Passwords: 10+ characters, not common/numeric/similar to the username. Sessions last 12 hours. Authenticator codes can't be replayed and retries are slowed.
- **The emailed/mailed private link** only works during the cure period. After that (board, committee, fine) the owner must sign in.
- For local demos, set `MFA_REQUIRED_ROLES=` (empty) to skip two-step login.
- **Not yet built:** a log of who viewed which case; single sign-on (e.g. Microsoft) for the board and management company. They sign in with their own password and authenticator app instead; SSO can be added if they all share one provider. The get-access throttle uses the app cache, so use a shared cache (e.g. Redis) when running more than one server instance.

## Automatic print & certified mail
Set `MAIL_PROVIDER=lob`, `LOB_API_KEY` (**start with a Lob test key**) and the `HOA_RETURN_*` address. Every notice is then emailed and sent to Lob for printing and mailing (owner and tenant). Hearing notices go certified with return receipt **and** first-class; others go first-class. With the default `manual` provider, nothing is sent: print the PDF and log the mailing by hand. Owners need street, city, state and ZIP (the Vantaca sync fills these). A provider failure never blocks the case; it shows on the notice and the dashboard counts it as unmailed. The Lob client matches their documented API but hasn't been run against a live account.

## Map
`/map/` shows homes colored by status (red: board/committee/fine/legal; orange: open or on hold; green: resolved; gray: none) with hover details. Owners need coordinates: `python manage.py geocode_owners` looks them up with the free US Census geocoder, or the Vantaca CSV can supply latitude/longitude columns. Architecture-committee requests are not built yet but would be another layer on this map.

## Process implemented (matches the Heron Landing procedure and flowchart)
New cases can be issued immediately on entry (default on). Cases can be **put on hold** (the cure clock pauses) or **sent to legal** at any stage. Owners see a six-step tracker: Notice issued, Cure period, Fine issued by Board, Violation sent to Compliance Committee / Homeowner Hearing, Fine Imposed & Posted to Ledger, Closed. (The app doesn't post to Vantaca's ledger: record the payment here and post in Vantaca.)

1. **Draft** — description, rule cite, required cure, cure days (default 15), photo(s).
2. **Notice of Violation** — starts the clock; emailed (owner and tenant if the home is rented) as a PDF with the photo, cure and deadline plus a private portal link. Print the PDF (owner and tenant versions) and mail it first-class; log the mailing.
3. **Owner response** — in the portal the owner can reply, say it's fixed (flags the case for **reinspection**), upload photos or ask for more time. The dashboard flags messages waiting past the 48-hour reply target.
   - Staff can set a new deadline as a short extension letter or a full **optional second notice**, mark the case corrected after reinspection, or dismiss it if there's no violation.
   - In a **safety situation**, staff can refer to the Board before the deadline.
4. **Board queue** — after the cure period the case lands here. The Board votes to **propose** a specific fine (checked against the $1,000 cap) and/or a suspension, dismiss, or give more time. Nothing is owed yet.
5. **Committee meeting batch** — create a meeting (date and Zoom link) under *Committee meetings* and send all board-approved cases to it at once, or one at a time. The cases become *Sent to Compliance Committee* and the committee gets one agenda. **Notice of Hearing** — at least 14 days ahead (enforced). The letter states the proposed amount ("the Board voted to levy…"), the date and place or virtual link, the right to counsel (7 days' notice), and attaches a copy of the Notice of Violation. Log certified (with return receipt) and regular mailing.
6. **Compliance Committee** — enter the vote (3+ members). It confirms or rejects the Board's proposal; it doesn't change the amount.
7. **Imposition** — a majority approval is final and no further Board vote is needed. The Notice of Imposition is emailed and printed, and the fine is due 5 days after the letter date. A rejection imposes nothing.
8. **Close or refer** — the case closes automatically once it is corrected **and** any fine is paid; otherwise refer it to the attorney.

Every action is on the case timeline. Association/manager details and fine rules are settings (`HOA_NAME`, `MANAGER_*`, `OFFICE_PHONE`, `FINE_AGGREGATE_CAP`, `HEARING_NOTICE_DAYS`, etc.).

## Things to verify / next steps
- Letter wording in `violations/pdf.py` is a template built from your guide. Have your attorney review it.
- Not yet built: the guide's rules on fining a tenant, the owner or both (the app tracks one fine per case), per-day fine calculation (the Board enters the total), roles (all staff can do everything), a Compliance Committee member roster with conflict checks, and certified-mail integration.
- Photos are stored on local disk behind login or the private token. Use object storage for production.

## Vantaca homeowner sync
Vantaca stays the source of truth for owner names, emails, phones and addresses; this app only reads.
```
python manage.py sync_vantaca --csv owners.csv   # works today: use any Vantaca owner/homeowner export
python manage.py sync_vantaca                    # API: needs VANTACA_BASE_URL / _OWNERS_PATH / _USER / _PASSWORD
```
Owners are matched on Vantaca's account ID, then property address, so hand-entered owners get linked, not duplicated. Notes and violations are never overwritten. Schedule it daily from cron. The API client is untested against a live Vantaca account; adjust `FIELD_ALIASES` in `violations/vantaca.py` to match the real response.
