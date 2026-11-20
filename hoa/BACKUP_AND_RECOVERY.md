# Backup and Recovery Plan

Audit date: 2026-11-20. Part of the security review (`SECURITY_REVIEW.md`). **Design and runbook only: nothing here
has been set up, and no application code was changed.** Render and vendor features are from general knowledge:
confirm current backup retention, point-in-time-recovery and notification options on Render's pages before relying on
them.

---

## 1. What has to be protected

| Asset | Where it lives in production | Can it be recreated? | Needs backup? |
|---|---|---|---|
| Database (cases, owners, notices, history, hearings, minutes text, logins, 2FA devices, email copies, mail tracking) | Render PostgreSQL | No | **Yes — the most important asset** |
| Photos and uploaded minutes | Private bucket (`DATA_STORAGE_PLAN.md`) | No | **Yes** |
| PDF letters / generated minutes | Generated on demand | Yes, from the database | No |
| Source code | GitHub | Yes | GitHub is the copy; optionally a periodic clone |
| Configuration and secrets | Render environment variables | Only if written down | **Yes — in a sealed recovery sheet, not in git** |
| Vantaca owner data | Vantaca (system of record) | Yes, re-import | No (but keep the last import file) |

---

## 2. Recovery targets (recommended for this application's size)

| Target | Value | Why |
|---|---|---|
| Recovery point (RPO), database | **24 hours** (less if the Render plan includes point-in-time recovery) | A few dozen edits a day; anything lost can be re-keyed from emails and Vantaca |
| Recovery point (RPO), files | **Near zero** | Versioned bucket; a second copy nightly |
| Recovery time (RTO), "site is down, data intact" | **4 hours** | Redeploy from GitHub |
| Recovery time (RTO), "database lost" | **1 business day** | Restore the newest dump into a new database |
| Recovery time (RTO), "Render account unreachable" | **2–3 business days** | Rebuild from GitHub + backup bucket |
| Retention | Daily ×14, weekly ×8, monthly ×12, yearly ×7 | Confirm the legal retention period with the association's attorney |

---

## 3. Database backups

Two independent layers, because a provider's own backups share its failures.

**Layer 1 — Render's built-in backups.** Use a paid database plan (free databases expire and are not backed up). Check
the current retention period and whether point-in-time recovery is included at your plan. Treat this as the fast
"undo the last bad hour" option.

**Layer 2 — Nightly encrypted off-site dump (the one that protects you from Render itself).**
A Render cron job, scheduled at night, runs a short script that:

1. dumps the database: `pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL" > hoa-YYYY-MM-DD.dump`
2. encrypts it with **`age` using a public key** held on the server (the server can write backups but cannot read them
   back; a stolen server credential does not expose history): `age -r "$BACKUP_PUBLIC_KEY" -o hoa-YYYY-MM-DD.dump.age …`
3. uploads it to the `hoa-backups` bucket on a **different vendor** from the live files (key with write-only access to
   that bucket);
4. deletes the local copy;
5. pings a monitoring address only if every step succeeded.

Note: the cron job needs a `pg_dump` whose version is the same as or newer than the server's. If the standard Python
runtime does not provide that, use a small Docker-based cron job (or run the script from a Board laptop weekly as a
fallback). This must be confirmed on Render during staging.

**Retention** is applied by the bucket's lifecycle rules (daily ×14, weekly ×8, monthly ×12, yearly ×7), or by a
tiny pruning step in the same job. Daily files are small (the sandbox dump of the full empty schema is about 230 KB;
real data will be a few MB).

**Encryption key custody.** Generate the `age` key pair once. Public key → Render secret `BACKUP_PUBLIC_KEY`. Private
key → printed or saved in two places: the manager's password manager and a sealed envelope with a second Board officer.
Without the private key the backups cannot be read — test this on day one (section 7).

---

## 4. File backups

See `DATA_STORAGE_PLAN.md` section 6. In short: B2 versioning plus an app key that cannot delete; nightly
`rclone copy` (never `sync`) to the second vendor; a quarterly offline snapshot on an encrypted drive.

