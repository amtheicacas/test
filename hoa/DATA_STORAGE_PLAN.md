# Data Storage Plan — photographs, minutes and other uploads

Audit date: 2026-11-20. Part of the security review (`SECURITY_REVIEW.md`). **Design only: no code was changed.**
Prices and vendor features below are from general knowledge and must be re-checked on each vendor's page before you
sign up.

---

## 1. Where files are stored and read today

| # | What | Stored by | Read by | Notes |
|---|---|---|---|---|
| 1 | Violation photos (staff) | `_save_photos` (`views.py:103`) from `violation_create` (`:358`) and `add_photos` (`:500`) → `Photo.image` (`upload_to=photo_path`) | `photo_file` (`views.py:1115`) for staff / committee / owner who owns the case | 10 MB each, `PIL.verify()`, EXIF date → `taken_at`; random name but **extension taken from the uploaded filename** (SEC-01) |
| 2 | Owner cure photos | `_save_photos(..., from_resident=True)` from `my_violation` and the emailed `/r/<token>/` page (`:1132`) | `photo_file` (logged-in), `resident_photo` (`:1280`, token) | Unauthenticated uploads possible through the token page |
| 3 | Photos embedded in PDF letters | n/a | `pdf.py:96` — `Image(ph.image.path)` | **Breaks on cloud storage** (`.path` does not exist) |
| 4 | Photos attached to the attorney email | n/a | `services.legal_attachment_bytes` (`:297`) via `.open("rb")` | Works on cloud storage |
| 5 | Meeting minutes upload | `committee_meeting_save` (`views.py:1009`) → `CommitteeMeeting.minutes` (`upload_to="minutes/"`) | `meeting_minutes` (`:1057`) | `.pdf .doc .docx .txt .png .jpg .jpeg`, 10 MB, extension check only; original filename kept (SEC-15) |
| 6 | Owner import file (Excel/CSV) | `import_owners` (`views.py:1383`) | Read in memory | Not stored as a file (verify when touching it) |
| 7 | Letters, committee-minutes PDFs, notices | Generated on demand (`pdf.py`, `minutes.py`) | Streamed to the browser | **Not stored** — nothing to back up; they can always be regenerated from the database |
| 8 | Email copies | `EmailLog` rows | Staff | In the database, not files |

There is **no** public media route (`config/urls.py` has none), so files can only be fetched through a view that checks
who is asking. Keep it that way.

Delete paths: only `services.delete_photo`, only while the violation is a draft, logged. Everything else keeps its
files even when the database row is removed.

---

## 2. Requirements → how each is met

| Requirement | Plan |
|---|---|
| Not publicly accessible | Private bucket, no public ACLs, no public bucket policy; the app serves every file itself through its existing permission-checked views (streamed), so **no file URL is ever shared** |
| Only authorized users | Unchanged: `can_view()` / token checks in the views (verified by the role × URL sweep) |
| No predictable or shared URLs | Random 128-bit object names; no pre-signed URLs handed to browsers (optional later, 60-second expiry if ever needed) |
| Validate type and size | See section 5 |
| Prevent malicious files | Only image types and 10 MB documents accepted; stored extension from the *detected* type; served with a fixed content type + `nosniff` + sandboxing CSP (SEC-01); no executable or HTML types ever accepted; no antivirus service (not worth the cost/complexity for image and PDF uploads from known users) |
| Safe filenames | The user's filename is never part of the storage key; keep it, if wanted, as display text in the database only |
| Protect evidence from change/deletion | App key that **cannot delete**, bucket versioning, independent second copy (section 4) |
| Preserve photo metadata | Original bytes are stored untouched (EXIF intact); `taken_at` copied to the database at upload. Never re-encode the stored original |
| Survive redeployments | Files live in the bucket, not on Render's disk; the database stores only the object name |
| Backups | Section 6 and `BACKUP_AND_RECOVERY.md` |

---

## 3. Recommended storage (affordable, S3-compatible)

Expected volume (assumption — please correct me): a few hundred cases a year, about three photos each at 2–4 MB, plus a
handful of PDFs → roughly **2–5 GB per year**. At that size every option below costs a few dollars a month at most, and
the first ~10 GB is free at both of the cheap vendors.

| | Backblaze B2 | Cloudflare R2 | AWS S3 |
|---|---|---|---|
| Free allowance (approx.) | 10 GB storage | 10 GB storage | small, time-limited |
| Storage price (approx.) | ~$6 per TB-month | ~$15 per TB-month | ~$23 per TB-month |
| Egress (download) | Free up to a multiple of stored data | Free | Charged |
| Versioning (keeps every upload) | **Yes** | Not available when I last checked | Yes |
| Key that can write but **not delete** | **Yes** (separate "delete" capability on an application key) | No (read-write includes delete) | Yes (IAM policy) |
| Lock objects against deletion | Yes (Object Lock) | No | Yes (Object Lock) |
| Works with `django-storages` (S3 backend) | Yes | Yes | Yes |
| Ease for a non-technical admin | Simple web console | Simple web console | Powerful but more complicated |

