"""Backups of the uploaded files: copy every file from the live storage to a second bucket at another company.

Rules (they are the point of a backup): COPY ONLY. Nothing in the backup is ever overwritten or deleted by this code; a file that already
exists with a different size is reported as a conflict and left alone. Every new copy is read back and its SHA-256 fingerprint compared with
the original before it counts as copied."""
import hashlib
import io
from dataclasses import dataclass, field

EXCLUDE = ("storage-check/",)       # the storage self-test's own files are not worth keeping
PLACEHOLDERS = (".bzEmpty",)        # Backblaze's 0-byte marker for a folder made on its website: not a real file


def walk(storage, prefix=""):
    """Every stored file name under `prefix` (ending in "/" or empty), found through the storage API: works for a local folder and a bucket."""
    dirs, files = storage.listdir(prefix)
    for f in files:
        yield prefix + f
    for d in dirs:
        yield from walk(storage, prefix + d + "/")


def referenced_files():
    """Every file name the database points at (letters, photos, minutes, site-visit photos), from every file field of every model."""
    from django.apps import apps
    from django.db import models as m
    names = set()
    for model in apps.get_models():
        for fld in model._meta.get_fields():
            if isinstance(fld, m.FileField) and not getattr(fld, "many_to_many", False):
                names.update(n for n in model.objects.exclude(**{fld.name: ""}).values_list(fld.name, flat=True) if n)
    return names


class S3Target:
    """The backup bucket, reached with its own key (boto3 directly: the website never uses this key)."""

    def __init__(self, bucket, endpoint_url, region, access_key, secret_key):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region or None, aws_access_key_id=access_key,
                                   aws_secret_access_key=secret_key, config=Config(signature_version="s3v4", retries={"max_attempts": 5}))

    def existing(self):
        out, token = {}, None
        while True:
            kw = {"Bucket": self.bucket, **({"ContinuationToken": token} if token else {})}
            r = self.client.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                out[o["Key"]] = o["Size"]
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]

    def exists(self, key):
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def put(self, key, data, sha):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, Metadata={"sha256": sha})

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()


@dataclass
class Report:
    copied: list = field(default_factory=list)
    present: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    failures: list = field(default_factory=list)       # (name, why)
    bytes_copied: int = 0
    would_copy: list = field(default_factory=list)
    missing_from_backup: list = field(default_factory=list)

    @property
    def ok(self):
        return not (self.conflicts or self.failures or self.missing_from_backup)


def copy_files(storage, target, dry_run=False, verify=False, exclude=EXCLUDE):
    rep = Report()
    have = target.existing()
    for name in sorted(walk(storage)):
        if name.startswith(exclude) or name.rsplit("/", 1)[-1] in PLACEHOLDERS:
            rep.skipped.append(name)
            continue
        try:
            with storage.open(name, "rb") as f:
                data = f.read()
            sha = hashlib.sha256(data).hexdigest()
            if name in have:
                if have[name] != len(data):
                    rep.conflicts.append(name)          # never overwrite: report and leave the backup copy alone
                elif verify and hashlib.sha256(target.get(name)).hexdigest() != sha:
                    rep.conflicts.append(name)
                else:
                    rep.present.append(name)
                continue
            if dry_run:
                rep.would_copy.append(name)
                continue
            target.put(name, data, sha)
            if hashlib.sha256(target.get(name)).hexdigest() != sha:
                rep.failures.append((name, "the copy read back differently from the original"))
                continue
            rep.copied.append(name)
            rep.bytes_copied += len(data)
            have[name] = len(data)
        except Exception as e:      # noqa: BLE001  (one bad file must not stop the rest)
            rep.failures.append((name, f"{type(e).__name__}: {e}"))
    return rep


def check_database(rep, target):
    """Every file the database refers to must now exist in the backup. Fills rep.missing_from_backup."""
    have = set(target.existing())
    rep.missing_from_backup = sorted(n for n in referenced_files() if n not in have)
    return rep