---

## 5. Monitoring backup failures

| Check | How | Who is told |
|---|---|---|
| Nightly dump failed | The script exits non-zero → Render's cron-job failure notification (enable in Render notifications) | Manager + one Board officer by email |
| Nightly dump did **not run at all** | "Heartbeat" service (e.g. a free healthchecks-style monitor): the job pings it on success; no ping within 26 hours → email | Same |
| Backup exists but is unreadable | Monthly automated test: download the newest file, decrypt with a **test-only** key pair kept on a different secret, run `pg_restore --list` | Same |
| File copy failed | `rclone copy` exit code, same heartbeat | Same |
| Bucket almost full / cost spike | Vendor billing alert at a small monthly amount | Manager |

---

## 6. Restore procedures

Keep these steps in the sealed recovery sheet as well as here. Always restore into a **new, empty** database first and
look at it before pointing the application at it; never restore over the live database.

### R1 — "Someone deleted or changed the wrong thing" (most likely)
1. Do **not** touch the live database. Create a scratch Render PostgreSQL (or use a local PostgreSQL).
2. Restore the most recent dump from before the mistake (R2 steps 2–4) into the scratch database.
3. Look up the rows in the scratch copy (admin or SQL) and re-enter or copy back only what is needed.
4. Delete the scratch database when finished (it contains personal information).

### R2 — Full database restore
1. Download the newest `.dump.age` from `hoa-backups`.
2. Decrypt: `age -d -i hoa-backup.key hoa-YYYY-MM-DD.dump.age > hoa.dump`
3. Create a new empty database and restore:
   `pg_restore --no-owner --no-acl -d "$NEW_DATABASE_URL" hoa.dump`
4. Check: `python manage.py migrate --check` (must report nothing to apply; run `migrate` if the dump predates a deploy)
   and compare row counts for owners, violations, notices and events with the last known figures.
5. Put the new `DATABASE_URL` into the web service's environment and redeploy.
6. Sign in as a Board member (2FA), open a recent case and a hearing, open a photo, and confirm the daily job runs
   (`python manage.py daily_jobs`).
7. Record what happened and the data window lost in the Board's records.

### R3 — Lost or damaged files
1. Check the live bucket's earlier versions first (B2 console → file → versions).
2. If the bucket is gone: `rclone copy hoa-backups:uploads hoa-uploads:` using the admin key, then re-run the "open five
   photos" check.
3. Database rows are unaffected because they hold names, not copies.

### R4 — Render account or region unavailable (rebuild)
1. New Render account/region; create PostgreSQL and the web service from GitHub (`render.yaml` once added, or by hand
   from `PRODUCTION_SECURITY_CHECKLIST.md`).
2. Restore the database (R2), point the service to the existing bucket, set all secrets from the recovery sheet.
3. Update DNS; re-check HTTPS; sign-in links use `SITE_URL`, so set it before testing.

### R5 — A migration or deploy went wrong
1. Roll the web service back to the previous deploy on Render (code only).
2. If the migration changed data, restore the pre-deploy dump (see rule below) into a scratch database and compare.
3. **Rule:** run the nightly dump manually immediately before any deploy that includes a migration.

---

## 7. Restore testing

| When | What | Pass condition |
|---|---|---|
| Before launch (day one) | Make a dump, encrypt, decrypt with the **stored private key**, restore into a scratch database, `migrate --check`, count rows, open the app against it | All steps succeed; the private key was actually retrieved from where it is stored |
| Monthly (automated) | Newest backup decrypts with the test key; `pg_restore --list` succeeds | Heartbeat stays green |
| Quarterly (30 minutes) | R1 or R2 into scratch; open 5 random photos from the file backup | Counts match; photos open |
| Yearly | Full rebuild drill (R4) on a spare Render workspace | Site works from backups alone; actual elapsed time recorded against the RTO |

