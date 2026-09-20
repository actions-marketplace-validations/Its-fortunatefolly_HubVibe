"""Run code and get the result back, via Gemini's own hosted sandbox."""

from .. import runtime
from ..providers import code_exec

MAX_CODE_CHARS = 8_000


async def execute(ctx, payload: dict) -> dict:
    code = payload.get("code")
    if not code or not isinstance(code, str) or not code.strip():
        raise runtime.InvalidRequest("`code` is required.")
    if len(code) > MAX_CODE_CHARS:
        raise runtime.InvalidRequest(
            f"`code` is {len(code)} characters, over the {MAX_CODE_CHARS} limit.")

    async def call(provider):
        return await provider.run(code)

    value = await ctx.run("execute", code_exec.PROVIDERS, call, per_attempt_seconds=75)
    return {
        "code": value["code"],
        "output": value["output"],
        "outcome": value["outcome"],
        "summary": value.get("summary"),
        "model": value["model"],
    }


SKILLS = {"code.execute": execute}
