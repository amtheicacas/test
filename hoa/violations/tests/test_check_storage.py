"""The storage self-check command."""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from io import StringIO

from violations.management.commands.check_storage import run_checks

pytestmark = pytest.mark.django_db


def test_local_storage_passes_and_leaves_no_overwrite(settings):
    out = StringIO()
    call_command("check_storage", stdout=out)
    assert "All checks passed" in out.getvalue() and "FAIL" not in out.getvalue()


def test_a_bucket_that_answers_an_unsigned_link_is_flagged():
    from django.core.files.storage import default_storage
    class S:
        def __init__(self): self.d = {}
        def save(self, n, c):
            while n in self.d: n = "x" + n
            self.d[n] = c.read(); return n
        def open(self, n, m="rb"):
            import io; return io.BytesIO(self.d[n])
        def url(self, n): return "https://bucket.example/" + n + "?sig=abc"
        def delete(self, n): self.d.pop(n)
    res = run_checks(S(), True, fetch=lambda u: 200)
    assert any(ok is False and "WITHOUT A SIGNATURE" in t for ok, t in res)
    for refusal in (400, 401, 403):                                   # B2 answers 401, R2 answers 400
        res = run_checks(S(), True, fetch=lambda u, r=refusal: 200 if "sig=" in u else r)
        assert not any(ok is False for ok, _ in res)
    res = run_checks(S(), True, fetch=lambda u: 302 if "sig=" not in u else 200)               # a redirect is not a refusal
    assert any(ok is False for ok, _ in res)


def test_a_storage_that_fails_is_reported_not_crashed(monkeypatch):
    from django.core.files import storage
    monkeypatch.setattr(storage.default_storage._wrapped.__class__, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("no disk")))
    with pytest.raises(CommandError):
        call_command("check_storage", stdout=StringIO())
