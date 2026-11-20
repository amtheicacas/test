# Testing checklist (pick up here)

Everything below runs on your own computer. Start the site with `start-site.bat`, sign in at http://127.0.0.1:8000. Test logins use the password `demo-password-123`.

## 0. One-time setup
0. **Once:** add the line `DJANGO_DEBUG=1` to `hoa/local.env` (copy `local.env.example` to `local.env` if you have none). `start-site.bat` already does this, but commands you type yourself (`python manage.py ...`) need it too.
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
- [ ] **Site-visit observations:** sign in as a committee member, open a meeting that has a case. Under the case, "Site-visit observations": write a note and add one or two photos (on a phone, take them with the camera), Save. It shows who, when, and a short fingerprint. Add a correction to it: the original stays. There is no edit or delete anywhere.
- [ ] As the owner of that case (owner login): nothing about site visits appears on any of your pages. As a committee member who owns the case: the section says you can't see it.
- [ ] As Board/manager: the case page shows the same section; "Download the case file (zip)" now has a `Site-Visit-Observations` folder (notes, photos, photo list with fingerprints, who looked) and a `manifest.csv`.
- [ ] **Saved letters:** send a notice (the first notice of a test case). On the case page, "Exact copies of letters sent" lists it; click "Open the exact copy": it is the same PDF that was emailed. Try the "Download the case file (zip)" button: unzip it and open `00-INDEX.html`. The case history shows "Case file downloaded".
- [ ] **Hearing notice attachment:** send a case to a hearing. The hearing notice PDF ends with a page titled "ATTACHMENT - Copy of Notice of Violation" followed by the exact first notice. The Second Notice is not attached.
- [ ] **Suspension:** no screen (Board vote, committee pages, case pages, owner pages, dashboard) mentions suspension.
- [ ] After `git pull`, double-click `start-site.bat`: it now also installs the new add-on (pypdf) and runs `migrate`.
- [ ] **Dashboard tabs:** under "Needs your attention" there is a row of status tabs (All, To do, To approve & send, Owner says fixed, Re-inspect, Messages, Imposed / unpaid, Cure periods...). Click one to see only that status; "All" shows everything. A link such as the overdue "messages" bar opens the right tab.
- [ ] **Photo limits:** on a case, add a text file as a "photo": a yellow message names it and says why; add 12 pictures at once: 10 are kept and the message says so.
- [ ] **Sign-in links:** ask for a link twice; only the newest works.
- [ ] **Roster:** Committee members page: pick members from the owner list; they get the committee role on their own owner login; copy the roster to next year.
- [ ] **Meetings:** schedule with "Create the Zoom meeting for me"; try <20 days (pop-up); repeat monthly; assign ticked waiting cases to a picked meeting; open a meeting to reschedule / cancel.
- [ ] **Committee page:** tick attendance; own-violation abstention; decisions; minutes (draft), approve at the next meeting.
- [ ] **One login:** an owner who is on the committee lands on their own home and uses the Committee button.
- [ ] **Owner home:** hearing banner and its page (only their own items); closed-violations toggle.
- [ ] **Board queue** bulk table; **Former owners** page; **Standard Responses**; dashboard charts.
- [ ] **Zoom:** after adding credentials to `hoa/local.env`, the hearing notice PDF shows that meeting's link.

## Uploaded minutes (Phase 3, SEC-15) and retention
- [ ] Committee meeting page: upload a real PDF and a real Word `.docx`; each shows as "Current file" with who, when and a fingerprint.
- [ ] Try a `.txt`, a picture, an old `.doc`, and a PDF renamed from a text file: each is refused with a plain message and nothing is saved.
- [ ] Upload a second file: it becomes current, and the first appears under "Earlier uploads" and still downloads.
- [ ] The file downloads (it does not open as a web page); a homeowner login cannot open the address.
- [ ] Former owners page says records are kept 7 years.

## File backup (copy only)
- [ ] `backup-files.bat --dry-run` lists what it would copy and writes nothing.
- [ ] `backup-files.bat` copies; a second run says everything is already in the backup.
- [ ] In the Cloudflare R2 bucket the same folders (`photos/`, `minutes/`, ...) appear; delete nothing there by hand.

## Database backup
- [ ] `python manage.py make_backup_key` prints a public key and writes `backup-private-key.txt` without showing it; running it again refuses to overwrite.
- [ ] `python manage.py backup_database --dry-run` says it dumped and encrypted and stored nothing.
- [ ] After the real run a file under `database/` appears in the R2 bucket; `decrypt_backup` turns it back into a database file.
