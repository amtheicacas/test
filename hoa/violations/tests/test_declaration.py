"""The Declaration (CC&Rs) restrictions added to the violation list, and the enforcement rules in Declaration section 8.1."""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command

from violations import services, standards_search
from violations.models import Owner, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(autouse=True)
def loaded():
    call_command("load_standards", verbosity=0)


def catalog():
    return json.loads((DATA / "declaration_catalog.json").read_text(encoding="utf8"))["items"]


# ---------- the catalog itself ----------
def test_both_documents_load_and_stay_separate():
    std = ViolationType.objects.filter(source="Community Standards").count()
    decl = ViolationType.objects.filter(source="Declaration").count()
    assert std >= 75 and decl >= 45
    call_command("load_standards", verbosity=0)
    assert ViolationType.objects.filter(source="Declaration").count() == decl            # idempotent
    t = ViolationType.objects.get(code="D-5.4")
    assert t.reference == "Declaration §5.4 Signs" and t.section == "5.4" and t.source == "Declaration"
    assert ViolationType.objects.get(code="3.22").source == "Community Standards"          # same section numbers never collide: codes differ


def test_every_declaration_entry_is_complete_and_free_of_known_ocr_errors():
    seen = set()
    for i in catalog():
        assert i["code"].startswith("D-") and i["code"] not in seen
        seen.add(i["code"])
        for f in ("title", "category", "observed", "cure", "rule", "keywords"):
            assert i[f].strip(), (i["code"], f)
        assert 1 <= i["cure_days"] <= 365
        assert i["rule"].startswith(("Section", "(", "If any", "Except", "The Declarant")), (i["code"], i["rule"][:30])
        for bad in ("Seetion", "sueh", "Reeeiver", "feneing", "Tor Sale", "d'A", "W ithout", "ARTICLE", "PARTY WALLS"):
            assert bad not in i["rule"], (i["code"], bad)


def test_scrambled_scan_pages_were_put_back_in_order():
    by = {i["code"]: i["rule"] for i in catalog()}
    assert by["D-5.16-short"].endswith("as a model home or for marketing purposes.")      # not the animals text
    assert "common household pets" not in by["D-5.16-short"] and "less than thirty (30) days" in by["D-5.16-short"]
    assert by["D-5.17"].startswith("Section 5.17 Animals") and "remove and properly dispose of any pet waste" in by["D-5.17"]
    assert "Section 5.15" not in by["D-5.14"] and by["D-5.36"].endswith("regardless of size.")


@pytest.mark.parametrize("code,must", [
    ("D-5.4", ["eighteen (18) inches by twenty-four (24) inches", "sixty (60) days", "twenty-four (24) months"]),
    ("D-5.16-short", ["No Lot may be leased for a period of less than thirty (30) days"]),
    ("D-5.16.2", ["complete name and mailing address of the Tenant"]),
    ("D-5.7a", ["14,500 pounds", "seven (7) days during any six (6) month period"]),
    ("D-5.33", ["two (2) days per year", "6:00 AM", "5:00 PM"]),
    ("D-5.34", ["between November 1 and January 31"]),
    ("D-5.32", ["twenty feet (20’) high"]),
    ("D-14.6.3", ["Environmental Protection Division", "grass carp"]),
    ("D-5.25e", ["PVC", "wrought iron", "aluminum rail", "beige", "black"]),
    ("D-5.27", ["eight (8) feet"]),
])
def test_key_rule_text_is_quoted_accurately(code, must):
    rule = ViolationType.objects.get(code=code).rule_text
    for m in must:
        assert m in rule, (code, m)


def test_the_rentals_and_signs_the_board_asked_about_are_there():
    names = {t.code: t for t in ViolationType.objects.filter(source="Declaration")}
    assert {"D-5.4", "D-5.16-short", "D-5.16.1", "D-5.16.2", "D-5.16-docs"} <= set(names)
    assert names["D-5.16-short"].category == "Leasing & rentals"


