import re
import pytest
from werkzeug.security import generate_password_hash
from app import create_app


@pytest.fixture
def app(tmp_path):
    a = create_app({"DATABASE": str(tmp_path / "t.db"), "TESTING": True, "SECRET_KEY": "x"})
    with a.app_context():
        a.init_db()
        import sqlite3
        d = sqlite3.connect(a.config["DATABASE"])
        d.execute("INSERT INTO users(email,name,password_hash,role) VALUES ('a@x.org','Admin',?, 'admin')",
                  (generate_password_hash("correct-horse-1"),))
        d.commit()
    return a


def tok(c, path):
    return re.search(r'name="csrf" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)


def post(c, path, data, form_page=None):
    data = dict(data, csrf=tok(c, form_page or path))
    return c.post(path, data=data, follow_redirects=True)


def test_full_flow(app):
    pub = app.test_client()
    r = post(pub, "/apply", dict(first_name="Jo", last_name="Doe", email="Jo@Example.com", tier_id=1, statement="hi"))
    assert b"Thank you" in r.data
    assert pub.post("/apply", data={}).status_code == 400  # CSRF enforced

    st = app.test_client()
    assert st.get("/admin").status_code == 302
    post(st, "/login", dict(email="a@x.org", password="correct-horse-1"))
    assert b"Pending applications" in st.get("/admin").data
    assert b"qualified" in st.get("/admin/leads").data
    r = post(st, "/admin/applications/1", dict(decision="approve", note="ok"))
    temp = re.search(r"temporary password: ([\w-]+)", r.get_data(as_text=True)).group(1)
    assert b"Jo Doe" in r.data
    post(st, "/admin/members/1/payment", dict(amount="75", paid_on="2026-01-02", renew="1"), "/admin/members/1")
    assert b"$75.00" in st.get("/admin/members/1").data
    assert b"jo@example.com" in st.get("/admin/members/export.csv").data

    m = app.test_client()
    post(m, "/login", dict(email="jo@example.com", password=temp))
    assert m.get("/portal").status_code == 302  # forced password change
    post(m, "/password", dict(current=temp, new="a-new-long-password"))
    assert b"Welcome, Jo" in m.get("/portal").data
    assert m.get("/admin").status_code == 403
    post(m, "/portal/profile", dict(phone="555", directory_opt_in="1"))
    assert b"Jo" in m.get("/portal/directory").data


def test_bad_login_and_reject(app):
    c = app.test_client()
    assert b"Invalid" in post(c, "/login", dict(email="a@x.org", password="nope")).data
    post(c, "/apply", dict(first_name="A", last_name="B", email="ab@x.com", tier_id=1))
    post(c, "/login", dict(email="a@x.org", password="correct-horse-1"))
    post(c, "/admin/applications/1", dict(decision="reject"))
    assert b"rejected" in c.get("/admin/applications?status=all").data
