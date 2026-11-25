"""Property tests (second stabilisation pass): the small functions that clean or read what people type are given thousands of generated strings (accents, emoji, control
characters, right-to-left text, very long text) and must always return something safe, never raise anything but their own plain-message error. Needs `hypothesis`
(listed in requirements-dev.txt); skipped where it is not installed."""
import re
import unicodedata
from decimal import Decimal

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

from violations import casefile, mailing, services, vantaca, views  # noqa: E402
from violations.models import split_emails  # noqa: E402

pytestmark = pytest.mark.django_db
EVERYTHING = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=300)       # any character except lone surrogates (which cannot be encoded)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FAST = settings(max_examples=300, deadline=None)


@FAST
@given(EVERYTHING)
def test_tracking_numbers_are_clean_or_refused_with_a_plain_message(raw):
    try:
        t = services.clean_tracking_number(raw)
    except services.WorkflowError:
        return
    assert t == "" or re.fullmatch(r"[A-Z0-9]{8,40}", t)


@FAST
@given(EVERYTHING)
def test_one_line_text_has_no_control_characters_is_short_and_stable(text):
    out = services.one_line(text)
    assert not CONTROL.search(out) and " " not in out and len(out) <= 200 and out == out.strip() and "  " not in out
    assert services.one_line(out) == out


@FAST
@given(EVERYTHING)
def test_a_name_for_the_print_service_is_at_most_40_characters_on_one_line(name):
    out = mailing._name_for_lob(name)
    assert len(out) <= 40 and "\n" not in out and out == out.strip()


@FAST
@given(EVERYTHING)
def test_text_going_into_a_print_service_field_has_no_control_characters_or_double_quotes(text):
    out = mailing._clean(text)
    assert not CONTROL.search(out) and '"' not in out and out == out.strip()


@FAST
@given(st.dictionaries(EVERYTHING, EVERYTHING, max_size=6), EVERYTHING)
def test_the_print_service_request_never_lets_a_value_add_or_end_a_form_part(fields, filename):
    body, ctype = mailing._multipart(fields, "file", "notice.pdf", b"%PDF-1")
    boundary = ctype.split("boundary=")[1]
    assert body.count(f"--{boundary}".encode()) == len({mailing._clean(k) for k in fields}) + 2       # one part per field, the file, and the closing line
    assert body.endswith(f"--{boundary}--\r\n".encode())


@FAST
@given(st.one_of(EVERYTHING, st.integers(), st.none()))
def test_a_spreadsheet_cell_that_could_run_as_a_formula_is_always_defused(value):
    out = casefile._safe_cell(value)
    if isinstance(value, str) and value:
        assert out[:1] == "'" or (value.lstrip(" ")[:1] not in ("=", "+", "-", "@") and value[0] not in "\t\r\n")
    else:
        assert out == value


@FAST
@given(EVERYTHING)
def test_a_typed_amount_is_money_or_a_plain_message(text):
    try:
        d = services._amount(text)
    except services.WorkflowError:
        return
    assert d.is_finite() and abs(d) < 1_000_000 and d == d.quantize(Decimal("0.01"))


@FAST
@given(st.one_of(EVERYTHING, st.integers(), st.floats(allow_nan=True), st.none()))
def test_a_typed_whole_number_is_never_an_error(value):
    assert isinstance(views._int(value, 7), int)


@FAST
@given(EVERYTHING)
def test_a_typed_date_is_a_real_date_or_nothing(text):
    d = views._date(text)
    assert d is None or (d.year >= 1 and d.isoformat() == d.isoformat())


@FAST
@given(EVERYTHING)
def test_an_after_the_form_address_is_only_ever_this_site(rf_text):
    from django.test import RequestFactory
    request = RequestFactory().post("/x/", {"next": rf_text}, HTTP_HOST="testserver")
    out = views._safe_next(request)
    assert out == "" or (out.startswith("/") and not out.startswith("//") and "\\" not in out and not CONTROL.search(out))


@FAST
@given(EVERYTHING)
def test_email_lists_split_without_error_and_never_hold_a_separator(text):
    for e in split_emails(text):
        assert e and not re.search(r"[;,\s]", e)


@FAST
@given(EVERYTHING)
def test_the_owner_list_readers_never_raise_on_text(text):
    assert vantaca.parse_us_address(text) is None or set(vantaca.parse_us_address(text)) == {"line1", "city", "state", "zip"}
    date_value, ok = vantaca.parse_date(text)
    assert isinstance(ok, bool) and (date_value is None or ok)
    assert vantaca._one_line(text) == " ".join(text.split())
    c = vantaca._coordinate(text, -90, 90)
    assert c is None or -90 <= c <= 90


@FAST
@given(st.dictionaries(st.sampled_from(["Account ID", "Homeowner Name", "Property Address", "Email Address", "Mailing Address", "Tenant", "Phone", "Latitude", "Longitude", "ZIP", "City", "Lot"]),
                       EVERYTHING, max_size=12))
def test_any_row_of_text_either_normalises_or_is_skipped_never_a_crash(row):
    d = vantaca.normalize(row)
    for f in ("name", "property_address"):
        assert not CONTROL.search(d.get(f) or "") and "\n" not in (d.get(f) or "")
