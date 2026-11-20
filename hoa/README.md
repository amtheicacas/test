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
| Compliance Committee | cases the Board has voted on (waiting to be scheduled) and the meetings they are assigned to | read the case file (photos, letters, owner messages); on each meeting's page record attendees, notes, minutes and one decision per violation, then submit the results to the Board. No access to other cases, the dashboard, the map or owner contact details |
| Homeowner | only their own violations | see stage, photos and letters; message the association; upload cure photos |

Create manager, board and committee logins in `/admin/` under Users (add a **Profile** with the role; they then set up two-step login on first sign-in). Homeowner logins are created by invitation (Admin → Owners → *Send portal invitation*), by the Vantaca sync with `--invite`, or by the owner themselves on the *Get access* page.

## Command reference (run from the `hoa` folder; on Windows use `.venv\Scripts\python manage.py ...`)
| Command | What it does |
|---|---|
| `migrate` | Update the database after pulling new code (the `start-site.bat` file does this for you) |
| `load_standards` | Load or refresh the violation types (Community Standards + CC&Rs). It overwrites those rows, so run it after pulls, not after you hand-edit types |
| `daily_jobs` | The daily job: releases held mailings and holds, sends reminders, moves expired cure periods to the Board. Schedule it once a day on the live site |
| `make_login manager\|board\|committee --username x` | Create/reset a staff login for testing |
| `owner_login --address "7316 Great Egret"` | Give an existing owner a portal login for testing |
| `make_test_owner --email you@x.com` | Create a throwaway owner that emails you |
| `seed_demo` | Demo owners, cases and one login per role. Testing only |
| `backdate_meeting [id]` | Testing only: pretend a committee meeting already happened |
| `clear_test_data --yes` | Delete all violations and committee meetings (keeps owners, types, logins) |

Settings can go in a private file, `hoa/local.env` (copy `local.env.example`; it is never saved to GitHub), as `NAME=value` lines. Configuration is by environment variables: `HOA_NAME`, `MANAGER_*`, `OFFICE_PHONE`, `SITE_URL` (used in links), `SECRET_KEY` and `DJANGO_DEBUG=0` for production, `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD` for SMTP (without `EMAIL_HOST`, emails print to the console). Run `python manage.py advance_violations` daily from cron; the dashboard and board queue also do it when opened.

## The Fining Resolution (April 17, 2023)
The letters and rules follow the Resolution Establishing Compliance Committee and Creating Fining/Suspension Procedures:
- **Notice of Violation** (Exhibit 1): first-class mail, "information to believe", cure-by date, the fine/suspension/lawsuit/attorney's-fees warning, and (for rented homes) the owner joint-and-several-liability paragraph. It keeps the courtesy-notice layout.
- **Board vote:** a fine and/or suspension needs a **majority of the Board**; the vote is recorded. A suspension names the Common Areas/facilities and a number of days. The vote only *proposes* it.
- **Notice of Hearing** (Exhibit 2): certified mail with return receipt, plus an optional regular copy (`HEARING_REGULAR_COPY`), at least 14 days ahead (enforced) and **20 recommended** (a warning appears under 20; `HEARING_NOTICE_RECOMMENDED_DAYS`). Wording follows the exhibit: Section 720.305(2)(b), right to counsel with 7 days' notice, hearing in absence, the owner paragraph and suspension sentence for rented homes, a copy of the Notice of Violation attached.
- **Compliance Committee:** 3+ voting members, majority approves; once approved it is imposed with no further Board vote.
- **Notice of Imposition** (Exhibit 3): certified mail, the fine due within 5 days, payable to the Association (`HOA_PAYMENT_PAYABLE_TO`, `HOA_PAYMENT_ADDRESS`), and the suspension (all owners, tenants, occupants, guests; not ingress/egress or parking).
- The Board may skip steps for health/safety threats (the "Refer to Board now" button).
Please have your attorney confirm the wording and that this matches Fla. Stat. 720.305 as it applies to you.

