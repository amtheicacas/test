CREATE TABLE IF NOT EXISTS tiers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, annual_dues REAL NOT NULL DEFAULT 0, description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS members (
  id INTEGER PRIMARY KEY,
  first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  phone TEXT DEFAULT '', organization TEXT DEFAULT '', job_title TEXT DEFAULT '',
  address TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '', zip TEXT DEFAULT '',
  tier_id INTEGER REFERENCES tiers(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','lapsed','suspended','former')),
  joined_on TEXT, renews_on TEXT,
  directory_opt_in INTEGER NOT NULL DEFAULT 0,
  tags TEXT DEFAULT '', notes TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','staff','member')),
  member_id INTEGER REFERENCES members(id), active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT '', phone TEXT DEFAULT '',
  organization TEXT DEFAULT '', source TEXT DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'new' CHECK (stage IN ('new','contacted','qualified','converted','lost')),
  assigned_to INTEGER REFERENCES users(id), notes TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY, lead_id INTEGER REFERENCES leads(id),
  first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL,
  phone TEXT DEFAULT '', organization TEXT DEFAULT '', job_title TEXT DEFAULT '',
  address TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '', zip TEXT DEFAULT '',
  tier_id INTEGER REFERENCES tiers(id), statement TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  submitted_at TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, decision_note TEXT DEFAULT '',
  member_id INTEGER REFERENCES members(id)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
  amount REAL NOT NULL, method TEXT DEFAULT 'check', paid_on TEXT NOT NULL,
  period TEXT DEFAULT '', note TEXT DEFAULT '', recorded_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at TEXT NOT NULL,
  location TEXT DEFAULT '', description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rsvps (
  event_id INTEGER NOT NULL REFERENCES events(id), member_id INTEGER NOT NULL REFERENCES members(id),
  attended INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (event_id, member_id)
);
CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
  category TEXT DEFAULT 'General', description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY, member_id INTEGER REFERENCES members(id), lead_id INTEGER REFERENCES leads(id),
  kind TEXT NOT NULL, summary TEXT NOT NULL, actor TEXT DEFAULT 'system',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_member ON activity(member_id);
CREATE INDEX IF NOT EXISTS idx_payments_member ON payments(member_id);
