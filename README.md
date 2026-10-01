# ICAC Membership Manager

Flask + SQLite membership system: lead tracking, public applications, member records, dues, events and a members-only portal.

## Run
```
pip install -r icac/requirements.txt
cd icac
flask --app app create-admin      # first staff admin (prompts for password)
flask --app app run               # http://127.0.0.1:5000
python seed.py                    # optional demo data (admin@icac.test / demo-password-123)
```
Tests: `PYTHONPATH=icac pytest tests`. Set `SECRET_KEY` in production (otherwise one is generated in `icac/.secret_key`) and serve behind HTTPS.

## What it does
| Area | Features |
|---|---|
| Leads | Pipeline (new → contacted → qualified → converted/lost), owner, source, touchpoint log |
| Applications | Public `/apply` form auto-creates/advances a lead; staff approve (creates member + portal login with one-time temp password) or reject |
| Members | Contact info, tier, status, renewal date, tags, notes, searchable list, CSV export |
| Tracking | Per-member timeline (notes, status changes, payments, RSVPs, profile edits), payments ledger, event attendance, dashboard (pipeline, renewals due, YTD dues) |
| Portal | Announcements, events + RSVP, profile self-service, opt-in directory, billing history, resources (active members only) |
| Admin | Tiers and staff accounts; roles: admin / staff / member |

## Notes / next steps
- Passwords are hashed; first-login password change is forced; all POSTs are CSRF-protected.
- No email or online payments yet: temp passwords are shown to staff once, and dues are recorded manually. Natural next steps are SMTP invites/renewal reminders and Stripe checkout.
- Lapsed-status automation (flip to `lapsed` after `renews_on` passes) is not scheduled; run it from cron or add a daily job.
