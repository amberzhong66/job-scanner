"""AI fit-classifier using DeepSeek (OpenAI-compatible API).

The candidate profile goes in the SYSTEM prompt (a stable prefix), so DeepSeek's
context cache kicks in across calls and input cost drops further. Only the job
goes in the user message. Output is strict JSON.

No key -> the pipeline simply skips this layer and stays rule-only.
"""
from __future__ import annotations

import json

SYSTEM_TEMPLATE = """You are a job-fit classifier helping ONE candidate triage roles.

CANDIDATE PROFILE:
{profile}

For the job the user gives you, decide how well it fits THIS candidate.
Read the description, not just the title — a generic title can hide a great fit, and a
keyword-matching title can hide a role the candidate explicitly does not want.

Respond with ONLY a JSON object, no prose, no markdown:
{{"fit": "strong" | "maybe" | "no",
  "reason": "<= 18 words, concrete, why it fits or not",
  "tags": ["<=4 short tags, e.g. finance-flavored-revops, systems-heavy, pure-accounting>"]}}

Rules:
- "strong" = squarely in the target zone AND not an anti-target.
- "no" = clearly an anti-target (pure accounting/audit/tax-compliance, pure SWE/backend/
  infra/ML), or unrelated function, regardless of matching keywords.
- "maybe" = adjacent or unclear from the description.
"""


class DeepSeekClassifier:
    def __init__(self, api_key, profile_text,
                 model="deepseek-v4-flash", base_url="https://api.deepseek.com"):
        from openai import OpenAI  # imported lazily so no key => no dependency needed
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.system = SYSTEM_TEMPLATE.format(profile=profile_text.strip())

    def classify(self, title, department, location, description) -> dict:
        user = (
            f"TITLE: {title}\n"
            f"DEPARTMENT: {department}\n"
            f"LOCATION: {location}\n"
            f"DESCRIPTION:\n{(description or '')[:3000]}"
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            stream=False,
        )
        data = json.loads(resp.choices[0].message.content)
        fit = str(data.get("fit", "")).lower().strip()
        if fit not in ("strong", "maybe", "no"):
            fit = "maybe"
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        return {
            "fit": fit,
            "reason": str(data.get("reason", ""))[:140],
            "tags": ",".join(str(t) for t in tags[:4]),
        }
