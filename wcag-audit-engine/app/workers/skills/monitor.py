"""Baseline a page, then check it later and get a diff summary.

Two workers sharing one piece of state (the ledger): `monitor.snapshot`
writes it, `monitor.check` reads the last one, re-fetches, and asks Gemini
what changed. The ledger is the only place this state CAN live -- two paid
calls are, from the caller's side, unrelated purchases with no session
between them.
"""

import hashlib

from .. import ledger, runtime
from . import extract as extract_skill
from . import llm as llm_skill


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


async def snapshot(ctx, payload: dict) -> dict:
    url = extract_skill.validate_url(payload.get("url"))
    page = await extract_skill.extract_page(ctx, {"url": url})
    content_hash = _hash(page["text"])
    ledger.save_monitor_snapshot(url, content_hash, page["text"][:20_000], page.get("title"))
    return {
        "url": url,
        "title": page.get("title"),
        "text_chars": page["text_chars"],
        "content_hash": content_hash,
        "note": "Baseline saved. Call monitor.check on this URL later to see what changed.",
    }


async def check(ctx, payload: dict) -> dict:
    url = extract_skill.validate_url(payload.get("url"))
    previous = ledger.get_monitor_snapshot(url)
    if previous is None:
        raise runtime.InvalidRequest(
            "No baseline exists for this URL yet. Call monitor.snapshot on it first.")

    page = await extract_skill.extract_page(ctx, {"url": url})
    new_hash = _hash(page["text"])
    changed = new_hash != previous["content_hash"]

    result = {
        "url": url,
        "title": page.get("title"),
        "changed": changed,
        "baseline_age_seconds": previous["age_seconds"],
    }

    if changed:
        material = (
            f"PREVIOUS version of this page:\n\n{previous['text'][:8000]}\n\n---\n\n"
            f"CURRENT version:\n\n{page['text'][:8000]}")
        analysis = await llm_skill.analyze(ctx, {
            "text": material,
            "question": ("Summarize concretely what changed between the previous and "
                        "current version. If nothing meaningful changed beyond "
                        "formatting or whitespace, say so explicitly."),
        })
        result["change_summary"] = analysis["answer"]
        result["model"] = analysis["model"]
    else:
        result["change_summary"] = "No change detected since the last baseline."

    ledger.save_monitor_snapshot(url, new_hash, page["text"][:20_000], page.get("title"))
    return result


SKILLS = {"monitor.snapshot": snapshot, "monitor.check": check}
