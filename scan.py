#!/usr/bin/env python3
"""Job Scanner CLI.

Usage:
    python scan.py                 # run a full scan
    python scan.py --reset         # wipe the database first (fresh baseline)
    python scan.py --top 30        # show more rows in the apply queue
"""
import argparse
import os
import sys

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from job_scanner.core import run, export_queue  # noqa: E402

DB_PATH = os.path.join(HERE, "jobs.db")
OUT_DIR = os.path.join(HERE, "output")

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    console = Console()
    HAVE_RICH = True
except Exception:
    HAVE_RICH = False


def load_yaml(name):
    with open(os.path.join(HERE, "config", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "job-scanner/0.1 (personal job search)",
        "Accept": "application/json",
    })
    return s


def p(msg):
    if HAVE_RICH:
        console.print(msg)
    else:
        # strip rich markup for plain terminals
        import re
        print(re.sub(r"\[/?[a-z0-9 _#]*\]", "", msg))


def show_queue(res, top):
    rows = res.queue[:top]
    if HAVE_RICH:
        t = Table(title=f"Apply Queue (top {len(rows)} of {len(res.queue)})", box=box.SIMPLE_HEAVY)
        for col, style in [("★", "yellow"), ("Score", "bold cyan"), ("Role", ""),
                           ("Loc", ""), ("Company", "green"), ("Title", ""),
                           ("Location", "dim"), ("Sr", "magenta")]:
            t.add_column(col, style=style, overflow="fold")
        for r in rows:
            fresh = "NEW" if r["key"] in res._fresh else ""
            t.add_row(fresh, str(r["total_score"]), str(int(r["role_score"])),
                      str(int(r["location_score"])), r["company"], r["title"],
                      r["location_raw"] or "-", r["seniority"] or "")
        console.print(t)
    else:
        for r in rows:
            fresh = "*" if r["key"] in res._fresh else " "
            print(f"{fresh} {r['total_score']:>5}  {r['company'][:18]:<18}  "
                  f"{r['title'][:48]:<48}  {r['location_raw'][:22]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe DB before scanning")
    ap.add_argument("--top", type=int, default=20, help="rows to show in queue")
    ap.add_argument("--no-close", action="store_true", help="skip closed detection")
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        p("[dim]database reset[/dim]")

    companies = load_yaml("companies.yaml")["companies"]
    prefs = load_yaml("preferences.yaml")

    p(f"[bold]Scanning {len(companies)} companies...[/bold]")
    res = run(companies, prefs, DB_PATH, make_session(), mark_closed=not args.no_close)

    p(f"\n[bold]Scan summary[/bold]: fetched [cyan]{res.fetched}[/cyan] open postings  |  "
      f"[green]NEW {len(res.new)}[/green]  "
      f"[yellow]UPDATED {len(res.updated)}[/yellow]  "
      f"[red]CLOSED {len(res.closed)}[/red]")

    if res.fetch_failures:
        p(f"\n[red]Fetch issues ({len(res.fetch_failures)}) — these companies were skipped, "
          f"db NOT touched for them:[/red]")
        for name, err in res.fetch_failures:
            p(f"  [red]·[/red] {name}: {err[:80]}")
        p("[dim]  (A 404/Not Found usually means the slug is wrong — see README §find-slug)[/dim]")

    p("")
    show_queue(res, args.top)

    csv_path, json_path = export_queue(res, prefs, OUT_DIR)
    p(f"\n[dim]Saved:[/dim] {os.path.relpath(csv_path, HERE)}  |  {os.path.relpath(json_path, HERE)}")


if __name__ == "__main__":
    main()