## After the Committee approves: review, post to the ledger, send the letter
When the Compliance Committee approves a fine and/or suspension, the decision (votes, amount, suspension) is **saved and waits**. No letter goes out yet. It appears on the dashboard and the Board queue under *Committee decisions awaiting approval to post & send*. A Board member or the manager reviews it and approves:
1. The **fine is posted to the owner's ledger**: through the Vantaca API once configured (see below), or, until then, by posting it in Vantaca and recording the reference here. A fine is never posted twice; a failed posting changes nothing and can be retried.
2. The **Notice of Imposition** is generated, emailed and mailed (certified) to the owner **and the tenant** if the home is rented, each addressed to them. It states the amount, that it was posted to their ledger, and that it is due 5 days from the letter date. The due date starts at approval.
3. The case moves to *Fine Imposed & Posted to Ledger*; record the payment when it arrives, and the case closes once it is also corrected.
The Committee's approval is what imposes the fine under the Resolution (no new Board vote); this step is the administrative check and posting. Suspension-only decisions skip the ledger but still go through the same approval and letter. Committee members and homeowners cannot approve.
**Posting to Vantaca automatically:** Vantaca's ledger endpoint isn't public, so it is configurable and **has not been run against a live account**. Ask Vantaca (vendorsupport@vantaca.com) for: the endpoint that posts a charge to an owner's account, its required fields, the charge/GL code for violation fines, an idempotency or reference field, and a test environment. Then set `VANTACA_LEDGER_POST_PATH`, `VANTACA_FINE_CHARGE_CODE` and, if their field names differ, `VANTACA_LEDGER_BODY` and `VANTACA_LEDGER_REF_KEY`. Owners need their Vantaca account ID (from the owner sync). Detecting payments from Vantaca automatically is not built yet.

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

## Certified mail tracking
With the print/mail provider (Lob) turned on, every letter handed over is recorded as a **mail piece** (owner mailing address, property address, tenant; certified or first class). For certified pieces the USPS tracking number comes back from Lob and is stored on the case, shown to staff and to the owner with a **Track on USPS** link, and the app asks Lob for tracking updates in the daily job (`daily_jobs`) and on the case's **Refresh tracking** button: status, event history, expected and actual delivery date. A delivered certified letter fills in the return-receipt date. Returned, re-routed or refused mail is flagged on the dashboard. Without Lob, enter tracking numbers by hand under **Mailing log**. This has not been run against a live Lob account: check it with a Lob test key first (the event names Lob returns may need small adjustments).

## Automatic print & certified mail
Set `MAIL_PROVIDER=lob`, `LOB_API_KEY` (**start with a Lob test key**) and the `HOA_RETURN_*` address. Every notice is then emailed and sent to Lob for printing and mailing (owner and tenant). Hearing notices go certified with return receipt **and** first-class; others go first-class. With the default `manual` provider, nothing is sent: print the PDF and log the mailing by hand. Owners need street, city, state and ZIP (the Vantaca sync fills these). A provider failure never blocks the case; it shows on the notice and the dashboard counts it as unmailed. The Lob client matches their documented API but hasn't been run against a live account.

## Getting homes and owners in (do this before the Vantaca API is set up)
1. **Import owners** (manager menu): upload a CSV, see a preview and any problems (missing ZIP, bad emails, duplicate addresses), then import. A template is one click away; the same columns are what a Vantaca export provides, and the later API sync uses the same code, so nothing is thrown away. Re-importing updates people (matched on Vantaca account ID, then address) and never deletes.
2. **Map:** click *Locate homes on the map* (or run `python manage.py geocode_owners`). It uses the free US Census batch geocoder. Units like "#52" are looked up by the building and fanned out in a small ring so every home has its own dot. The map has an address/owner search. Not yet run against the live Census service: check the first batch.
3. **Rented homes:** put the tenant's name (and email) on the owner record, by import or in Admin. Every notice then goes to the owner(s) and the tenant, each emailed separately by name with their own PDF, plus printed/mailed letters for both. The letters carry the Resolution's owner joint-and-several-liability wording.
4. **Several logins per household:** everyone linked to an owner record (owner, co-owner, tenant, other, up to `MAX_LOGINS_PER_HOUSEHOLD`=6) signs in on their own and sees the same violations, letters and messages; each message shows who wrote it. **Residents cannot edit any of their information.** Their **Household** page is read-only (it shows what is on file and who has a login, and tells them to contact the property manager to correct anything). Anyone whose email is on the record (owner, co-owner, extra owner emails or tenant) can use *Get access* to create their own login; staff can add other logins in Admin. Details come from Vantaca (import now, API later), which is the single source of truth: an import overwrites hand edits. When an owner leaves in the Vantaca feed, every login in the household is switched off.

