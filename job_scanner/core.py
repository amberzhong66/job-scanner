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
CREATE TABLE IF NOT EXISTS ai_cache(
  content_hash TEXT PRIMARY KEY, fit TEXT, reason TEXT, tags TEXT, model TEXT, ts TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # migrate older databases: add AI columns if missing
    for col in ("ai_fit TEXT", "ai_reason TEXT", "ai_tags TEXT"):
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return conn


# ---------- scoring ----------

def score_job(job: Job, prefs: dict):
    title = (job.title or "").lower()
    dept = (job.department or "").lower()

    def best(text):
        s = 0
        for kw, w in prefs["roles"]["positive"].items():
            if kw.strip().lower() in text:
                s = max(s, int(w))
        return s

    # A title match counts fully; a department-only match is capped, so an
    # entire "Revenue Operations" / "Financial Systems" department can't drag
    # every sub-role (sales ops, SWE, etc.) to the top of the queue.
    dept_cap = int(prefs["roles"].get("department_match_cap", 60))
    role = max(best(title), min(best(dept), dept_cap))

    negative_hit = any(n.lower() in f"{title} {dept}" for n in prefs["roles"]["negative"])
    if negative_hit and role < 100:
        role = max(0, role - 80)  # heavy penalty unless it's a perfect-match title

    # Location is decided by the TEXT first. The ATS "remote" flag is unreliable
    # (Ashby marks office roles isRemote=true to mean "remote-eligible"), so it
    # only yields a separate remote_eligible_score and never fakes a full 100.
    loc = (job.location_raw or "").lower()
    region = prefs["location"]["region_scores"]

    def hit(patterns):
        return any(re.search(r"\b" + re.escape(p.lower()) + r"\b", loc) for p in patterns)

    if hit(region["high"]["patterns"]):
        loc_score = region["high"]["score"]
    elif hit(region["nyc"]["patterns"]):
        loc_score = region["nyc"]["score"]
    elif "remote" in loc:
        loc_score = prefs["location"]["remote_score"]
    elif job.is_remote:
        loc_score = int(prefs["location"].get("remote_eligible_score", 70))
    else:
        loc_score = region["other"]["score"]

    seniority = ",".join(s for s in prefs["seniority_flags"] if s.lower() in title)

    # ---- hard rejects (total -> 0, i.e. dropped from the queue entirely) ----
    locp = prefs.get("location", {})

    def wb(patterns):  # word-boundary match anywhere in the location text
        return any(re.search(r"\b" + re.escape(p.lower()) + r"\b", loc) for p in patterns)

    # A location counts as "has US" if it names the US or any US region we track.
    us_signal = (wb(locp.get("us_signal_patterns", ["united states", "usa", "u.s.", "us"]))
                 or hit(region["high"]["patterns"]) or hit(region["nyc"]["patterns"]))
    non_us = wb(locp.get("non_us_reject", []))
    # Reject only PURELY non-US roles; "Remote US; Canada" keeps the US side.
    reject_location = (locp.get("non_us_hard_reject", True) and non_us and not us_signal)

    # Reject roles that are too senior (director/VP/head-of) — but NOT "staff"/
    # "senior" engineer titles, which can be exactly the target.
    sr_terms = prefs.get("seniority_reject", [])
    reject_senior = (prefs.get("seniority_hard_reject", True)
                     and any(re.search(r"\b" + re.escape(s.lower()) + r"\b", title) for s in sr_terms))

    w = prefs["weights"]
    if reject_location or reject_senior:
        total = 0.0
    else:
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
        self.ai_calls = 0          # how many times we actually hit the API
        self.ai_cached = 0         # how many verdicts were reused from cache
        self.ai_enabled = False
        self.deduped = 0           # how many duplicate postings were merged away


def _norm_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"\(.*?\)", " ", t)           # drop "(Remote)", "(Revenue)" etc.
    t = re.sub(r"\b(i{1,3}|iv|v)\b", " ", t)  # drop level roman numerals
    t = re.sub(r"[^a-z0-9 &/-]", " ", t)      # keep '-' and '/' so "- Public Sector" survives
    return re.sub(r"\s+", " ", t).strip()


def dedupe(current: dict):
    """Collapse multiple postings of the same role (same company + normalized
    title, e.g. one job listed across 23 cities) into ONE canonical Job.
    Keeps the longest description, unions the locations, OR-s is_remote.
    Canonical key = smallest key in the group, so the survivor is stable across runs."""
    groups = {}
    for key, j in current.items():
        gk = (j.company.strip().lower(), _norm_title(j.title))
        groups.setdefault(gk, []).append((key, j))

    out, merged = {}, 0
    for items in groups.values():
        if len(items) == 1:
            k, j = items[0]
            out[k] = j
            continue
        items.sort(key=lambda kv: kv[0])  # stable canonical
        ckey, cj = items[0]
        cj.description = max((j.description or "" for _, j in items), key=len)
        locs = []
        for _, j in items:
            if j.location_raw and j.location_raw not in locs:
                locs.append(j.location_raw)
            cj.is_remote = cj.is_remote or j.is_remote
        cj.location_raw = " | ".join(locs)[:300]
        out[ckey] = cj
        merged += len(items) - 1
    return out, merged