# ---------- search finds them ----------
@pytest.mark.parametrize("text,code", [
    ("airbnb short term rental", "D-5.16-short"),
    ("for sale sign in the yard", "D-5.4"),
    ("owner never told us who the tenant is", "D-5.16.2"),
    ("pond shoreline plants cut", "D-14.6.3"),
    ("loud speakers at night", "D-5.5"),
    ("yard sale every weekend", "D-5.33"),
    ("tree branch hangs over the sidewalk", "D-5.27"),
    ("pool is green and full of mosquitoes", "D-5.23"),
    ("sidewalk is covered in mildew", "D-9.1s"),
    ("5.4", "D-5.4"),
])
def test_search_finds_declaration_violations(text, code):
    top = [t.code for t in standards_search.local_search(text, limit=4)]
    assert code in top, (text, top)


def test_a_bare_section_number_lists_both_documents():
    assert [t.code for t in standards_search.local_search("3.22")][:3] == ["3.22", "3.22-debris", "3.22-timing"]
    assert [t.code for t in standards_search.local_search("Decl. 5.4")] == ["D-5.4"]
    assert [t.code for t in standards_search.local_search("\u00a79.1")] == ["D-9.1", "D-9.1r", "D-9.1s"]
    assert standards_search.local_search("99.99") == []


def test_labels_distinguish_the_two_documents():
    d = standards_search.serialize(ViolationType.objects.get(code="D-5.4"))
    s = standards_search.serialize(ViolationType.objects.get(code="3.48"))
    assert d["label"] == "Decl. §5.4" and s["label"] == "§3.48" and d["source"] == "Declaration"


# ---------- letters cite the Declaration ----------
def test_notice_of_violation_cites_the_declaration(settings, tmp_path):
    import io
    import shutil
    import subprocess
    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image
    from violations.models import Notice, Photo
    from violations.pdf import render_notice_pdf
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    settings.MEDIA_ROOT = tmp_path
    o = Owner.objects.create(name="Pat", property_address="1 A St", zip_code="34241")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.get(code="D-5.4"), description="A sign.", cure_required="Remove it.",
                                 cure_deadline=date.today() + timedelta(days=7), status=S.NOTICE_SENT)
    b = io.BytesIO()
    Image.new("RGB", (9, 9)).save(b, "PNG")
    Photo.objects.create(violation=v, image=SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png"))
    n = Notice.objects.create(violation=v, kind="violation")
    t = " ".join(subprocess.run(["pdftotext", "-", "-"], input=render_notice_pdf(n), capture_output=True).stdout.decode().split())
    assert "Declaration \u00a75.4 Signs" in t and "Sign without approval" in t
    assert "eighteen (18) inches by twenty-four (24) inches" in t
    assert "Signs. No sign of any kind" not in t.replace("Signs Section 5.4 Signs.", "") or "Section 5.4 Signs." not in t  # no repeated section heading


# ---------- enforcement rules from Declaration 8.1 ----------
def _pending_board():
    o = Owner.objects.create(name="Pat", property_address="1 A St")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.get(code="D-5.4"), description="d", cure_required="c", status=S.PENDING_BOARD,
                                 cure_deadline=date.today() - timedelta(days=3))
    return v


def test_a_suspension_is_limited_to_sixty_days():
    v = _pending_board()
    with pytest.raises(services.WorkflowError, match="limits a suspension for an infraction to 60 days"):
        services.board_decide(v, "proceed", date.today(), suspension="the pool", suspension_days=90, votes_for=3, votes_against=0)
    services.board_decide(v, "proceed", date.today(), suspension="the pool", suspension_days=60, votes_for=3, votes_against=0)
    v.refresh_from_db()
    assert v.proposed_suspension_days == 60


def test_suspension_limit_is_configurable(settings):
    settings.SUSPENSION_MAX_DAYS = 30
    v = _pending_board()
    with pytest.raises(services.WorkflowError):
        services.board_decide(v, "proceed", date.today(), suspension="the pool", suspension_days=31, votes_for=3, votes_against=0)


@pytest.mark.parametrize("fine,says", [(500, "does not"), (1000, "This fine qualifies")])
def test_the_approval_card_says_whether_the_fine_can_become_a_lien(client, fine, says):
    from django.contrib.auth import get_user_model
    from violations.models import Profile
    v = _pending_board()
    Violation.objects.filter(pk=v.pk).update(status=S.LEDGER_PENDING, fine_amount=fine, proposed_fine=fine)
    u = get_user_model().objects.create_user("m", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="manager")
    client.force_login(u)
    page = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Declaration §8.1(A)(i)" in page and says in page
