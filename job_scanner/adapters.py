"""ATS adapters: each turns a company's public job board JSON into normalized Job objects.

Add a new ATS by writing one class with a .fetch(slug, company_name) method,
then registering it in ADAPTERS at the bottom. That's the whole maintenance story:
one adapter per ATS, not one crawler per company.
"""
from __future__ import annotations

import html as _html
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Job:
    source: str          # ats type, e.g. "greenhouse"
    company: str
    job_id: str          # ATS-native id -> stable across title/url changes
    title: str
    url: str
    location_raw: str = ""
    is_remote: bool | None = None
    department: str = ""
    description: str = ""
    posted_at: str = ""
    updated_at: str = ""
    fetched_at: str = ""

    @property
    def key(self) -> str:
        # Stable primary key. NOT url or title (those change).
        return f"{self.company}::{self.source}::{self.job_id}"

    def content_hash(self) -> str:
        norm_desc = re.sub(r"\s+", " ", (self.description or "")).strip().lower()[:2000]
        basis = "|".join([
            (self.title or "").strip().lower(),
            (self.location_raw or "").strip().lower(),
            (self.department or "").strip().lower(),
            str(self.is_remote),
            norm_desc,
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


class BaseAdapter:
    name = "base"

    def __init__(self, session):
        self.session = session

    def fetch(self, slug: str, company_name: str) -> list[Job]:
        raise NotImplementedError


class GreenhouseAdapter(BaseAdapter):
    name = "greenhouse"
    BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def fetch(self, slug, company_name):
        r = self.session.get(self.BASE.format(slug=slug), timeout=25)
        r.raise_for_status()
        data = r.json()
        out = []
        for j in data.get("jobs", []):
            loc = ((j.get("location") or {}).get("name") or "")
            depts = j.get("departments") or []
            dept = depts[0]["name"] if depts and depts[0].get("name") else ""
            out.append(Job(
                source="greenhouse",
                company=company_name,
                job_id=str(j.get("id")),
                title=j.get("title", "") or "",
                url=j.get("absolute_url", "") or "",
                location_raw=loc,
                is_remote="remote" in loc.lower(),
                department=dept,
                description=strip_html(j.get("content", ""))[:4000],
                updated_at=j.get("updated_at", "") or "",
            ))
        return out


class LeverAdapter(BaseAdapter):
    name = "lever"
    BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"

    def fetch(self, slug, company_name):
        r = self.session.get(self.BASE.format(slug=slug), timeout=25)
        r.raise_for_status()
        data = r.json()
        out = []
        for j in data:
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or ""
            wt = (j.get("workplaceType") or "").lower()
            posted = ""
            if j.get("createdAt"):
                try:
                    posted = datetime.fromtimestamp(j["createdAt"] / 1000, tz=timezone.utc).isoformat()
                except Exception:
                    pass
            out.append(Job(
                source="lever",
                company=company_name,
                job_id=str(j.get("id")),
                title=j.get("text", "") or "",
                url=j.get("hostedUrl", "") or "",
                location_raw=loc,
                is_remote=(wt == "remote") or ("remote" in loc.lower()),
                department=cats.get("team", "") or cats.get("department", "") or "",
                description=(j.get("descriptionPlain") or "")[:4000],
                posted_at=posted,
            ))
        return out


class AshbyAdapter(BaseAdapter):
    name = "ashby"
    BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

    def fetch(self, slug, company_name):
        r = self.session.get(self.BASE.format(slug=slug), timeout=25)
        r.raise_for_status()
        data = r.json()
        out = []
        for j in data.get("jobs", []):
            loc = j.get("location", "") or ""
            desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))
            out.append(Job(
                source="ashby",
                company=company_name,
                job_id=str(j.get("id")),
                title=j.get("title", "") or "",
                url=j.get("jobUrl") or j.get("applyUrl", "") or "",
                location_raw=loc,
                is_remote=bool(j.get("isRemote")),
                department=j.get("department", "") or j.get("team", "") or "",
                description=(desc or "")[:4000],
                posted_at=j.get("publishedAt", "") or "",
            ))
        return out


ADAPTERS = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}