**Recommendation: Backblaze B2 for the live bucket, with the independent backup copy on a different vendor
(Cloudflare R2 free tier, or a second B2 account).**
Reason: B2 natively gives the two protections you asked for — *the app can add files but cannot delete them*, and *old
versions are kept* — which is what protects violation evidence. R2 is the cheapest to operate and is a good second
vendor for backups. AWS S3 is equally capable if the Board prefers a brand-name vendor; it just costs more and is
fiddlier.

Do **not** use Render's disk for uploads: it is wiped on redeploy and ties you to a single server.

---

## 4. Bucket design

- **Two buckets, one vendor each:**
  1. `hoa-uploads` (B2, private): live photos and minutes.
  2. `hoa-backups` (R2 or second B2 account, private): a copy of the uploads and the encrypted database dumps.
- **Keys / credentials (never in git, stored as Render secrets):**
  - App key — B2 capabilities: list, read, **write only** (no delete), limited to `hoa-uploads`.
  - Backup key — read on `hoa-uploads`, write on `hoa-backups`; used only by the backup job.
  - Admin key — kept offline by the manager and one Board officer, used only for restores and clean-ups.
- **Object names:** keep the existing scheme (`photos/<32 hex>.<ext>`, `minutes/<32 hex>.<ext>`); old database rows keep
  working unchanged because the database stores the name, not a URL.
- **Bucket settings:** no public access; versioning on (B2 does this by default); encryption at rest (provider default);
  lifecycle — **keep all versions** (cost is tiny; these are legal records). Do not set expiry rules.
- **Delete behaviour:** `delete_photo` (draft only) tries to delete the stored file. With a no-delete key this will be
  refused; the change should treat that as "leave the file, remove the database row" and log it. Orphan files in the
  bucket are acceptable and kept for evidence.

### Sketch of the application change (for approval — not applied)

- Add `django-storages[s3]` (and `boto3`) to `requirements.txt`.
- In `settings.py`: when `STORAGE_BUCKET` is set, configure `STORAGES["default"]` with the S3 backend, the vendor
  endpoint URL, `default_acl=None`, `querystring_auth=True`, `file_overwrite=False`, `signature_version="s3v4"`; when not
  set (local development), keep today's `MEDIA_ROOT` behaviour so nothing changes on the user's PC and in tests.
- Environment variables: `STORAGE_BUCKET`, `STORAGE_ENDPOINT_URL`, `STORAGE_REGION`, `STORAGE_ACCESS_KEY_ID`,
  `STORAGE_SECRET_ACCESS_KEY`.
- `pdf.py:96`: read the bytes with `ph.image.open("rb")` instead of `.path` (the same method
  `legal_attachment_bytes` already uses).
- Views keep using `FileResponse(p.image.open("rb"))`, which streams from cloud storage; add the explicit image
  content-type and sandbox header (SEC-01).
- Tests keep using a temporary local folder (`settings.MEDIA_ROOT`) exactly as today.

---

## 5. Upload validation rules (target)

| Rule | Photos | Minutes / documents |
|---|---|---|
| Allowed types | JPEG, PNG, GIF, WebP, (HEIC only if Pillow support is added) | PDF, DOC, DOCX, TXT, PNG, JPEG |
| Type decided by | Pillow's detected format (not the filename, not the browser's type) | File signature: PDF header `%PDF`, DOCX/zip structure, image via Pillow, TXT must decode as text |
| Stored extension | From the detected type | From the verified type |
| Maximum size | 10 MB each (today) | 10 MB (today) |
| Per-request cap | **New:** at most 10 files and 40 MB total | 1 file |
| Pixel limit | Pillow's decompression-bomb guard stays on (rejects absurd dimensions) | n/a |
| Bad file feedback | **New:** tell the user which file was rejected and why (today it is skipped silently) | already shown |
| Filename | Random key; original name kept only as text if needed | Same |
| Who may upload | Staff on any case; owner on own case; token-holder during the cure period (with throttle, SEC-12) | Staff and committee (as today) |
| Serving | Fixed image content-type, `inline`, `nosniff`, `Content-Security-Policy: default-src 'none'; sandbox` | Fixed type from the verified format; Word files download |
| Record who/when | **New (additive):** `uploaded_by` and `uploaded_at`, optional `sha256` of the original bytes for tamper evidence | Audit entry on upload **and replacement** |

