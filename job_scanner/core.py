"""Core pipeline: storage (SQLite) -> snapshot diff -> scoring -> apply queue."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from .adapters import ADAPTERS, Job


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  key TEXT PRIMARY KEY, source TEXT, company TEXT, job_id TEXT,
  title TEXT, url TEXT, location_raw TEXT, is_remote INTEGER, department TEXT,
  description TEXT, posted_at TEXT, updated_at TEXT,
  content_hash TEXT, status TEXT, first_seen TEXT, last_seen TEXT,
  role_score REAL, location_score REAL, total_score REAL, seniority TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, company TEXT, key TEXT,
  event TEXT, title TEXT
);
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, companies INTEGER,
  fetched INTEGER, new_cnt INTEGER, updated_cnt INTEGER, closed_cnt INTEGER
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------- scoring ----------

def score_job(job: Job, prefs: dict):
    title = (job.title or "").lower()
    dept = (job.department or "").lower()
    text = f"{title} {dept}"

    role = 0
    for kw, w in prefs["roles"]["positive"].items():
        if kw.strip().lower() in text:
            role = max(role, int(w))
    negative_hit = any(n.lower() in text for n in prefs["roles"]["negative"])
    if negative_hit and role < 100:
        role = max(0, role - 80)  # heavy penalty unless it's a perfect-match title

    loc = (job.location_raw or "").lower()
    region = prefs["location"]["region_scores"]
    loc_score = region["other"]["score"]
    if job.is_remote or "remote" in loc:
        loc_score = prefs["location"]["remote_score"]
    else:
        def hit(patterns):
            return any(re.search(r"\b" + re.escape(p.lower()) + r"\b", loc) for p in patterns)
        if hit(region["high"]["patterns"]):
            loc_score = region["high"]["score"]
        elif hit(region["nyc"]["patterns"]):
            loc_score = region["nyc"]["score"]

    seniority = ",".join(s for s in prefs["seniority_flags"] if s.lower() in title)

    w = prefs["weights"]
    total = round(role * w["role"] + loc_score * w["location"], 1)
    return role, loc_score, total, seniority


# ---------- db helpers ----------

def load_open_jobs(conn):
    rows = conn.execute("SELECT * FROM jobs WHERE status='open'").fetchall()
    return {r["key"]: r for r in rows}


def upsert_new(conn, j: Job, ch, now, r, l, t, sen):
    conn.execute(
        """INSERT OR REPLACE INTO jobs(key,source,company,job_id,title,url,location_raw,
           is_remote,department,description,posted_at,updated_at,content_hash,status,
           first_seen,last_seen,role_score,location_score,total_score,seniority)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (j.key, j.source, j.company, j.job_id, j.title, j.url, j.location_raw,
         1 if j.is_remote else 0, j.department, j.description, j.posted_at, j.updated_at,
         ch, "open", now, now, r, l, t, sen),
    )


def update_existing(conn, j: Job, ch, now, r, l, t, sen):
    conn.execute(
        """UPDATE jobs SET title=?,url=?,location_raw=?,is_remote=?,department=?,
           description=?,posted_at=?,updated_at=?,content_hash=?,status='open',last_seen=?,
           role_score=?,location_score=?,total_score=?,seniority=? WHERE key=?""",
        (j.title, j.url, j.location_raw, 1 if j.is_remote else 0, j.department,
         j.description, j.posted_at, j.updated_at, ch, now, r, l, t, sen, j.key),
    )


def touch(conn, key, now, r, l, t, sen):
    conn.execute(
        "UPDATE jobs SET last_seen=?,role_score=?,location_score=?,total_score=?,seniority=? WHERE key=?",
        (now, r, l, t, sen, key),
    )


def set_closed(conn, key, now):
    conn.execute("UPDATE jobs SET status='closed', last_seen=? WHERE key=?", (now, key))


