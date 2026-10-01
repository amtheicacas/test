"""Load demo data: python seed.py  (creates admin@icac.test / demo-password-123 -- demo only!)"""
from datetime import date, timedelta
from werkzeug.security import generate_password_hash
from app import create_app

app = create_app()
with app.app_context():
    from flask import g
    app.init_db()
    db = __import__("sqlite3").connect(app.config["DATABASE"])
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute("SELECT 1 FROM users").fetchone():
        raise SystemExit("Database already has users; refusing to seed.")
    h = generate_password_hash("demo-password-123")
    db.execute("INSERT INTO users(email,name,password_hash,role) VALUES ('admin@icac.test','Demo Admin',?, 'admin')", (h,))
    today = date.today()
    for i, (f, l, org, st) in enumerate([("Ada", "Lovelace", "Analytical Engines", "active"), ("Grace", "Hopper", "Navy Labs", "active"),
                                         ("Alan", "Turing", "Bletchley Group", "lapsed")]):
        cur = db.execute("INSERT INTO members(first_name,last_name,email,organization,tier_id,status,joined_on,renews_on,directory_opt_in) VALUES (?,?,?,?,1,?,?,?,1)",
                         (f, l, f"{f.lower()}@example.com", org, st, (today - timedelta(days=300)).isoformat(),
                          (today + timedelta(days=30 - 100 * (st == 'lapsed'))).isoformat()))
        db.execute("INSERT INTO users(email,name,password_hash,role,member_id) VALUES (?,?,?,'member',?)", (f"{f.lower()}@example.com", f"{f} {l}", h, cur.lastrowid))
        db.execute("INSERT INTO payments(member_id,amount,paid_on,period) VALUES (?,75,?,?)", (cur.lastrowid, (today - timedelta(days=300)).isoformat(), str(today.year)))
    db.execute("INSERT INTO leads(name,email,organization,source) VALUES ('Linus T','linus@example.com','Kernel Co','Conference')")
    db.execute("INSERT INTO events(title,starts_at,location,description) VALUES ('Annual Meeting',?, 'Main Hall','Yearly gathering')", ((today + timedelta(days=21)).isoformat() + "T18:00",))
    db.execute("INSERT INTO announcements(title,body) VALUES ('Welcome','Welcome to the new member portal.')")
    db.execute("INSERT INTO resources(title,url,category) VALUES ('Member handbook','https://example.com/handbook','Guides')")
    db.commit()
    print("Seeded. Log in as admin@icac.test or ada@example.com / demo-password-123")