---

## 6. Backup of files (summary — full runbook in `BACKUP_AND_RECOVERY.md`)

- **Layer 1 (live protection):** B2 versioning + no-delete key. An accidental or malicious overwrite or delete keeps the
  earlier version.
- **Layer 2 (independent copy, nightly):** a Render cron job runs `rclone copy` from `hoa-uploads` to `hoa-backups`
  (using `copy`, not `sync`, so a deletion never propagates).
- **Layer 3 (offline snapshot, quarterly):** the manager downloads the whole bucket to an encrypted external drive kept
  by a Board officer.
- **Restore test:** quarterly, pick five random photos from the database and confirm each opens from the backup bucket.

---

## 7. Moving existing local files (only if you keep today's data)

If production starts fresh (recommended), there is nothing to move. Otherwise:

1. Copy `hoa/media/` to the bucket with `rclone copy` preserving paths (`photos/…`, `minutes/…`).
2. Load the database; names in the rows match the copied paths, so no database change is needed.
3. Open five photos and one minutes file as different roles to confirm.

---

## 8. Redeploy safety check

Before go-live and after any storage change: upload a photo → redeploy the service → open the photo as a Board member
and as the owner. Then confirm the object exists in the bucket and that the app key cannot delete it
(`rclone delete` with the app key must fail).

---

## 9. Issues specific to storage

| ID | Issue | Priority | Cx |
|---|---|---|---|
| SEC-01 | Extension taken from the filename → HTML served from the app's origin | High | S |
| SEC-06 | Local disk is wiped on deploy; `pdf.py` uses `.path` | High | M |
| SEC-12 | Token page uploads have no count/size/rate limit; rejects are silent | Medium | S |
| SEC-15 | Minutes: extension-only check, original name kept, replacement not recorded | Medium | S |
| — | Draft-photo deletion needs a "file delete refused" path once the key cannot delete | Medium | S |
| — | Optional: `uploaded_by`, `sha256`, `original_filename` on `Photo` (additive migration) | Low | S |

## 10. Decisions needed

1. Storage vendor (recommended: B2 live + R2 copy).
2. Whether production starts empty or carries current test files.
3. How long photos and minutes must be kept (ask the association's attorney; the plan assumes "indefinitely" because
   the cost is negligible).


---

## Addendum (Part A): saved copies of every letter sent
- New private files: `sent-letters/<year>/<random>.pdf` in the same storage as photos and minutes (MEDIA_ROOT locally; the private bucket when `STORAGE_BUCKET` is set). The database row (`SentLetter`) holds who/when/channel/tracking and a SHA-256 fingerprint; the application re-checks the fingerprint when a copy is opened and in the case-file index.
- These are legal records: they must be in the file backups, bucket versioning on, and the app key must not be able to delete (see section 4). A restore test must open at least one saved letter and confirm its fingerprint.
- The app never deletes a saved letter (no page does, the admin is read-only). Deleting a violation through the admin (or `clear_test_data`, `purge_archived_owners`) still removes the database rows, not the files in the bucket.
- The vendor is still undecided: the settings are vendor-neutral (S3 API). The recommended pairing in section 3 stands (Backblaze B2 live + Cloudflare R2 copy).
- A live site now refuses to start without `STORAGE_BUCKET` or `MEDIA_ROOT` (a persistent disk path), so files can never silently land on a disk that is wiped at every update.


---

## Addendum: site-visit photographs
New private files under `site-visits/<year>/<random>.<ext>` (same storage as photos, minutes and `sent-letters/`). They are the ORIGINAL uploaded bytes (never re-encoded), with a SHA-256 fingerprint in the database; the app re-checks it whenever one is opened and in the case-file manifest. Internal evidence: include `site-visits/` in the nightly file copy and bucket versioning; never public, never served to owners.


## Update: SEC-15 (minutes uploads) built
Add-only `MinutesFile` rows (migration 0042); files under `minutes/<year>/<random>.<pdf|docx>`; content checks, history and permission-checked download as described in `CLAUDE.md` ("Uploaded minutes"). The `minutes/` prefix is part of the file backups. Still to prove against a real private bucket: upload, redeploy, download, fingerprint still matches.


## Update: vendor chosen (B2 live + R2 copy) and a correction about "cannot delete"
See `PROJECT_STATUS.md` section 25. A key without delete permission can still hide an object with a delete marker on a versioned bucket (it needs `writeFiles` for that); only `deleteFiles` removes a stored version. Keep versioning on, give the live app key no `deleteFiles`, and treat the second copy at R2 as the real protection. `python manage.py check_storage` reports what a key can do.