**Rehearsed in the sandbox for this audit (PostgreSQL 16):** `pg_dump -Fc` of a database built by all 37 migrations →
encrypt/decrypt round-trip with both `age` (public-key) and `gpg` (both returned a byte-identical file) → `pg_restore` into a new database
→ `manage.py migrate --check` clean → a test row survived. This validates the commands and format, not Render's
environment, and the dataset was tiny.

---

## 8. Things to write on the recovery sheet (never in git)

Render login and the people who may use it; GitHub owner; domain registrar and DNS login; `SECRET_KEY` (rotating it
only signs everyone out); `DATABASE_URL` (Render shows it); storage and backup vendor logins; the three storage keys;
the backup private key; email-provider credentials; Zoom and Lob keys; the list of who is Board, manager and committee;
where the last Vantaca owner export is kept.

---

## 9. Issues specific to backup and recovery

| ID | Issue | Priority | Cx |
|---|---|---|---|
| SEC-07 | No backup or restore plan existed | High | M |
| — | Cron job `pg_dump` version vs. server version to be confirmed on Render | High (verify in staging) | S |
| — | No scheduled cleanup of old sessions/links (`clearsessions`, expired `LoginLink`) — keeps the dump small and removes stale tokens | Medium | S |
| — | `daily_jobs` must be a Render cron job; if it silently stops, reminders and certified-mail tracking stop. Add a heartbeat ping to it too | High | S |
| — | Management commands that delete data (`clear_test_data`, `purge_archived_owners`) need a production guard so a backup is not the only protection | Medium | S |

## 10. Decisions needed

1. Who holds the backup private key (recommended: manager + one Board officer) and who may approve a restore.
2. Retention periods (attorney).
3. Whether Render's plan with point-in-time recovery is worth the extra cost over the nightly dump alone.
4. Second vendor for off-site copies (recommended: Cloudflare R2 free tier).


---

## Addendum (Part A): what the file backups must now include
`sent-letters/` (the exact PDF of every letter sent) is added to the uploads that must be backed up (photos, minutes). Treat it as the most important file area: it is the proof of what each owner was sent. Quarterly restore test: pick one saved letter from the database, restore it from the backup copy, and check that its SHA-256 equals the fingerprint on the case page / in the case-file manifest.


---

## Addendum: `site-visits/` is part of the file backups
Add the `site-visits/` prefix (committee drive-through photographs) next to `sent-letters/`, photos and minutes. In the quarterly restore test, open one site-visit photograph from the restored copy and compare its SHA-256 with the fingerprint in the case file's `Site-Visit-Observations/photos-manifest.csv`.


## Addendum: `minutes/` is part of the file backups
Uploaded minutes (PDF/.docx) live under `minutes/`; keep that prefix in the nightly file copy next to `sent-letters/` and `site-visits/`. Older versions of replaced minutes are kept on purpose and must be backed up too.


## Addendum: the file copy is built (`backup_files`)
Replaces the `rclone copy` idea for files: `python manage.py backup_files` copies every uploaded file from the live bucket to the backup bucket, never overwrites or deletes, verifies each new copy by fingerprint, and checks that every file the database refers to is in the backup. Run it nightly as a Render cron job (after the database dump) with `BACKUP_HEARTBEAT_URL` set. Quarterly restore test is unchanged (restore one saved letter and compare its SHA-256).


## Addendum: the database backup is built (`backup_database`)
Replaces the shell script in section 3: `python manage.py backup_database` dumps (pg_dump custom format on the live site), encrypts to the public key `BACKUP_PUBLIC_KEY` with `age` (pyrage), writes `database/hoa-<date-time>.dump.age` to the R2 bucket, never overwrites, verifies the upload, and pings `BACKUP_DB_HEARTBEAT_URL` only on success. Restore: download the file, `python manage.py decrypt_backup <file.age> <private key file> hoa.dump` (or `age -d -i <key> file.age > hoa.dump`), then `pg_restore --no-owner --no-acl -d <NEW empty database> hoa.dump` (section 6, R2). **Private key custody: the Board member who runs the site holds it and shares it with one other person** (record their roles here and on the sealed recovery sheet). The cron job host must have a `pg_dump` at least as new as the database server (still to confirm on Render).
