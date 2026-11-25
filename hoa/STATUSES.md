# Statuses and labels, for review (regenerated)

Generated from the code (`python manage.py print_statuses`), so this is exactly what the app does. Change any wording and send it back.

**How it works.** Every case has one internal status. The app shows it two ways:
- **Owners** see the Association's six procedure steps with a date, one plain sentence for the current step, and a short colored status label.
- **Board / Manager** see seven steps (the owner's "Fine issued by Board" is split into **Awaiting Board vote** and **Fine issued by Board**), and under each step the sub-steps (done / current / not yet / bypassed) with dates and notes.

Manager and Board have identical access. "Sent to legal" counts as open: owners can still message.

## Status by status


## Owner status labels (the colored pill)
In Cure Period (yellow) · Sent to Board for Fine (orange) · Hearing Being Scheduled (orange) · Sent to Compliance Committee (red) · Decision Being Processed (red) · Fine Imposed (red) · Sent to Legal (red) · Fine Imposed, Sent to Legal (red) · On Hold · Verifying Your Correction (yellow) · Closed: Corrected · Closed: Waived · Closed: No Fine · Closed: Dismissed · Closed

## Committee meeting minutes
Draft: awaiting approval → Approved (at the next meeting). Meeting results: Committee working → Awaiting administrative review → Reviewed and processed.

## Flags and badges (labels, not statuses)
Point in time (purple) · Safety issue · Board declared emergency · Re-inspect · Mail held until [date] · Mailing cancelled: corrected first · Direct referral / After the fine (legal) · Unread message (envelope)
