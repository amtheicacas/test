# Running costs of the My Heron Landing Application (estimate)

Prices were looked up on 2026-11-20 from the vendors' pages and from third-party pages; **confirm each on the vendor's own site
before signing up.** Items marked (?) could not be confirmed. US dollars, per month unless stated. Nothing here has been set up.

| Tool | What it does for us | Cost |
|---|---|---|
| **Render** web service (Starter) | Runs the website, always on | about $7 |
| **Render** PostgreSQL | The database (all cases, owners, letters' records) | about $6 for the smallest; about $19-20 for 1 GB (sources differ). Budget $20 |
| **Render** cron job (?) | Runs the daily reminders / certified-mail tracking | small; confirm |
| **Postmark** | Sends the emails (sign-in links, notices, reminders) | $15 for up to 10,000 emails, extra about $1.80 per 1,000 |
| **Storage** (Backblaze B2 for the live files) | Private place for photos, saved letters, minutes, site-visit photos | $6.95 per TB; the first 10 GB are free. A few GB is about $0 |
| **Storage copy** (Cloudflare R2) | Second copy of files and encrypted database backups | $0.015 per GB after the first 10 GB free; downloads free. About $0 |
| **Domain** (Cloudflare registrar) | The web and email address | about $10-15 per YEAR (?) |
| **Cloudflare DNS + Email Routing** | Domain records Postmark needs; receives replies | free |
| **Backup heartbeat monitor** | Emails us if a nightly backup did not run | free tier usually enough (?) |
| **Lob** | Prints and mails paper letters | letters from about $0.83 each (print, envelope, postage). **Certified mail adds the USPS fee, about $5.30-$5.55 plus extra for a return receipt.** Lob's own certified price and any monthly plan fee: confirm with Lob |
| **Zoom** | Hearing meetings and links | $0 extra if the Association already has a paid host account; otherwise the price of one (?) |
| **Vantaca** | Owner list (system of record) | already paid; ask whether the data feed costs extra (?) |
| **GitHub** | Holds the code | free |
| **Anthropic (optional "Ask AI" button)** | Off unless a key is added | pay per use; leave off |

## Rough totals
- **Fixed monthly:** Render about $13-27 + Postmark $15 + storage about $0-1 = **about $28-43 a month**, plus the domain (about $1 a month averaged).
- **Variable:** paper mail. One certified hearing notice is roughly $6-9 all in; ordinary letters under $1 each. This depends on the number of cases and
  is the cost most likely to surprise us.

## How to pay without a personal card
1. **Put every account in the Association's name, never in one person's name** (company name "Heron Landing ...", a role email such as
   a shared board address, billing contact = the Association). If one person leaves the Board, nobody is locked out. Add a second
   officer as an owner/admin on each account. Record the logins on the recovery sheet (`BACKUP_AND_RECOVERY.md` section 8).
2. **Ask the Association's bank (or treasurer) for an Association debit card or a virtual card on the operating account**,
   used only for these subscriptions. Most banks that serve HOAs can issue one in days; a virtual card can be locked or capped
   at a monthly limit. This is the cleanest answer: it is automatic, charges go straight to the Association's books, and no one is reimbursed.
3. **Or ask the management company whether it can pay these as vendor invoices.** Small plans are card-only, so this works best for
   larger or invoiced services (Lob prepaid credit, a Zoom invoice) and is slower for monthly subscriptions. Ask.
4. **Prepay where possible:** Lob lets you load credit (confirm), and domains can be paid for several years at once.
5. **Turn on spending alerts** (every vendor has them) and a monthly spending cap on the card, so a mistake or attack cannot run up a bill.
6. The Board should approve the budget (about $30-45 a month plus mail) once, so the monthly charges are an approved recurring expense.
Card numbers and logins never go in chat or in this repository.

Open question for the Board: which of options 2 and 3 the Association will use, and who the billing contact is.
