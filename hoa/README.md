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

## Process implemented
1. **Draft** — description, required cure, cure days, photo(s).
2. **Issue notice** — starts the cure clock, emails the owner a PDF (photo, cure, deadline) with a private reply link. Print the same PDF from the case page, mail it, and log the date / tracking number.
3. **Owner response** — the private link lets the owner reply, request more time and upload cure photos. Staff reply, grant an extension (new deadline + extension notice) or mark cured.
4. **Cure period expires** — the case moves to the **board queue**. Board votes: proceed / dismiss / grant more time.
5. **Hearing** — on "proceed", schedule a fining-committee hearing. The app enforces at least 14 days' notice and emails the hearing notice (print and mail too).
6. **Committee decision** — requires 3+ voting members and a majority to approve; then a fine notice is issued. Fines are checked against the $100 / $1,000 caps (Fla. Stat. 720.305(2)) unless your documents say otherwise.

Every action is recorded on a case timeline.

## Things to verify / next steps
- Wording in `violations/pdf.py` is a template. Have your association attorney review it, along with the process against your declaration and bylaws. The stage order follows Fla. Stat. 720.305 (committee approval is the gate for fines), not "fine, then review".
- Letters are generated for you to print and mail; a mail-service integration (Lob, USPS) isn't included. Mail the notice by the method your documents require (some need certified mail).
- Photos are stored on local disk behind login or the private token. Use object storage for production.
- Not yet built: user roles (all staff can do everything), suspension of use rights, repeat-offense tracking, fine payments/ledger, a custom flowchart (send it and I'll match it).

## Vantaca homeowner sync
Vantaca stays the source of truth for owner names, emails, phones and addresses; this app only reads.
```
python manage.py sync_vantaca --csv owners.csv   # works today: use any Vantaca owner/homeowner export
python manage.py sync_vantaca                    # API: needs VANTACA_BASE_URL / _OWNERS_PATH / _USER / _PASSWORD
```
Owners are matched on Vantaca's account ID, then property address, so hand-entered owners get linked, not duplicated. Notes and violations are never overwritten. Schedule it daily from cron. The API client is untested against a live Vantaca account; adjust `FIELD_ALIASES` in `violations/vantaca.py` to match the real response.