def run(companies, prefs, db_path, session, adapter_registry=None, mark_closed=True,
        classifier=None, max_ai=200, refresh_ai=False):
    adapter_registry = adapter_registry or ADAPTERS
    conn = connect(db_path)
    now = iso_now()
    res = ScanResult()
    res.ai_enabled = classifier is not None

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

    # per_company counts above are RAW (pre-dedupe) on purpose: the circuit
    # breaker should judge "did the fetch work", not the post-dedupe size.
    current, res.deduped = dedupe(current)
    res.fetched = len(current)

    scored = {}  # key -> (role, loc, total, seniority)
    for key, j in current.items():
        ch = j.content_hash()
        r, l, t, sen = score_job(j, prefs)
        scored[key] = (r, l, t, sen)
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

    conn.commit()

    # ---- AI precision layer (only if a classifier is supplied) ----
    if classifier is not None:
        prefilter = int(prefs.get("ai", {}).get("prefilter_min", 1))
        # only spend AI on jobs that survived the rules (role>0 AND not hard-rejected); best first
        candidates = sorted(
            [k for k in current if scored[k][0] >= prefilter and scored[k][2] > 0],
            key=lambda k: scored[k][2], reverse=True,
        )
        done = 0
        for key in candidates:
            if done >= max_ai:
                break
            j = current[key]
            ch = j.content_hash()
            cached = None
            if not refresh_ai:
                row = conn.execute("SELECT * FROM ai_cache WHERE content_hash=?", (ch,)).fetchone()
                cached = row
            if cached is not None:
                fit, reason, tags = cached["fit"], cached["reason"], cached["tags"]
                res.ai_cached += 1
            else:
                try:
                    v = classifier.classify(j.title, j.department, j.location_raw, j.description)
                except Exception:
                    continue  # one failed call shouldn't abort the whole run
                fit, reason, tags = v["fit"], v["reason"], v["tags"]
                if isinstance(tags, (list, tuple)):
                    tags = ",".join(str(x) for x in tags)
                reason = str(reason)
                conn.execute(
                    "INSERT OR REPLACE INTO ai_cache(content_hash,fit,reason,tags,model,ts) VALUES(?,?,?,?,?,?)",
                    (ch, fit, reason, tags, getattr(classifier, "model", "?"), now),
                )
                res.ai_calls += 1
                done += 1
            conn.execute(
                "UPDATE jobs SET ai_fit=?,ai_reason=?,ai_tags=? WHERE key=?",
                (fit, reason, tags, key),
            )
        conn.commit()

    conn.execute(
        "INSERT INTO scans(ts,companies,fetched,new_cnt,updated_cnt,closed_cnt) VALUES(?,?,?,?,?,?)",
        (now, len(companies), res.fetched, len(res.new), len(res.updated), len(res.closed)),
    )
    conn.commit()

    # ---- build apply queue ----
    # Once AI has run, its verdict leads: "no" is dropped even with a high keyword
    # score, "strong"/"maybe" float up. Jobs AI hasn't seen fall back to the rule
    # threshold so nothing silently disappears.
    fresh = set(res.new) | set(res.updated)
    thr = prefs["thresholds"]["apply_queue_min"]
    rank = {"strong": 3, "maybe": 2, None: 1, "": 1, "no": 0}
    rows = conn.execute("SELECT * FROM jobs WHERE status='open'").fetchall()
    keep = []
    for r in rows:
        if r["total_score"] == 0:
            continue  # hard-rejected (non-US / too senior) — never surface
        fit = r["ai_fit"] if "ai_fit" in r.keys() else None
        if fit == "no":
            continue
        if fit in ("strong", "maybe"):
            keep.append(r)
        elif (fit in (None, "")) and r["total_score"] >= thr:
            keep.append(r)
    keep.sort(key=lambda r: (rank.get(r["ai_fit"] if "ai_fit" in r.keys() else None, 1),
                             r["key"] in fresh, r["total_score"]), reverse=True)
    res.queue = keep
    res._fresh = fresh
    conn.close()
    return res


def export_queue(res, prefs, out_dir):
    import csv
    import os
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    os.makedirs(out_dir, exist_ok=True)
    cols = ["ai_fit", "ai_reason", "ai_tags", "total_score", "role_score", "location_score",
            "seniority", "company", "title", "location_raw", "department", "url", "status"]
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


def export_jds(db_path, out_dir, min_score=1):
    """Export full job descriptions for relevant open jobs.
    Writes one JSONL (machine-usable, for skill analysis / cover letters) and one
    readable .md. Only open jobs with total_score >= min_score (default: anything
    not hard-rejected). Returns (jsonl_path, md_path, count)."""
    import os
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    os.makedirs(out_dir, exist_ok=True)
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT company,title,location_raw,total_score,role_score,ai_fit,url,department,description "
        "FROM jobs WHERE status='open' AND total_score>=? ORDER BY total_score DESC",
        (min_score,),
    ).fetchall()
    conn.close()

    jsonl_path = os.path.join(out_dir, f"jds_{ts}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in r.keys()}, ensure_ascii=False) + "\n")

    md_path = os.path.join(out_dir, f"jds_{ts}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Job descriptions ({len(rows)} roles, score >= {min_score})\n\n")
        for r in rows:
            f.write(f"## {r['title']} — {r['company']}\n")
            f.write(f"- score {r['total_score']} | {r['location_raw']} | {r['url']}\n\n")
            f.write((r["description"] or "(no description captured)").strip() + "\n\n---\n\n")
    return jsonl_path, md_path, len(rows)