## Map
**Views and labels:** *Site plan* is the default: the community's plat with a box for every house at its lot position (lots 1-95), colored by status, with the lot number inside; it needs no internet and no address lookup because it goes by the **Lot no.** on each owner. *Street map* and *Satellite* use real coordinates (run *Locate homes* first). *Labels* toggles Address and Lot no. beside each home (labels appear once you zoom in). Your last view is remembered. The plat image and lot positions were made from the supplied plat PDF with `tools/build_site_plan.py` (the artwork is the builder's "artist's rendering ... not an official site plan", so it carries a not-to-scale note; confirm you may use it, or swap in the HOA's own plan and re-run the tool). Satellite uses Esri World Imagery for testing; for production use a provider with a key (`MAP_SATELLITE_URL`).

**Missing lot number:** if exactly one house has a blank Lot No. and exactly one number from 1 to N is unused, the import fills it in and tells you in the preview; anything less certain is left alone and flagged.

**Filters:** tick any combination of stages (notice issued / cure period, board review / fine issued by Board, sent to Compliance Committee / hearing, fine imposed & posted to ledger, on hold, sent to legal, resolved / closed) and choose how recently the violation was issued (all time, or the last 30, 60, 90 or 180 days). Dots re-color from only the cases that match, a summary shows how many homes and cases match, and each stage shows its count. *Show homes with no violations* hides or shows the gray dots. The age is counted from the date the first Notice of Violation was issued.
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

## Violation Issuance Timing
When a violation is entered you choose how it goes out:
- **Standard (default):** emailed to the owner (and tenant) at once; the printed US Mail copy is held `MAIL_DELAY_BUSINESS_DAYS` (default 2) business days (Mon-Fri, holidays not skipped). If the violation is marked corrected or dismissed in that time, the mailing is cancelled and the postage is saved. Held mailings are released by `python manage.py daily_jobs` (schedule it daily) and also when a manager opens the dashboard; a **Mail now** button on the case releases one early.
- **Immediate - urgent:** email and US Mail go out right away.
- **Draft:** saved without sending; issue it later from the case page with either timing.
The cure period runs from the day the email goes out. A cure period of 0 makes the letter say "immediately (no later than 12 hours after the date of this correspondence)".

## Putting a case on hold
A hold needs an **On hold until** date. The owner gets an email 7 and 2 days before it ends (`REMINDER_DAYS`) telling them to take action. On that date (checked by `daily_jobs` and whenever a manager opens the dashboard) the hold lifts itself: the case shows **Off hold: cure period restarted**, the cure period starts over from that day (earlier extensions are cleared) and the owner is emailed the new deadline. "Lift hold now" does the same early. Only holds placed before a hearing is set restart the cure period; a case held at the committee or imposition stage goes back to where it was.

## The owner portal's front page
After signing in an owner lands on a page of **tiles**, one per part of the portal: Violations (open count, next cure date and a status icon: green all clear, yellow in cure period, orange with the Board, red with the Compliance Committee or due soon), Account details (read-only), HOA documents and Sign-in options. The list of tiles lives in `violations/portal.py`: to add a module (ARC requests, inquiries...) add one function there and list it in `OWNER_MODULES`. Staff keep the top-bar navigation.

## How owners sign in
Owners don't need a password. On the sign-in page they enter the email address on file (any email in the Vantaca Email field, the co-owner's, or the tenant's while the lease is active); the app emails a one-time link that is good for 15 minutes. The link opens a page with a **Sign in** button (so email scanners can't use it up), and the owner then stays signed in on that device for 30 days. The page gives the same answer whether or not the email is on file. An owner who wants a password can set one under **Sign-in options**, and can reset it themselves with **Forgot your password?** (emailed link, good for 24 hours); nobody at the office has to reset anything. Owners can't change their email: it comes from the management company (Vantaca), and when it changes the old address stops working and the new one works. Board, manager and committee sign in with a password and the two-step code. Email must be set up for any of this to work for real people (see README, email).

## Testing as an owner
`python manage.py owner_login --address "7316 Great Egret"` gives that existing owner a portal login (default password `demo-password-123`). `make_test_owner --email you@example.com` creates a throwaway test owner instead. Testing only.

