"""Safe explanation and connection templates; no upstream requests or secrets."""

import json
from datetime import datetime

from quotamesh.routing import runtime_snapshot

REASONS = {
    "no_credentials": "No credential matches this provider target.",
    "disabled": "The profile or credential is disabled.",
    "expired": "The credential expiry date has passed.",
    "invalid": "The key was rejected; rotate or explicitly retest it.",
    "unusable": "Account, billing, or regional access is blocked.",
    "trial_blocked": "This profile does not allow trial access.",
    "paid_blocked": "This profile does not allow paid or unknown-plan access.",
    "missing_secret": "The referenced environment variable is missing from the server process.",
    "cooldown": "This quota group and model are temporarily rate limited.",
    "exhausted": "The provider reported quota exhaustion with reset evidence.",
    "degraded": "A recent provider or model-access failure temporarily blocks this candidate.",
    "cap_reached": "The profile's observed paid spending reached a configured cap.",
    "unknown_price": "Capped paid routing needs both input and output token prices.",
    "unknown_spend": "Unknown paid costs prevent measuring this cap safely.",
    "accounting_unavailable": "Paid accounting is incomplete; paid routing is blocked.",
}


def explain(app, slug):
    snapshot = runtime_snapshot(app, slug)
    profile = snapshot["profile"]
    if profile is None and slug != "default":
        raise LookupError("Profile not found")
    decisions = [
        {**d, "explanation": REASONS.get(d["skip_reason"], "Eligible under the saved policy.")}
        for d in snapshot["decisions"]
    ]
    now = datetime.fromisoformat(snapshot["evaluated_at"])
    recoveries = [
        d["recovery_at"]
        for d in decisions
        if d["recovery_at"] and datetime.fromisoformat(d["recovery_at"]) > now
    ]
    listed = {d["credential_id"] for d in decisions}
    caps = {}
    for period in ("daily", "monthly"):
        cap = (profile or {}).get(f"paid_{period}_cap_usd")
        unknown = bool(snapshot["usage"]["unknown_" + period])
        caps[period] = {
            "cap_usd": cap,
            "observed_usd": snapshot["usage"][period],
            "headroom_usd": None
            if cap is None or unknown
            else max(0, cap - snapshot["usage"][period]),
            "incomplete": unknown,
            "source": "LOCAL",
            "window": "UTC",
        }
    return {
        "evaluated_at": snapshot["evaluated_at"],
        "configured": profile is not None,
        "profile": profile,
        "decisions": decisions,
        "selected": next((d for d in decisions if d["eligible"]), None),
        "earliest_recovery_at": min(recoveries, default=None),
        "caps": caps,
        "unallocated": [
            {
                "credential_id": c["id"],
                "label": c["label"],
                "provider_id": c["provider_id"],
                "reason": "Not listed in this profile; never considered for its requests.",
            }
            for c in snapshot["credentials"].values()
            if c["id"] not in listed
        ],
        "notice": "Policy eligibility, not a live quota guarantee. Only gateway traffic is counted.",
    }


def integration_snippets(base_url, slug, shell="bash", name=None):
    alias = "qm/" + slug
    if shell == "powershell":
        env = (
            f'$env:QUOTAMESH_BASE_URL = "{base_url}"\n'
            f'$env:QUOTAMESH_MODEL = "{alias}"\n'
            "$env:QUOTAMESH_KEY = (quotamesh key | Out-String).Trim()"
        )
        curl = (
            '$body = @{model=$env:QUOTAMESH_MODEL; messages=@(@{role="user"; '
            'content="Hello"})} | ConvertTo-Json -Depth 5\n'
            'Invoke-RestMethod "$env:QUOTAMESH_BASE_URL/chat/completions" -Method Post '
            '-Headers @{Authorization="Bearer $env:QUOTAMESH_KEY"} '
            '-ContentType "application/json" -Body $body'
        )
    else:
        env = (
            f'export QUOTAMESH_BASE_URL="{base_url}"\n'
            f'export QUOTAMESH_MODEL="{alias}"\n'
            'export QUOTAMESH_KEY="$(quotamesh key)"'
        )
        curl = (
            'curl "$QUOTAMESH_BASE_URL/chat/completions" \\\n'
            '  -H "Authorization: Bearer $QUOTAMESH_KEY" \\\n'
            '  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"model":"{alias}","messages":[{{"role":"user","content":"Hello"}}]}}\''
        )
    python = (
        "import os\nfrom openai import OpenAI\n\n"
        'client = OpenAI(base_url=os.environ["QUOTAMESH_BASE_URL"],\n'
        '                api_key=os.environ["QUOTAMESH_KEY"], max_retries=0)\n'
        "response = client.chat.completions.create(\n"
        '    model=os.environ["QUOTAMESH_MODEL"],\n'
        '    messages=[{"role": "user", "content": "Hello"}],\n)\n'
        "print(response.choices[0].message.content)"
    )
    node = (
        'import OpenAI from "openai";\n\n'
        "const client = new OpenAI({baseURL: process.env.QUOTAMESH_BASE_URL,\n"
        "  apiKey: process.env.QUOTAMESH_KEY, maxRetries: 0});\n"
        "const response = await client.chat.completions.create({\n"
        "  model: process.env.QUOTAMESH_MODEL,\n"
        '  messages: [{role: "user", content: "Hello"}],\n});\n'
        "console.log(response.choices[0].message.content);"
    )
    opencode = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": "quotamesh/" + alias,
            "provider": {
                "quotamesh": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "QuotaMesh",
                    "options": {"baseURL": base_url, "apiKey": "{env:QUOTAMESH_KEY}"},
                    "models": {alias: {"name": name or alias}},
                }
            },
        },
        indent=2,
    )
    return {
        "base_url": base_url,
        "model": alias,
        "shell": shell,
        "snippets": {
            "env": env,
            "python": python,
            "node": node,
            "curl": curl,
            "opencode": opencode,
        },
        "instructions": {
            "python": "Install openai; set the environment variables, then run the Python file.",
            "node": "Install openai with npm; save as .mjs and run with node.",
            "curl": "Set the environment variables first. PowerShell uses Invoke-RestMethod.",
            "opencode": "Merge into opencode.json; set QUOTAMESH_KEY in the OpenCode process.",
            "env": "Run in your client's shell. The local gateway key is separate from provider keys.",
        },
        "notice": "Chat Completions only. Model tool/vision support depends on your explicit targets.",
    }
