# Statuses and labels, for review (regenerated)

This is generated from the code (`python manage.py print_statuses`), so it is exactly what the app does. Change any wording and send it back.

**How it works.** Every case has one internal status. The app shows it two ways:
- **Owners** see the Association's six procedure steps with a date, one plain sentence for the current step, and a short colored status label.
- **Board / Manager** see the same six steps, and under each one the sub-steps (done / current / not yet / bypassed) with dates and notes.

Manager and Board now have identical access.

## Status by status
| Internal status | Staff label | Owner pill | Current step (owner) | What the owner reads | Staff: current step and its sub-steps |
|---|---|---|---|---|---|
| draft | Draft | (not visible) | - |  | - |
| notice_sent | Cure period running | In Cure Period | Cure period | Please correct this by Nov 29, 2026. You can reply, upload a photo of the fix, or ask for more time. | **Cure period**: Cure period (current); Owner replied (todo) |
| pending_board | Awaiting board vote | Sent to Board for Fine | Fine issued by Board | The cure time has ended. The Board is reviewing this violation. | **Fine issued by Board**: Waiting for Board vote (current); Board voted (todo); Assigned to a committee meeting (todo) |
| hearing_needed | Board approved — schedule hearing | Hearing Being Scheduled | Fine issued by Board | The Board has issued a fine, which is subject to a Compliance Committee hearing. A hearing date is being scheduled. | **Fine issued by Board**: Waiting for Board vote (done); Board voted (done); Assigned to a committee meeting (current) |
| hearing_scheduled | Sent to Compliance Committee | Sent to Compliance Committee | Violation sent to Compliance Committee / Homeowner Hearing | A hearing is set for Dec 9, 2026. Your hearing notice explains how to attend. | **Violation sent to Compliance Committee / Homeowner Hearing**: Hearing notice sent (certified) (todo); Hearing held (todo); Committee decision recorded (todo); Meeting results submitted to the Board (todo) |
| on_hold | On hold | On Hold | Cure period | Please correct this by Nov 29, 2026. You can reply, upload a photo of the fix, or ask for more time. This case is on hold until Dec 3, 2026. | **Cure period**: Cure period (current); Owner replied (todo); On hold (current) |
| ledger_pending | Committee decided: awaiting Board review | Committee Decision Pending | Fine Imposed & Posted to Ledger | The Committee has decided your case. The Board is finalizing the decision and you will receive a letter. | **Fine Imposed & Posted to Ledger**: Board review of the Committee's decision (current); Decision letter sent (certified) (todo); Posted to ledger (todo); Payment received (todo) |
| waived | Waived by the Committee (closed) | Closed: Waived | Closed | The Committee waived this violation. This matter is closed. | **Closed**: Closed (done) |
| fine_approved | Imposed by committee — awaiting payment / cure | Fine Imposed | Fine Imposed & Posted to Ledger | A fine of $100.00 was imposed, due Dec 24, 2026. Please see your decision letter. | **Fine Imposed & Posted to Ledger**: Board review of the Committee's decision (todo); Decision letter sent (certified) (todo); Posted to ledger (done); Payment received (current) |
| fine_rejected | Rejected by committee | Closed: No Fine | Closed | The Committee did not approve a fine. This matter is closed. | **Closed**: Closed (done) |
| referred_legal | Referred to attorney | Referred to Association Attorney | Referred to the attorney | This matter has been referred to the Association's attorney. | **Referred to the attorney**: Referred to the attorney (done); Email to the attorney (current) |
| closed | Closed (cured and paid) | Closed | Closed | This violation was corrected and any fine paid. This matter is closed. | **Closed**: Closed (done) |
| cured | Cured | Closed: Corrected | Closed | This violation was corrected. Thank you. | **Closed**: Closed (done) |
| dismissed | Dismissed | Closed: Dismissed | Closed | This violation was dismissed. No further action is needed. | **Closed**: Closed (done) |

**The six steps and their sub-steps (Board / Manager view)**

- **Notice issued**: Notice issued; Emailed; US Mail copy
- **Cure period**: Cure period; Owner replied
- **Fine issued by Board**: Waiting for Board vote; Board voted; Assigned to a committee meeting
- **Violation sent to Compliance Committee / Homeowner Hearing**: Hearing notice sent (certified); Hearing held; Committee decision recorded; Meeting results submitted to the Board
- **Fine Imposed & Posted to Ledger**: Board review of the Committee's decision; Decision letter sent (certified); Posted to ledger; Payment received
- **Closed**: Closed

## Owner status labels (the colored pill)
In Cure Period (yellow) · Sent to Board for Fine (orange) · Hearing Being Scheduled (orange) · Sent to Compliance Committee (red) · Committee Decision Pending (red) · Fine Imposed (red) · Referred to Association Attorney (red) · On Hold · Verifying Your Correction (yellow, when staff marked it for re-inspection) · Closed: Corrected · Closed: Waived · Closed: No Fine · Closed: Dismissed · Closed

## Flags and badges (labels, not statuses)
Point in time (purple) · Safety issue · Board declared emergency · Re-inspect · Mail held until [date] · Mailing cancelled: corrected first · Direct referral / Normal path (legal) · Unread message (envelope)

## Questions for you
1. "Referred to attorney" is still treated as ended by the code (owners can't message on it). It probably should be open.
2. Do the six step names and the owner sentences read right? (They are the Association's own step names.)
3. Owners see a fine that was imposed as **Closed** in their list (as you asked), but the tile still says "Fine due [date]" until it is paid.
4. Statuses "Awaiting board vote" and "Board approved — schedule hearing" both sit under "Fine issued by Board". Fine, or split them into two steps?
