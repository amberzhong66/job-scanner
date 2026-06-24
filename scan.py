#!/usr/bin/env python3
"""Job Scanner CLI.

Usage:
    python scan.py                 # run a full scan (uses AI if a key is set)
    python scan.py --reset         # wipe the database first (fresh baseline)
    python scan.py --top 30        # show more rows in the apply queue
    python scan.py --no-ai         # force rule-only even if a key is set
    python scan.py --refresh-ai    # re-ask the AI even for cached jobs
    python scan.py --max-ai 50     # cap how many API calls this run (cost guard)

AI layer (optional): put your DeepSeek key in a file named `.env` next to this script:
    DEEPSEEK_API_KEY=sk-xxxxxxxx
No key -> the scanner runs rule-only, exactly as before.
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


def load_dotenv():
    """Tiny .env reader (no extra dependency)."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_yaml(name):
    with open(os.path.join(HERE, "config", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile():
    p_ = os.path.join(HERE, "config", "profile.md")
    return open(p_, encoding="utf-8").read() if os.path.exists(p_) else ""


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "job-scanner/0.2 (personal job search)",
                      "Accept": "application/json"})
    return s


def p(msg):
    if HAVE_RICH:
        console.print(msg)
    else:
        import re
        print(re.sub(r"\[/?[a-z0-9 _#]*\]", "", msg))


FIT_STYLE = {"strong": "bold green", "maybe": "yellow", "no": "red", None: "dim", "": "dim"}


def show_queue(res, top):
    rows = res.queue[:top]
    if HAVE_RICH:
        t = Table(title=f"Apply Queue (top {len(rows)} of {len(res.queue)})", box=box.SIMPLE_HEAVY)
        headers = [("*", "yellow"), ("Fit", ""), ("Score", "bold cyan"),
                   ("Company", "green"), ("Title", ""), ("Location", "dim"),
                   ("Why (AI)", "italic")]
        for col, style in headers:
            t.add_column(col, style=style, overflow="fold")
        for r in rows:
            fit = r["ai_fit"] if "ai_fit" in r.keys() else None
            fit_txt = f"[{FIT_STYLE.get(fit,'dim')}]{(fit or '-').upper()}[/]"
            fresh = "NEW" if r["key"] in res._fresh else ""
            reason = (r["ai_reason"] if "ai_reason" in r.keys() else "") or ""
            t.add_row(fresh, fit_txt, str(r["total_score"]), r["company"],
                      r["title"], r["location_raw"] or "-", reason)
        console.print(t)
    else:
        for r in rows:
            fit = (r["ai_fit"] if "ai_fit" in r.keys() else None) or "-"
            fresh = "*" if r["key"] in res._fresh else " "
            print(f"{fresh} {fit.upper():<7} {r['total_score']:>5}  {r['company'][:16]:<16}  "
                  f"{r['title'][:44]:<44}  {r['location_raw'][:20]}")


def build_classifier(args, prefs):
    if args.no_ai:
        return None
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        p("[dim]No DEEPSEEK_API_KEY found - running rule-only. "
          "(Add it to a .env file to enable AI fit-classification.)[/dim]")
        return None
    try:
        from job_scanner.classifier import DeepSeekClassifier
    except Exception as e:
        p(f"[red]openai package not installed ({e}). Run: pip install openai[/red]")
        return None
    model = prefs.get("ai", {}).get("model", "deepseek-v4-flash")
    return DeepSeekClassifier(api_key=key, profile_text=load_profile(), model=model)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-close", action="store_true")
    ap.add_argument("--no-ai", action="store_true", help="force rule-only")
    ap.add_argument("--refresh-ai", action="store_true", help="ignore AI cache")
    ap.add_argument("--max-ai", type=int, default=200, help="cap API calls this run")
    args = ap.parse_args()

    load_dotenv()

    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        p("[dim]database reset[/dim]")

    companies = load_yaml("companies.yaml")["companies"]
    prefs = load_yaml("preferences.yaml")
    clf = build_classifier(args, prefs)

    mode = f"[cyan]AI: {prefs.get('ai',{}).get('model')}[/cyan]" if clf else "[dim]rule-only[/dim]"
    p(f"[bold]Scanning {len(companies)} companies...[/bold]  ({mode})")

    res = run(companies, prefs, DB_PATH, make_session(),
              mark_closed=not args.no_close, classifier=clf,
              max_ai=args.max_ai, refresh_ai=args.refresh_ai)

    p(f"\n[bold]Scan summary[/bold]: fetched [cyan]{res.fetched}[/cyan]  |  "
      f"[green]NEW {len(res.new)}[/green]  [yellow]UPDATED {len(res.updated)}[/yellow]  "
      f"[red]CLOSED {len(res.closed)}[/red]")
    if res.ai_enabled:
        p(f"[dim]AI: {res.ai_calls} new verdicts, {res.ai_cached} reused from cache[/dim]")

    if res.fetch_failures:
        p(f"\n[red]Fetch issues ({len(res.fetch_failures)}) - skipped, db untouched for them:[/red]")
        for name, err in res.fetch_failures:
            p(f"  [red].[/red] {name}: {err[:80]}")
        p("[dim]  (404 usually = wrong slug - see README find-slug)[/dim]")

    p("")
    show_queue(res, args.top)
    csv_path, json_path = export_queue(res, prefs, OUT_DIR)
    p(f"\n[dim]Saved:[/dim] {os.path.relpath(csv_path, HERE)}  |  {os.path.relpath(json_path, HERE)}")


if __name__ == "__main__":
    main()
