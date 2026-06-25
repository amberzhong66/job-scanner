#!/usr/bin/env python3
"""Quick analysis of jobs.db — read-only, prints a profile of what you've collected.
Run:  python analyze.py        (uses ./jobs.db)
      python analyze.py path/to/jobs.db
SQLite ships with Python, so no install needed.
"""
import os
import re
import sqlite3
import sys
from collections import Counter

DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db")

if not os.path.exists(DB):
    print(f"找不到 {DB} —— 先在 job-scanner 文件夹里跑 python analyze.py，或把 db 路径作为参数传进来。")
    sys.exit(1)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
q = lambda s, *a: conn.execute(s, a).fetchall()
one = lambda s, *a: conn.execute(s, a).fetchone()[0]


def bar(n, total, width=30):
    if not total:
        return ""
    return "█" * max(1, round(width * n / total))


print("=" * 70)
print(f"  jobs.db 分析  ({DB})")
print("=" * 70)

# --- overall ---
total = one("SELECT COUNT(*) FROM jobs")
open_n = one("SELECT COUNT(*) FROM jobs WHERE status='open'")
closed_n = one("SELECT COUNT(*) FROM jobs WHERE status='closed'")
scans = one("SELECT COUNT(*) FROM scans")
print(f"\n[总览]  岗位总数 {total}   |   open {open_n}   closed {closed_n}   |   扫描次数 {scans}")

# --- per source ---
print("\n[按数据源]")
for r in q("SELECT source, COUNT(*) c FROM jobs WHERE status='open' GROUP BY source ORDER BY c DESC"):
    print(f"  {r['source']:<12} {r['c']:>4}  {bar(r['c'], open_n)}")

# --- per company ---
print("\n[按公司 — open 岗位数，看哪些公司值钱]")
rows = q("SELECT company, COUNT(*) c FROM jobs WHERE status='open' GROUP BY company ORDER BY c DESC")
for r in rows:
    print(f"  {r['company']:<14} {r['c']:>4}  {bar(r['c'], rows[0]['c'])}")

# --- duplicate analysis (evidence for whether to build dedupe) ---
print("\n[重复岗位 — 同公司+相同归一化标题出现多次]")


def norm(t):
    t = (t or "").lower()
    t = re.sub(r"\(.*?\)", "", t)              # drop (Remote) etc.
    t = re.sub(r"\b(i{1,3}|iv|v)\b", "", t)     # drop roman numerals
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


seen = Counter()
groups = {}
for r in q("SELECT company, title FROM jobs WHERE status='open'"):
    k = (r["company"], norm(r["title"]))
    seen[k] += 1
    groups.setdefault(k, []).append(r["title"])
dups = {k: v for k, v in seen.items() if v > 1}
dup_extra = sum(v - 1 for v in dups.values())
print(f"  有 {len(dups)} 组重复，多占了 {dup_extra} 条记录（占 open 的 {dup_extra*100//max(open_n,1)}%）")
for k, v in sorted(dups.items(), key=lambda x: -x[1])[:8]:
    print(f"    {v}x  {k[0]} — {groups[k][0][:50]}")

# --- location buckets ---
print("\n[地点分布 — open]")
buckets = Counter()
NON_US = ["canada", "toronto", "poland", "india", "bengaluru", "ireland", "dublin",
          "united kingdom", "london", "germany", "france", "spain", "emea", "apac"]
for r in q("SELECT location_raw, is_remote FROM jobs WHERE status='open'"):
    loc = (r["location_raw"] or "").lower()
    has_us = bool(re.search(r"\b(united states|usa|u\.s\.|us)\b", loc)) or "remote - us" in loc
    has_non_us = any(re.search(r"\b" + re.escape(p) + r"\b", loc) for p in NON_US)
    if "remote" in loc and has_us:
        buckets["Remote US"] += 1
    elif has_non_us and not has_us:
        buckets["纯非美国"] += 1
    elif "remote" in loc:
        buckets["Remote (未注明国家)"] += 1
    else:
        buckets["其它/onsite"] += 1
for name, c in buckets.most_common():
    print(f"  {name:<20} {c:>4}  {bar(c, open_n)}")

# --- score distribution ---
print("\n[分数分布 — open，total_score]")
band = Counter()
for r in q("SELECT total_score FROM jobs WHERE status='open'"):
    s = r["total_score"] or 0
    if s == 0:
        band["0 (被否决)"] += 1
    elif s < 45:
        band["1–44 (门槛下)"] += 1
    elif s < 70:
        band["45–69"] += 1
    elif s < 90:
        band["70–89"] += 1
    else:
        band["90+"] += 1
for name in ["90+", "70–89", "45–69", "1–44 (门槛下)", "0 (被否决)"]:
    if band[name]:
        print(f"  {name:<16} {band[name]:>4}  {bar(band[name], open_n)}")

# --- AI status ---
ai_done = one("SELECT COUNT(*) FROM jobs WHERE status='open' AND ai_fit IS NOT NULL AND ai_fit<>''")
print(f"\n[AI] 已判过 fit 的 open 岗位：{ai_done} / {open_n}")
if ai_done:
    for r in q("SELECT ai_fit, COUNT(*) c FROM jobs WHERE status='open' AND ai_fit<>'' GROUP BY ai_fit"):
        print(f"  {r['ai_fit']:<8} {r['c']}")

print("\n" + "=" * 70)
conn.close()