## Send to legal
"Send to legal…" on a case opens a two-step flow. Step 1 records the referral: if a fine was imposed and the violation is still not corrected it is the **normal path**; from any other stage it is a **direct referral** (the Board vote, hearing and fine steps are shown as bypassed, and a reason is required). Step 2 is a **drafted email to the attorney** (case summary, timeline, status, owner replies) with every letter sent to the owner and the violation photos as attachments. Nothing is sent automatically: you edit the draft, then click Send, or download everything as a .zip to send from your own email. Set `ATTORNEY_NAME` and `ATTORNEY_EMAIL` to pre-fill the address. Cases referred but not yet emailed show on the dashboard.

## Compliance Committee meetings: results, minutes and administrative review
Each committee meeting has a working page (committee members log in and land on it). They record who attended, notes and the minutes, then one decision per violation: fine confirmed, fine confirmed with a new amount, waived/resolved (closes it; allowed even before the hearing), or fine rejected. Votes and a short finding are recorded. **Submit results to the Board** sends the whole list for review; nothing is imposed yet. Board members and the manager see the meeting under **Committee results** (and as a dashboard task): each row shows the original fine, the committee's amount and recommendation, and the Board approves it (a fine is posted to the ledger and Letter 3 is sent), modifies it (lower the fine, never above the hearing-notice amount; or waive), or returns it to the committee. That page is the permanent record of the meeting.

### Minutes
The committee does not have to type minutes. On the meeting page they enter who was present or absent, other attendees, the times and notes, and one decision per violation (including whether the owner attended); the app writes the minutes from those entries (HTML and PDF). After the meeting they stay a **draft**. When the next meeting is on the calendar, its page and the committee home page list the earlier draft minutes for approval; approving locks the entries. An uploaded minutes file is still optional.

### After the Committee decides
Submitting the meeting's results sends them for **administrative review** (the Association's office, i.e. Board / Manager). Approving a fine posts it to the ledger and generates the Fine Imposed letter (Letter 3), which goes by certified mail through the print/mail provider with a copy by email. If the violation is still not corrected, the case is then **sent to legal** ("Fine imposed — sent to legal").

### Committee roster
The committee members are loaded once a year under **Committee members** (managers and board members edit; committee members can read it). "Copy last year's roster" carries the same people forward. On each meeting page the roster is listed and you tick **Present / Absent / Excused** for each member; the minutes use those ticks (with a note if fewer than three members were present, the number needed to vote on a fine). Extra members and other attendees can still be typed in.

### Zoom
With Zoom connected, scheduling a meeting creates the Zoom meeting for you (waiting room on): its link, ID, passcode and dial-in numbers are filled in and go straight onto the hearing notices, so nobody has to go to Zoom. To connect it, a Zoom account admin creates a **Server-to-Server OAuth app** (Zoom App Marketplace, Develop, Build App) with the scope to create meetings, then set `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` and `ZOOM_HOST_EMAIL` (the email of the Zoom user who hosts; required). This follows Zoom's documented API but has not been run against a live Zoom account: create one test meeting and check it. If Zoom can't be reached the app says so and you can paste a link as before.

### Former owners (kept 3 years)
Owners who move out or sell are never deleted by the sync: they are marked **inactive** with the date and a reason, their logins (and a tenant's, when the lease ends) switch off, and they drop out of lists, the map and the new-violation form. Their violations, letters and emails stay attached to them. When Vantaca shows a different owner at an address (a sale), the old owner is archived and the new owner gets a fresh record, so the new owner starts with a clean history. Find them under **Former owners** (search by name, address, lot, email, tenant or account number), where they can also be reactivated. Records are kept `ARCHIVE_RETENTION_YEARS` (3) years; `python manage.py purge_archived_owners` lists what is past that and, only with `--yes`, deletes it. In Admin you can also tick owners and "Mark as moved out / sold".

### Committee logins
Committee members change each year. On the roster you can link a member to their login; that login then switches on and off by itself with the current year's roster (the daily job and every roster edit apply it). The roster page lists committee logins that aren't linked to anyone.

### Hearing banner for owners
When an owner has a case assigned to an upcoming committee meeting, their home page opens with a banner ("You have a homeowner hearing in 9 days", with the date, time and days remaining). It opens a page with the Zoom/location details and only that owner's own violations on the agenda. Owners never see anyone else's items, a count of them, or the full agenda.
