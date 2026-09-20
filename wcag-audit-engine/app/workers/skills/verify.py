"""Check specific claims against specific sources: for each claim, does the
material support it, contradict it, or say nothing about it -- with the
quote the verdict is decided from.
"""

from .. import runtime
from ..providers import gemini
from . import extract as extract_skill

MAX_CLAIMS = 10
MAX_SOURCES = 4

_VERIFIER = (
    "You are a careful fact-checker. For each claim, decide whether the "
    "sources SUPPORT it, CONTRADICT it, or are UNSUPPORTED (the sources say "
    "nothing on point). Quote the exact sentence the verdict is based on, "
    "or null if UNSUPPORTED. Never infer beyond what the text states."
)


async def verify_claims(ctx, payload: dict) -> dict:
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims or not all(
            isinstance(c, str) and c.strip() for c in claims):
        raise runtime.InvalidRequest("`claims` must be a non-empty list of strings.")
    if len(claims) > MAX_CLAIMS:
        raise runtime.InvalidRequest(f"At most {MAX_CLAIMS} claims per call.")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise runtime.InvalidRequest("`sources` must be a non-empty list of URLs.")
    if len(sources) > MAX_SOURCES:
        raise runtime.InvalidRequest(f"At most {MAX_SOURCES} sources per call.")
    urls = [extract_skill.validate_url(u, field="sources") for u in sources]

    read, unread = [], []
    for url in urls:
        if ctx.remaining() < 30:
            unread.append({"url": url, "reason": "ran out of time before this source"})
            continue
        try:
            page = await extract_skill.extract_page(ctx, {"url": url})
            read.append({"url": page.get("final_url") or url, "title": page.get("title"),
                        "text": page["text"][:6000]})
        except runtime.WorkerError as exc:
            unread.append({"url": url, "reason": exc.detail})

    if not read:
        raise runtime.TransientProviderError(
            "None of the given sources could be read.", reason="no_sources")

    material = "\n\n---\n\n".join(
        f"[{i + 1}] {r['title'] or r['url']} ({r['url']}):\n{r['text']}"
        for i, r in enumerate(read))
    claims_list = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
    prompt = (
        f"Sources:\n\n{material}\n\n---\n\nClaims to check:\n{claims_list}\n\n"
        'Return a JSON object: {"verdicts": [{"claim": "...", '
        '"verdict": "SUPPORTED|CONTRADICTED|UNSUPPORTED", "quote": "..." or null, '
        '"source_n": <source number> or null}, ...]} with exactly one entry per '
        "claim, in the same order as given."
    )

    async def call(provider):
        return await provider.generate_json(prompt, system=_VERIFIER, temperature=0.0)

    value = await ctx.run("verify", gemini.PROVIDERS, call, per_attempt_seconds=90)
    parsed = value["json"]
    verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(verdicts, list) or len(verdicts) != len(claims):
        raise runtime.InvalidProviderResponse("Model did not return one verdict per claim.")

    return {
        "claims": claims,
        "verdicts": verdicts,
        "sources_read": [{"n": i + 1, "url": r["url"], "title": r["title"]}
                         for i, r in enumerate(read)],
        "sources_unread": unread,
        "model": value["model"],
    }


SKILLS = {"verify.claims": verify_claims}