def log_event(conn, now, company, key, event, title):
    conn.execute(
        "INSERT INTO events(ts,company,key,event,title) VALUES(?,?,?,?,?)",
        (now, company, key, event, title),
    )


# ---------- pipeline ----------

class ScanResult:
    def __init__(self):
        self.fetched = 0
        self.new, self.updated, self.closed = [], [], []
        self.fetch_failures = []   # (company, error)
        self.queue = []            # list of sqlite Row


def run(companies, prefs, db_path, session, adapter_registry=None, mark_closed=True):
    adapter_registry = adapter_registry or ADAPTERS
    conn = connect(db_path)
    now = iso_now()
    res = ScanResult()

    prev = load_open_jobs(conn)
    prev_counts = {}
    for row in prev.values():
        prev_counts[row["company"]] = prev_counts.get(row["company"], 0) + 1

    current = {}
    per_company = {}  # company -> (count, ok)
    for c in companies:
        cls = adapter_registry.get(c["ats_type"])
        if not cls:
            res.fetch_failures.append((c["name"], f"unknown ats_type '{c['ats_type']}'"))
            continue
        try:
            jobs = cls(session).fetch(c["slug"], c["name"])
            ok = True
        except Exception as e:
            res.fetch_failures.append((c["name"], str(e)))
            jobs, ok = [], False
        per_company[c["name"]] = (len(jobs), ok)
        for j in jobs:
            j.fetched_at = now
            current[j.key] = j

    res.fetched = len(current)

    for key, j in current.items():
        ch = j.content_hash()
        r, l, t, sen = score_job(j, prefs)
        if key not in prev:
            upsert_new(conn, j, ch, now, r, l, t, sen)
            res.new.append(key)
            log_event(conn, now, j.company, key, "new", j.title)
        elif prev[key]["content_hash"] != ch:
            update_existing(conn, j, ch, now, r, l, t, sen)
            res.updated.append(key)
            log_event(conn, now, j.company, key, "updated", j.title)
        else:
            touch(conn, key, now, r, l, t, sen)

    if mark_closed:
        for key, row in prev.items():
            if key in current:
                continue
            comp = row["company"]
            cnt, ok = per_company.get(comp, (0, False))
            prevc = prev_counts.get(comp, 0)
            # circuit breaker: a fetch failure or suspiciously small result
            # should NOT be read as "all jobs closed".
            if (not ok) or cnt == 0 or (prevc and cnt < 0.5 * prevc):
                continue
            set_closed(conn, key, now)
            res.closed.append(key)
            log_event(conn, now, comp, key, "closed", row["title"])

    conn.execute(
        "INSERT INTO scans(ts,companies,fetched,new_cnt,updated_cnt,closed_cnt) VALUES(?,?,?,?,?,?)",
        (now, len(companies), res.fetched, len(res.new), len(res.updated), len(res.closed)),
    )
    conn.commit()

    fresh = set(res.new) | set(res.updated)
    thr = prefs["thresholds"]["apply_queue_min"]
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='open' AND total_score>=? ORDER BY total_score DESC",
        (thr,),
    ).fetchall()
    rows.sort(key=lambda r: (r["key"] in fresh, r["total_score"]), reverse=True)
    res.queue = rows
    res._fresh = fresh
    conn.close()
    return res


def export_queue(res, prefs, out_dir):
    import csv
    import os
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    os.makedirs(out_dir, exist_ok=True)
    cols = ["total_score", "role_score", "location_score", "seniority", "company",
            "title", "location_raw", "department", "url", "status"]
    csv_path = os.path.join(out_dir, f"apply_queue_{ts}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fresh"] + cols)
        for r in res.queue:
            w.writerow([r["key"] in res._fresh] + [r[c] for c in cols])
    json_path = os.path.join(out_dir, f"apply_queue_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([{**{c: r[c] for c in cols}, "fresh": r["key"] in res._fresh}
                   for r in res.queue], f, ensure_ascii=False, indent=2)
    return csv_path, json_path
