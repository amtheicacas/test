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

Create logins in `/admin/` under Users (add a **Profile** with the role). For homeowners, select owners in Admin → Owners → *Send portal invitation*; they get an email to set a password. The emailed private link still works without a login.

Configuration is by environment variables: `HOA_NAME`, `MANAGER_*`, `OFFICE_PHONE`, `SITE_URL` (used in links), `SECRET_KEY` and `DJANGO_DEBUG=0` for production, `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD` for SMTP (without `EMAIL_HOST`, emails print to the console). Run `python manage.py advance_violations` daily from cron; the dashboard and board queue also do it when opened.

## Automatic print & certified mail
Set `MAIL_PROVIDER=lob`, `LOB_API_KEY` (**start with a Lob test key**) and the `HOA_RETURN_*` address. Every notice is then emailed and sent to Lob for printing and mailing (owner and tenant). Hearing notices go certified with return receipt **and** first-class; others go first-class. With the default `manual` provider, nothing is sent: print the PDF and log the mailing by hand. Owners need street, city, state and ZIP (the Vantaca sync fills these). A provider failure never blocks the case; it shows on the notice and the dashboard counts it as unmailed. The Lob client matches their documented API but hasn't been run against a live account.

## Map
`/map/` shows homes colored by status (red: board/committee/fine/legal; orange: open or on hold; green: resolved; gray: none) with hover details. Owners need coordinates: `python manage.py geocode_owners` looks them up with the free US Census geocoder, or the Vantaca CSV can supply latitude/longitude columns. Architecture-committee requests are not built yet but would be another layer on this map.

## Process implemented (matches the Heron Landing procedure and flowchart)
New cases can be issued immediately on entry (default on). Cases can be **put on hold** (the cure clock pauses) or **sent to legal** at any stage. Owners see a six-step tracker: notice issued, cure period, board review, Compliance Committee, decision, closed.

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
