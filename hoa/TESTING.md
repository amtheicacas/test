# Testing checklist (pick up here)

Everything below runs on your own computer. Start the site with `start-site.bat`, sign in at http://127.0.0.1:8000. Test logins use the password `demo-password-123`.

## 0. One-time setup
1. `git pull`, then double-click `start-site.bat` (it runs `migrate`).
2. `python manage.py load_standards` (violation types, including the weeds ones).
3. Logins: `make_login board --username board1`, `make_login committee --username committee1`; `owner_login --address "7316 Great Egret"` for the owner. Use a separate browser window for each person.

## 1. Entering a violation (manager)
- [ ] **+ New**: type `weeds`, pick a result; `[location]` fills with the address; pick an owner by typing `7316`.
- [ ] A tenant shows under "This notice will go to".
- [ ] Add photos (thumbnail with an x), save with the default **Standard** timing. Email appears in the black window; the mailed copy shows "held 2 business days".
- [ ] Try **Immediate: urgent**, **Draft**, 0 cure days, and **Point in time** (goes straight to the Board queue).

## 2. Owner side
- [ ] Sign-in page: enter the owner's email, then find the link in the black window (or your inbox). Opening it shows a **Sign in** button; clicking it signs in. Reusing the link says it expired.
- [ ] **Sign-in options**: set a password, sign out, sign in with it. **Forgot your password?** sends a reset link.
- [ ] Welcome name, "What's new", card with photo and cure date, yellow "In Cure Period".
- [ ] Open the case: tracker on top, the letter shown, correspondence, message box. The extension-days box appears only after ticking "I need more time".
- [ ] Reply "it's fixed" with a photo. As manager an envelope shows on the Violations list; open the case, **Mark for re-inspection**.

## 3. Board
- [ ] **Board queue**: select rows, set decision/fine/date, fill into selected, tick "approved by majority", record. Hover a row for the photo.
- [ ] Single case: Hold with an "until" date; Refer to Board now (confirmation pop-up); Send to legal (draft email, nothing is sent until you click).

## 4. Compliance Committee
- [ ] Committee login: case under "Waiting to be scheduled". Manager: **Committee meetings** (date 14+ days out), send waiting cases.
- [ ] `python manage.py backdate_meeting`, then as committee: attendees, notes, minutes upload, one decision per case (try confirm, change amount, waive, reject), **Submit to the Board**.
- [ ] Board: **Committee results** tab: approve, modify (lower the fine or waive), return. Letter 3 appears under the case's correspondence (certified).

## 5. Before going live
- [ ] Email service set (see README) and a test email received.
- [ ] HOA address, phone, contact email, signer name set; return address is the mailbox.
- [ ] `ATTORNEY_EMAIL`, `HOA_DOCUMENTS_URL`, `SITE_URL` set; `daily_jobs` scheduled.
- [ ] `clear_test_data --yes` (or use a fresh database on the live host); delete test logins.

## Open decisions / not built yet
- Continuing-violation fines ($100/day up to $1,000): decide with the attorney whether to build day counting (README: Fining Resolution).
- Letter text: the "respond at [private link]" line on letters 1 and 3 (remove, reword, or email-only).
- Committee emails (when a case or meeting is ready), inbound email replies, Vantaca API (owners and ledger), Sarasota County Property Appraiser check, hosting.
- Verify the CC&R text (it was read from a scan) and which document wins where it conflicts with the Community Standards.

## 6. Added since the first checklist (please test)
- [ ] **Roster:** Committee members page: pick members from the owner list; they get the committee role on their own owner login; copy the roster to next year.
- [ ] **Meetings:** schedule with "Create the Zoom meeting for me"; try <20 days (pop-up); repeat monthly; assign ticked waiting cases to a picked meeting; open a meeting to reschedule / cancel.
- [ ] **Committee page:** tick attendance; own-violation abstention; decisions; minutes (draft), approve at the next meeting.
- [ ] **One login:** an owner who is on the committee lands on their own home and uses the Committee button.
- [ ] **Owner home:** hearing banner and its page (only their own items); closed-violations toggle.
- [ ] **Board queue** bulk table; **Former owners** page; **Standard Responses**; dashboard charts.
- [ ] **Zoom:** after adding credentials to `hoa/local.env`, the hearing notice PDF shows that meeting's link.
