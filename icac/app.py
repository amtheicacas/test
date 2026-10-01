"""ICAC Membership Manager: leads, applications, members, payments, events and a members-only portal."""
import csv
import io
import os
import secrets
import sqlite3
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
LEAD_STAGES = ["new", "contacted", "qualified", "converted", "lost"]
MEMBER_STATUSES = ["active", "lapsed", "suspended", "former"]
MEMBER_FIELDS = ["first_name", "last_name", "email", "phone", "organization", "job_title",
                 "address", "city", "state", "zip"]


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATABASE=os.path.join(BASE, "icac.db"), TESTING=False)
    key = os.environ.get("SECRET_KEY")
    if not key:
        path = os.path.join(BASE, ".secret_key")
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(secrets.token_hex(32))
            os.chmod(path, 0o600)
        key = open(path).read().strip()
    app.config["SECRET_KEY"] = key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config.update(config or {})

    # ---------- database ----------
    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_):
        d = g.pop("db", None)
        if d:
            d.close()

    def init_db():
        with open(os.path.join(BASE, "schema.sql")) as f:
            db().executescript(f.read())
        if not db().execute("SELECT 1 FROM tiers").fetchone():
            db().executemany("INSERT INTO tiers(name, annual_dues, description) VALUES (?,?,?)", [
                ("Individual", 75, "For individual professionals"),
                ("Student", 25, "Full-time students"),
                ("Organization", 400, "Agencies and companies, up to 10 seats")])
        db().commit()

    app.init_db = init_db

    def q(sql, *args):
        return db().execute(sql, args).fetchall()

    def q1(sql, *args):
        return db().execute(sql, args).fetchone()

    def run(sql, *args):
        cur = db().execute(sql, args)
        db().commit()
        return cur.lastrowid

    def log(kind, summary, member_id=None, lead_id=None):
        actor = g.user["name"] if g.get("user") else "system"
        db().execute("INSERT INTO activity(member_id, lead_id, kind, summary, actor) VALUES (?,?,?,?,?)",
                     (member_id, lead_id, kind, summary, actor))

    # ---------- auth / csrf ----------
    @app.before_request
    def load_user():
        g.user = None
        if session.get("uid"):
            u = q1("SELECT * FROM users WHERE id=? AND active=1", session["uid"])
            if u:
                g.user = u
            else:
                session.clear()
        if request.method == "POST":
            tok = session.get("csrf")
            if not tok or not secrets.compare_digest(tok, request.form.get("csrf", "")):
                abort(400, "Invalid or missing CSRF token")

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return dict(csrf=session["csrf"], user=g.get("user"), LEAD_STAGES=LEAD_STAGES,
                    MEMBER_STATUSES=MEMBER_STATUSES, today=date.today().isoformat())

    def login_required(*roles):
        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **kw):
                if not g.user:
                    return redirect(url_for("login", next=request.path))
                if roles and g.user["role"] not in roles:
                    abort(403)
                if g.user["must_change_password"] and request.endpoint not in ("change_password", "logout"):
                    return redirect(url_for("change_password"))
                return fn(*a, **kw)
            return wrapper
        return deco

    staff = login_required("admin", "staff")
    admin_only = login_required("admin")
    member_only = login_required("member")

    def home_for(u):
        return url_for("dashboard") if u["role"] in ("admin", "staff") else url_for("portal_home")

    @app.route("/")
    def index():
        if g.user:
            return redirect(home_for(g.user))
        return render_template("index.html", tiers=q("SELECT * FROM tiers ORDER BY annual_dues"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            u = q1("SELECT * FROM users WHERE email=? AND active=1", request.form["email"].strip().lower())
            if u and check_password_hash(u["password_hash"], request.form["password"]):
                session.clear()
                session["uid"] = u["id"]
                nxt = request.args.get("next", "")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = home_for(u)
                return redirect(nxt)
            flash("Invalid email or password.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/password", methods=["GET", "POST"])
    @login_required()
    def change_password():
        if request.method == "POST":
            new = request.form["new"]
            if not check_password_hash(g.user["password_hash"], request.form["current"]):
                flash("Current password is incorrect.", "error")
            elif len(new) < 10:
                flash("New password must be at least 10 characters.", "error")
            else:
                run("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                    generate_password_hash(new), g.user["id"])
                flash("Password updated.", "ok")
                return redirect(home_for(g.user))
        return render_template("password.html")

    # ---------- public: apply ----------
    @app.route("/apply", methods=["GET", "POST"])
    def apply():
        tiers = q("SELECT * FROM tiers ORDER BY annual_dues")
        if request.method == "POST":
            f = request.form
            email = f["email"].strip().lower()
            if not (f["first_name"].strip() and f["last_name"].strip() and email):
                flash("Name and email are required.", "error")
                return render_template("apply.html", tiers=tiers, f=f)
            if q1("SELECT 1 FROM members WHERE email=?", email):
                flash("A member with this email already exists. Please log in.", "error")
                return render_template("apply.html", tiers=tiers, f=f)
            tier = q1("SELECT id FROM tiers WHERE id=?", f.get("tier_id", type=int))
            if not tier:
                flash("Please choose a membership tier.", "error")
                return render_template("apply.html", tiers=tiers, f=f)
            lead = q1("SELECT id FROM leads WHERE email=? AND stage!='converted'", email)
            lead_id = lead["id"] if lead else run(
                "INSERT INTO leads(name, email, phone, organization, source, stage) VALUES (?,?,?,?,?,'qualified')",
                f"{f['first_name']} {f['last_name']}".strip(), email, f.get("phone", ""),
                f.get("organization", ""), "Website application")
            vals = [f.get(k, "").strip() for k in MEMBER_FIELDS]
            vals[2] = email
            app_id = run(f"INSERT INTO applications({','.join(MEMBER_FIELDS)}, tier_id, statement, lead_id) "
                         f"VALUES ({','.join('?' * len(MEMBER_FIELDS))},?,?,?)",
                         *vals, tier["id"], f.get("statement", ""), lead_id)
            db().execute("UPDATE leads SET stage='qualified' WHERE id=? AND stage IN ('new','contacted')", (lead_id,))
            log("application", f"Application #{app_id} submitted by {email}", lead_id=lead_id)
            db().commit()
            return render_template("apply_done.html")
        return render_template("apply.html", tiers=tiers, f={})

    # ---------- staff: dashboard ----------
    @app.route("/admin")
    @staff
    def dashboard():
        soon = (date.today() + timedelta(days=60)).isoformat()
        stats = {
            "active": q1("SELECT COUNT(*) c FROM members WHERE status='active'")["c"],
            "lapsed": q1("SELECT COUNT(*) c FROM members WHERE status='lapsed'")["c"],
            "pending": q1("SELECT COUNT(*) c FROM applications WHERE status='pending'")["c"],
            "open_leads": q1("SELECT COUNT(*) c FROM leads WHERE stage IN ('new','contacted','qualified')")["c"],
            "revenue_ytd": q1("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE paid_on>=?",
                              f"{date.today().year}-01-01")["s"],
        }
        pipeline = {s: q1("SELECT COUNT(*) c FROM leads WHERE stage=?", s)["c"] for s in LEAD_STAGES}
        renewals = q("SELECT * FROM members WHERE status='active' AND renews_on<=? ORDER BY renews_on LIMIT 15", soon)
        recent = q("""SELECT a.*, m.first_name||' '||m.last_name AS member_name FROM activity a
                      LEFT JOIN members m ON m.id=a.member_id ORDER BY a.id DESC LIMIT 15""")
        return render_template("admin/dashboard.html", stats=stats, pipeline=pipeline, renewals=renewals, recent=recent)

    # ---------- staff: leads ----------
    @app.route("/admin/leads")
    @staff
    def leads():
        stage = request.args.get("stage", "")
        rows = q("""SELECT l.*, u.name AS owner FROM leads l LEFT JOIN users u ON u.id=l.assigned_to
                    WHERE (?='' OR l.stage=?) ORDER BY l.id DESC""", stage, stage)
        return render_template("admin/leads.html", leads=rows, stage=stage,
                               staff_users=q("SELECT id, name FROM users WHERE role!='member' AND active=1"))

    @app.route("/admin/leads/new", methods=["POST"])
    @staff
    def lead_new():
        f = request.form
        if not f["name"].strip():
            flash("Name is required.", "error")
            return redirect(url_for("leads"))
        lid = run("INSERT INTO leads(name,email,phone,organization,source,notes,assigned_to) VALUES (?,?,?,?,?,?,?)",
                  f["name"].strip(), f.get("email", "").strip().lower(), f.get("phone", ""),
                  f.get("organization", ""), f.get("source", ""), f.get("notes", ""), g.user["id"])
        log("lead", f"Lead created: {f['name']}", lead_id=lid)
        db().commit()
        flash("Lead added.", "ok")
        return redirect(url_for("leads"))

    @app.route("/admin/leads/<int:lid>", methods=["GET", "POST"])
    @staff
    def lead_detail(lid):
        lead = q1("SELECT * FROM leads WHERE id=?", lid) or abort(404)
        if request.method == "POST":
            f = request.form
            stage = f["stage"] if f["stage"] in LEAD_STAGES else lead["stage"]
            if stage != lead["stage"]:
                log("lead", f"Stage {lead['stage']} -> {stage}", lead_id=lid)
            if f.get("note", "").strip():
                log("note", f["note"].strip(), lead_id=lid)
            owner = f.get("assigned_to", type=int)
            db().execute("UPDATE leads SET stage=?, notes=?, assigned_to=? WHERE id=?",
                         (stage, f.get("notes", ""), owner, lid))
            db().commit()
            flash("Lead updated.", "ok")
            return redirect(url_for("lead_detail", lid=lid))
        return render_template("admin/lead.html", lead=lead,
                               timeline=q("SELECT * FROM activity WHERE lead_id=? ORDER BY id DESC", lid),
                               apps=q("SELECT * FROM applications WHERE lead_id=?", lid),
                               staff_users=q("SELECT id, name FROM users WHERE role!='member' AND active=1"))

    # ---------- staff: applications ----------
    @app.route("/admin/applications")
    @staff
    def applications():
        status = request.args.get("status", "pending")
        rows = q("""SELECT a.*, t.name AS tier FROM applications a LEFT JOIN tiers t ON t.id=a.tier_id
                    WHERE (?='all' OR a.status=?) ORDER BY a.id DESC""", status, status)
        return render_template("admin/applications.html", apps=rows, status=status)

    @app.route("/admin/applications/<int:aid>", methods=["GET", "POST"])
    @staff
    def application_detail(aid):
        a = q1("""SELECT a.*, t.name AS tier, t.annual_dues FROM applications a
                  LEFT JOIN tiers t ON t.id=a.tier_id WHERE a.id=?""", aid) or abort(404)
        if request.method == "POST":
            if a["status"] != "pending":
                flash("This application was already decided.", "error")
                return redirect(url_for("application_detail", aid=aid))
            decision, note = request.form["decision"], request.form.get("note", "")
            if decision == "reject":
                db().execute("""UPDATE applications SET status='rejected', reviewed_by=?, reviewed_at=datetime('now'),
                                decision_note=? WHERE id=?""", (g.user["id"], note, aid))
                if a["lead_id"]:
                    db().execute("UPDATE leads SET stage='lost' WHERE id=?", (a["lead_id"],))
                log("application", f"Application #{aid} rejected", lead_id=a["lead_id"])
                db().commit()
                flash("Application rejected.", "ok")
                return redirect(url_for("applications"))
            if decision != "approve":
                abort(400)
            if q1("SELECT 1 FROM members WHERE email=?", a["email"]) or q1("SELECT 1 FROM users WHERE email=?", a["email"]):
                flash("A member or user with this email already exists.", "error")
                return redirect(url_for("application_detail", aid=aid))
            joined = date.today()
            mid = run(f"""INSERT INTO members({','.join(MEMBER_FIELDS)}, tier_id, status, joined_on, renews_on)
                          VALUES ({','.join('?' * len(MEMBER_FIELDS))},?, 'active', ?, ?)""",
                      *[a[k] for k in MEMBER_FIELDS], a["tier_id"], joined.isoformat(),
                      (joined + timedelta(days=365)).isoformat())
            temp = secrets.token_urlsafe(9)
            run("INSERT INTO users(email,name,password_hash,role,member_id,must_change_password) VALUES (?,?,?,'member',?,1)",
                a["email"], f"{a['first_name']} {a['last_name']}", generate_password_hash(temp), mid)
            db().execute("""UPDATE applications SET status='approved', reviewed_by=?, reviewed_at=datetime('now'),
                            decision_note=?, member_id=? WHERE id=?""", (g.user["id"], note, mid, aid))
            if a["lead_id"]:
                db().execute("UPDATE leads SET stage='converted' WHERE id=?", (a["lead_id"],))
            log("membership", f"Application #{aid} approved; member created ({a['tier']})", member_id=mid, lead_id=a["lead_id"])
            db().commit()
            # Shown once to staff; hand to the member securely. Member must change it at first login.
            flash(f"Approved. Portal login for {a['email']} — temporary password: {temp}", "ok")
            return redirect(url_for("member_detail", mid=mid))
        return render_template("admin/application.html", a=a)

    # ---------- staff: members ----------
    @app.route("/admin/members")
    @staff
    def members():
        s, status, tier = request.args.get("q", "").strip(), request.args.get("status", ""), request.args.get("tier", type=int)
        like = f"%{s}%"
        rows = q("""SELECT m.*, t.name AS tier FROM members m LEFT JOIN tiers t ON t.id=m.tier_id
                    WHERE (?='' OR m.status=?) AND (? IS NULL OR m.tier_id=?)
                    AND (?='' OR m.first_name||' '||m.last_name LIKE ? OR m.email LIKE ? OR m.organization LIKE ? OR m.tags LIKE ?)
                    ORDER BY m.last_name, m.first_name""", status, status, tier, tier, s, like, like, like, like)
        return render_template("admin/members.html", members=rows, qs=s, status=status, tier=tier,
                               tiers=q("SELECT * FROM tiers"))

    @app.route("/admin/members/export.csv")
    @staff
    def members_export():
        rows = q("""SELECT m.id, m.first_name, m.last_name, m.email, m.phone, m.organization, m.job_title,
                    m.address, m.city, m.state, m.zip, t.name AS tier, m.status, m.joined_on, m.renews_on, m.tags
                    FROM members m LEFT JOIN tiers t ON t.id=m.tier_id ORDER BY m.id""")
        out = io.StringIO()
        w = csv.writer(out)
        if rows:
            w.writerow(rows[0].keys())
        for r in rows:
            # neutralise spreadsheet formula injection
            w.writerow([("'" + str(v)) if isinstance(v, str) and v[:1] in "=+-@" else v for v in tuple(r)])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=members.csv"})

    @app.route("/admin/members/new", methods=["GET", "POST"])
    @staff
    def member_new():
        if request.method == "POST":
            f = request.form
            email = f["email"].strip().lower()
            if not (f["first_name"].strip() and f["last_name"].strip() and email):
                flash("Name and email are required.", "error")
            elif q1("SELECT 1 FROM members WHERE email=?", email):
                flash("A member with this email already exists.", "error")
            else:
                vals = [f.get(k, "").strip() for k in MEMBER_FIELDS]
                vals[2] = email
                joined = date.today()
                mid = run(f"""INSERT INTO members({','.join(MEMBER_FIELDS)}, tier_id, joined_on, renews_on)
                              VALUES ({','.join('?' * len(MEMBER_FIELDS))},?,?,?)""", *vals, f.get("tier_id", type=int),
                          joined.isoformat(), (joined + timedelta(days=365)).isoformat())
                log("membership", "Member added manually", member_id=mid)
                db().commit()
                return redirect(url_for("member_detail", mid=mid))
        return render_template("admin/member_form.html", tiers=q("SELECT * FROM tiers"), m=request.form, new=True)

    @app.route("/admin/members/<int:mid>")
    @staff
    def member_detail(mid):
        m = q1("SELECT m.*, t.name AS tier, t.annual_dues FROM members m LEFT JOIN tiers t ON t.id=m.tier_id WHERE m.id=?", mid) or abort(404)
        return render_template("admin/member.html", m=m,
            payments=q("SELECT * FROM payments WHERE member_id=? ORDER BY paid_on DESC, id DESC", mid),
            timeline=q("SELECT * FROM activity WHERE member_id=? ORDER BY id DESC", mid),
            events=q("""SELECT e.title, e.starts_at, r.attended FROM rsvps r JOIN events e ON e.id=r.event_id
                        WHERE r.member_id=? ORDER BY e.starts_at DESC""", mid),
            account=q1("SELECT id, active FROM users WHERE member_id=?", mid))

    @app.route("/admin/members/<int:mid>/edit", methods=["GET", "POST"])
    @staff
    def member_edit(mid):
        m = q1("SELECT * FROM members WHERE id=?", mid) or abort(404)
        if request.method == "POST":
            f = request.form
            email = f["email"].strip().lower()
            if q1("SELECT 1 FROM members WHERE email=? AND id!=?", email, mid):
                flash("Another member already uses that email.", "error")
                return render_template("admin/member_form.html", tiers=q("SELECT * FROM tiers"), m=f, new=False, mid=mid)
            status = f["status"] if f["status"] in MEMBER_STATUSES else m["status"]
            vals = [f.get(k, "").strip() for k in MEMBER_FIELDS]
            vals[2] = email
            sets = ",".join(f"{k}=?" for k in MEMBER_FIELDS)
            db().execute(f"UPDATE members SET {sets}, tier_id=?, status=?, renews_on=?, tags=?, notes=? WHERE id=?",
                         (*vals, f.get("tier_id", type=int), status, f.get("renews_on") or m["renews_on"],
                          f.get("tags", "").strip(), f.get("notes", ""), mid))
            db().execute("UPDATE users SET email=?, name=? WHERE member_id=?",
                         (email, f"{vals[0]} {vals[1]}", mid))
            if status != m["status"]:
                log("membership", f"Status {m['status']} -> {status}", member_id=mid)
                if status in ("suspended", "former"):
                    db().execute("UPDATE users SET active=0 WHERE member_id=?", (mid,))
                elif status == "active":
                    db().execute("UPDATE users SET active=1 WHERE member_id=?", (mid,))
            else:
                log("membership", "Record updated", member_id=mid)
            db().commit()
            flash("Member saved.", "ok")
            return redirect(url_for("member_detail", mid=mid))
        return render_template("admin/member_form.html", tiers=q("SELECT * FROM tiers"), m=m, new=False, mid=mid)

    @app.route("/admin/members/<int:mid>/note", methods=["POST"])
    @staff
    def member_note(mid):
        q1("SELECT 1 FROM members WHERE id=?", mid) or abort(404)
        if request.form.get("note", "").strip():
            log("note", request.form["note"].strip(), member_id=mid)
            db().commit()
        return redirect(url_for("member_detail", mid=mid))

    @app.route("/admin/members/<int:mid>/payment", methods=["POST"])
    @staff
    def member_payment(mid):
        m = q1("SELECT * FROM members WHERE id=?", mid) or abort(404)
        f = request.form
        try:
            amount = float(f["amount"])
            paid_on = date.fromisoformat(f["paid_on"])
        except (ValueError, KeyError):
            flash("Enter a valid amount and date.", "error")
            return redirect(url_for("member_detail", mid=mid))
        if amount <= 0:
            flash("Amount must be positive.", "error")
            return redirect(url_for("member_detail", mid=mid))
        db().execute("INSERT INTO payments(member_id,amount,method,paid_on,period,note,recorded_by) VALUES (?,?,?,?,?,?,?)",
                     (mid, amount, f.get("method", "check"), paid_on.isoformat(), f.get("period", ""), f.get("note", ""), g.user["id"]))
        msg = f"Payment of ${amount:,.2f} recorded"
        if f.get("renew"):
            base = max(date.fromisoformat(m["renews_on"]) if m["renews_on"] else paid_on, paid_on)
            new_renew = base + timedelta(days=365)
            db().execute("UPDATE members SET renews_on=?, status='active' WHERE id=?", (new_renew.isoformat(), mid))
            db().execute("UPDATE users SET active=1 WHERE member_id=? AND ?!='suspended'", (mid, m["status"]))
            msg += f"; renewed through {new_renew.isoformat()}"
        log("payment", msg, member_id=mid)
        db().commit()
        flash("Payment recorded.", "ok")
        return redirect(url_for("member_detail", mid=mid))

    @app.route("/admin/members/<int:mid>/reset", methods=["POST"])
    @staff
    def member_reset(mid):
        u = q1("SELECT * FROM users WHERE member_id=?", mid) or abort(404)
        temp = secrets.token_urlsafe(9)
        run("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", generate_password_hash(temp), u["id"])
        log("account", "Portal password reset", member_id=mid)
        db().commit()
        flash(f"Temporary password for {u['email']}: {temp}", "ok")
        return redirect(url_for("member_detail", mid=mid))

    # ---------- staff: payments, events, content, tiers ----------
    @app.route("/admin/payments")
    @staff
    def payments():
        rows = q("""SELECT p.*, m.first_name||' '||m.last_name AS member FROM payments p
                    JOIN members m ON m.id=p.member_id ORDER BY p.paid_on DESC, p.id DESC LIMIT 200""")
        return render_template("admin/payments.html", payments=rows)

    @app.route("/admin/events", methods=["GET", "POST"])
    @staff
    def events_admin():
        if request.method == "POST":
            f = request.form
            if f["title"].strip() and f["starts_at"]:
                eid = run("INSERT INTO events(title,starts_at,location,description) VALUES (?,?,?,?)",
                          f["title"].strip(), f["starts_at"], f.get("location", ""), f.get("description", ""))
                flash("Event created.", "ok")
            return redirect(url_for("events_admin"))
        rows = q("""SELECT e.*, (SELECT COUNT(*) FROM rsvps WHERE event_id=e.id) AS rsvps,
                    (SELECT COUNT(*) FROM rsvps WHERE event_id=e.id AND attended=1) AS attended
                    FROM events e ORDER BY starts_at DESC""")
        return render_template("admin/events.html", events=rows)

    @app.route("/admin/events/<int:eid>", methods=["GET", "POST"])
    @staff
    def event_detail(eid):
        e = q1("SELECT * FROM events WHERE id=?", eid) or abort(404)
        if request.method == "POST":
            attended = {int(x) for x in request.form.getlist("attended") if x.isdigit()}
            for r in q("SELECT member_id FROM rsvps WHERE event_id=?", eid):
                db().execute("UPDATE rsvps SET attended=? WHERE event_id=? AND member_id=?",
                             (1 if r["member_id"] in attended else 0, eid, r["member_id"]))
            db().commit()
            flash("Attendance saved.", "ok")
            return redirect(url_for("event_detail", eid=eid))
        return render_template("admin/event.html", e=e, rsvps=q(
            """SELECT r.*, m.first_name||' '||m.last_name AS name, m.email FROM rsvps r
               JOIN members m ON m.id=r.member_id WHERE r.event_id=? ORDER BY m.last_name""", eid))

    @app.route("/admin/content", methods=["GET", "POST"])
    @staff
    def content():
        if request.method == "POST":
            f = request.form
            if f["kind"] == "announcement" and f["title"].strip() and f.get("body", "").strip():
                run("INSERT INTO announcements(title, body) VALUES (?,?)", f["title"].strip(), f["body"])
            elif f["kind"] == "resource" and f["title"].strip() and f.get("url", "").lower().startswith(("http://", "https://")):
                run("INSERT INTO resources(title,url,category,description) VALUES (?,?,?,?)",
                    f["title"].strip(), f["url"].strip(), f.get("category", "General") or "General", f.get("description", ""))
            else:
                flash("Missing fields (resource URLs must start with http:// or https://).", "error")
                return redirect(url_for("content"))
            flash("Published to the member portal.", "ok")
            return redirect(url_for("content"))
        return render_template("admin/content.html", announcements=q("SELECT * FROM announcements ORDER BY id DESC"),
                               resources=q("SELECT * FROM resources ORDER BY category, title"))

    @app.route("/admin/tiers", methods=["GET", "POST"])
    @admin_only
    def tiers_admin():
        if request.method == "POST":
            f = request.form
            try:
                dues = float(f["annual_dues"])
                run("INSERT INTO tiers(name, annual_dues, description) VALUES (?,?,?)", f["name"].strip(), dues, f.get("description", ""))
                flash("Tier added.", "ok")
            except (ValueError, sqlite3.IntegrityError):
                flash("Enter a unique name and numeric dues.", "error")
            return redirect(url_for("tiers_admin"))
        return render_template("admin/tiers.html", tiers=q("SELECT * FROM tiers ORDER BY annual_dues"),
                               staff_users=q("SELECT id,name,email,role,active FROM users WHERE role!='member'"))

    @app.route("/admin/staff", methods=["POST"])
    @admin_only
    def staff_add():
        f = request.form
        email = f["email"].strip().lower()
        role = f.get("role") if f.get("role") in ("admin", "staff") else "staff"
        if not email or q1("SELECT 1 FROM users WHERE email=?", email):
            flash("Email missing or already in use.", "error")
        else:
            temp = secrets.token_urlsafe(9)
            run("INSERT INTO users(email,name,password_hash,role,must_change_password) VALUES (?,?,?,?,1)",
                email, f["name"].strip() or email, generate_password_hash(temp), role)
            flash(f"Staff account created. Temporary password: {temp}", "ok")
        return redirect(url_for("tiers_admin"))

    # ---------- member portal ----------
    def me():
        return q1("SELECT m.*, t.name AS tier FROM members m LEFT JOIN tiers t ON t.id=m.tier_id WHERE m.id=?",
                  g.user["member_id"])

    @app.route("/portal")
    @member_only
    def portal_home():
        return render_template("portal/home.html", m=me(),
            announcements=q("SELECT * FROM announcements ORDER BY id DESC LIMIT 5"),
            events=q("""SELECT e.*, EXISTS(SELECT 1 FROM rsvps WHERE event_id=e.id AND member_id=?) AS going
                        FROM events e WHERE date(e.starts_at)>=date('now') ORDER BY e.starts_at LIMIT 5""", g.user["member_id"]))

    @app.route("/portal/profile", methods=["GET", "POST"])
    @member_only
    def portal_profile():
        m = me()
        if request.method == "POST":
            f = request.form
            editable = [k for k in MEMBER_FIELDS if k not in ("first_name", "last_name", "email")]
            sets = ",".join(f"{k}=?" for k in editable)
            db().execute(f"UPDATE members SET {sets}, directory_opt_in=? WHERE id=?",
                         (*[f.get(k, "").strip() for k in editable], 1 if f.get("directory_opt_in") else 0, m["id"]))
            log("profile", "Member updated own profile", member_id=m["id"])
            db().commit()
            flash("Profile saved.", "ok")
            return redirect(url_for("portal_profile"))
        return render_template("portal/profile.html", m=m)

    @app.route("/portal/billing")
    @member_only
    def portal_billing():
        return render_template("portal/billing.html", m=me(), payments=q(
            "SELECT * FROM payments WHERE member_id=? ORDER BY paid_on DESC", g.user["member_id"]))

    @app.route("/portal/directory")
    @member_only
    def portal_directory():
        return render_template("portal/directory.html", members=q(
            """SELECT first_name, last_name, organization, job_title, email, city, state FROM members
               WHERE status='active' AND directory_opt_in=1 ORDER BY last_name"""))

    @app.route("/portal/resources")
    @member_only
    def portal_resources():
        if me()["status"] != "active":
            flash("Resources are available to active members. Please renew your membership.", "error")
            return redirect(url_for("portal_billing"))
        return render_template("portal/resources.html", resources=q("SELECT * FROM resources ORDER BY category, title"))

    @app.route("/portal/events", methods=["GET", "POST"])
    @member_only
    def portal_events():
        mid = g.user["member_id"]
        if request.method == "POST":
            eid = request.form.get("event_id", type=int)
            e = q1("SELECT * FROM events WHERE id=?", eid) or abort(404)
            if request.form.get("action") == "cancel":
                db().execute("DELETE FROM rsvps WHERE event_id=? AND member_id=?", (eid, mid))
                log("event", f"Cancelled RSVP: {e['title']}", member_id=mid)
            else:
                db().execute("INSERT OR IGNORE INTO rsvps(event_id, member_id) VALUES (?,?)", (eid, mid))
                log("event", f"RSVP: {e['title']}", member_id=mid)
            db().commit()
            return redirect(url_for("portal_events"))
        return render_template("portal/events.html", events=q(
            """SELECT e.*, EXISTS(SELECT 1 FROM rsvps WHERE event_id=e.id AND member_id=?) AS going
               FROM events e ORDER BY e.starts_at DESC""", mid))

    @app.errorhandler(403)
    def forbidden(_):
        return render_template("error.html", code=403, msg="You don't have access to this page."), 403

    @app.errorhandler(404)
    def notfound(_):
        return render_template("error.html", code=404, msg="Not found."), 404

    @app.errorhandler(400)
    def badreq(e):
        return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400

    @app.cli.command("init")
    def init_cmd():
        """Create tables and default tiers."""
        init_db()
        print("Database ready.")

    @app.cli.command("create-admin")
    def create_admin():
        """Create the first admin account (prompts for details)."""
        import getpass
        init_db()
        email = input("Admin email: ").strip().lower()
        name = input("Name: ").strip() or email
        pw = getpass.getpass("Password (min 10 chars): ")
        if len(pw) < 10:
            raise SystemExit("Password too short.")
        db().execute("INSERT INTO users(email,name,password_hash,role) VALUES (?,?,?,'admin')",
                     (email, name, generate_password_hash(pw)))
        db().commit()
        print("Admin created.")

    return app


if __name__ == "__main__":
    create_app().run(debug=False)
