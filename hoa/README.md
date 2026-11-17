# HOA Violation Manager (Django)

Tracks HOA violation notices from first notice through board vote, fining-committee hearing and fine.

## Run
```
cd hoa
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver        # http://localhost:8000  (staff login)
```
Then open `/admin/` and add **Owners** (property, name, email, mailing address) and **Violation types** (governing-document cite, default cure days). Tests: `pytest`.

Configuration is by environment variables: `HOA_NAME`, `HOA_MAILING_ADDRESS`, `SITE_URL` (used in the resident links), `SECRET_KEY`, `DJANGO_DEBUG=0` for production, and `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD` for SMTP (without `EMAIL_HOST`, emails print to the console). Run `python manage.py advance_violations` daily from cron; the dashboard and board queue also do it when opened.

## Process implemented (matches the Heron Landing procedure and flowchart)
1. **Draft** — description, rule cite, required cure, cure days (default 15), photo(s).
2. **Notice of Violation** — starts the clock; emailed (owner and tenant if the home is rented) as a PDF with the photo, cure and deadline plus a private portal link. Print the PDF (owner and tenant versions) and mail it first-class; log the mailing.
3. **Owner response** — in the portal the owner can reply, say it's fixed (flags the case for **reinspection**), upload photos or ask for more time. The dashboard flags messages waiting past the 48-hour reply target.
   - Staff can set a new deadline as a short extension letter or a full **optional second notice**, mark the case corrected after reinspection, or dismiss it if there's no violation.
   - In a **safety situation**, staff can refer to the Board before the deadline.
4. **Board queue** — after the cure period the case lands here. The Board votes to **propose** a specific fine (checked against the $1,000 cap) and/or a suspension, dismiss, or give more time. Nothing is owed yet.
5. **Notice of Hearing** — at least 14 days ahead (enforced). The letter states the proposed amount ("the Board voted to levy…"), the date and place or virtual link, the right to counsel (7 days' notice), and attaches a copy of the Notice of Violation. Log certified (with return receipt) and regular mailing.
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
