"""Inference workers backed by Gemini on Vertex."""

from .. import runtime
from ..providers import completion, gemini

MAX_INPUT_CHARS = 200_000

_ANALYST = (
    "You are a precise analyst. Answer only from the material provided. "
    "If the material does not contain the answer, say so explicitly rather "
    "than inferring it. Be specific and concise."
)


def _require_text(payload: dict) -> str:
    text = payload.get("text")
    if not text or not isinstance(text, str) or not text.strip():
        raise runtime.InvalidRequest("`text` is required.")
    if len(text) > MAX_INPUT_CHARS:
        raise runtime.InvalidRequest(
            f"`text` is {len(text)} characters, over the {MAX_INPUT_CHARS} limit.")
    return text


async def analyze(ctx, payload: dict) -> dict:
    """Answer a question about supplied text.

    The instruction to refuse rather than infer IS the product: a buyer
    reselling this downstream needs "not stated" to come back as "not stated",
    not as a confident guess. That is the same rule the audit routes follow --
    a check that could not run is never reported as a pass.
    """
    text = _require_text(payload)
    question = (payload.get("question") or "Summarize the key points.").strip()

    prompt = f"Material:\n\n{text}\n\n---\n\nTask: {question}"

    async def call(provider):
        return await provider.generate(
            prompt, system=_ANALYST,
            temperature=float(payload.get("temperature", 0.2)))

    value = await ctx.run("analyze", gemini.PROVIDERS, call, per_attempt_seconds=120)
    return {
        "question": question,
        "answer": value["text"],
        "model": value["model"],
        "input_chars": len(text),
        "tokens": {"prompt": value["prompt_tokens"], "output": value["output_tokens"]},
    }


async def extract_structured(ctx, payload: dict) -> dict:
    """Pull caller-specified fields out of text as JSON.

    `fields` is the caller's schema, and the answer comes back in exactly that
    shape. Returning their keys rather than ours is what makes this
    composable: the next worker, or the buyer's own code, can rely on what it
    asked for instead of parsing prose.
    """
    text = _require_text(payload)
    fields = payload.get("fields")
    if (not isinstance(fields, list) or not fields
            or not all(isinstance(f, str) and f.strip() for f in fields)):
        raise runtime.InvalidRequest("`fields` must be a non-empty list of field names.")

    prompt = (
        f"Material:\n\n{text}\n\n---\n\n"
        f"Extract exactly these fields as a JSON object: {', '.join(fields)}.\n"
        "Use null for any field the material does not state. Do not guess."
    )

    async def call(provider):
        return await provider.generate_json(prompt, system=_ANALYST, temperature=0.0)

    value = await ctx.run("extract_structured", gemini.PROVIDERS, call,
                          per_attempt_seconds=120)
    parsed = value["json"]
    if not isinstance(parsed, dict):
        raise runtime.InvalidProviderResponse("Model returned JSON that is not an object.")
    # Guarantee the caller's keys exist, so a field the page did not state is
    # an explicit null rather than a KeyError in the buyer's code.
    return {
        "fields": {field: parsed.get(field) for field in fields},
        "model": value["model"],
        "tokens": {"prompt": value["prompt_tokens"], "output": value["output_tokens"]},
    }


async def generate(ctx, payload: dict) -> dict:
    """Raw text completion: your prompt, your system message, your model
    choice among what this deployment has configured. Unlike llm.analyze,
    nothing about the shape of the answer is prescribed."""
    prompt = payload.get("prompt")
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        raise runtime.InvalidRequest("`prompt` is required.")
    if len(prompt) > MAX_INPUT_CHARS:
        raise runtime.InvalidRequest(
            f"`prompt` is {len(prompt)} characters, over the {MAX_INPUT_CHARS} limit.")
    system = payload.get("system")
    if system is not None and (not isinstance(system, str) or len(system) > 2000):
        raise runtime.InvalidRequest(
            "`system`, when given, must be a string up to 2000 characters.")
    try:
        max_tokens = int(payload.get("max_tokens", 1024))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`max_tokens` must be a whole number.")
    if not 1 <= max_tokens <= 4096:
        raise runtime.InvalidRequest("`max_tokens` must be between 1 and 4096.")
    try:
        temperature = float(payload.get("temperature", 0.7))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`temperature` must be a number.")
    if not 0 <= temperature <= 2:
        raise runtime.InvalidRequest("`temperature` must be between 0 and 2.")
    requested_provider = payload.get("provider")
    if requested_provider is not None and not isinstance(requested_provider, str):
        raise runtime.InvalidRequest("`provider`, when given, must be a string.")
    requested_model = payload.get("model")
    if requested_model is not None and not isinstance(requested_model, str):
        raise runtime.InvalidRequest("`model`, when given, must be a string.")

    matching = [p for p in completion.PROVIDERS if p.matches(requested_provider, requested_model)]
    if not matching:
        if requested_provider is not None and requested_model is not None:
            detail = f"provider={requested_provider!r} does not offer model={requested_model!r}"
        elif requested_model is not None:
            detail = (f"model={requested_model!r} is not offered by any configured provider "
                      f"(known: {sorted(m for p in completion.PROVIDERS for m in p.models)})")
        else:
            detail = f"provider must be one of {sorted({p.provider_name for p in completion.PROVIDERS})}"
        raise runtime.InvalidRequest(f"`provider`/`model`: {detail}.")

    async def call(provider):
        return await provider.generate(prompt, system, max_tokens, temperature, model=requested_model)

    value = await ctx.run("generate", matching, call, per_attempt_seconds=90)
    return {
        "text": value["text"], "model": value["model"], "provider": value["provider"],
        "finish_reason": value.get("finish_reason"),
        "usage": {"input_tokens": value["prompt_tokens"],
                  "output_tokens": value["output_tokens"]},
    }


SKILLS = {"llm.analyze": analyze, "llm.extract": extract_structured, "llm.generate": generate}

