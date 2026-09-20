"""Code execution via Gemini's own hosted code-execution tool on Vertex.

CONNECTED, NOT REBUILT. This deliberately does NOT run customer code in a
sandbox this service operates -- it asks Gemini's code-execution tool to run
it in Google's own sandbox and reports the result. No process isolation, no
seccomp policy, no filesystem to secure: there is none of that here, because
none of that is ours to run.
"""

import os

import httpx

from .. import runtime
from . import google_auth
from .gemini import _cost_micros, model_url, output_tokens_of

# Auto-updating alias rather than a pinned version -- see search_grounding.py.
_MODEL = os.environ.get("WORKER_CODE_EXEC_MODEL", "gemini-flash-latest")
_TIMEOUT = float(os.environ.get("WORKER_CODE_EXEC_TIMEOUT_SECONDS", "60"))


class _GeminiCodeExecution:
    id = f"vertex-code-exec:{_MODEL}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def run(self, code: str) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        project = google_auth.project()
        url = model_url(project, _MODEL, "generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": (
                "Run exactly this Python code using the code execution tool and report "
                f"its output. Do not modify it.\n\n```python\n{code}\n```")}]}],
            "tools": [{"code_execution": {}}],
            "generationConfig": {"temperature": 0.0},
        }
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Vertex code execution timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Vertex code execution unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Vertex code execution returned {response.status_code}",
                reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Vertex code execution rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise runtime.InvalidProviderResponse("Code execution returned no candidate.")
        parts = (candidates[0].get("content") or {}).get("parts") or []

        # One response can carry SEVERAL code/result pairs -- the model may run
        # code, read the output, then run more. Assigning instead of collecting
        # kept only the last pair, so the caller paid for the whole chain and
        # was shown the tail of it.
        code_blocks, output_blocks, outcome, summary_bits = [], [], None, []
        for part in parts:
            if "executableCode" in part:
                block = (part["executableCode"] or {}).get("code")
                if block:
                    code_blocks.append(block)
            elif "codeExecutionResult" in part:
                result = part["codeExecutionResult"] or {}
                # Last non-OK outcome wins: if any step failed, say so.
                step_outcome = result.get("outcome")
                if step_outcome and (outcome is None or step_outcome != "OUTCOME_OK"):
                    outcome = step_outcome
                if result.get("output"):
                    output_blocks.append(result["output"])
            elif part.get("text"):
                summary_bits.append(part["text"])

        ran_code = "\n\n".join(code_blocks) if code_blocks else None
        output = "\n".join(output_blocks) if output_blocks else None

        if ran_code is None and output is None:
            raise runtime.InvalidProviderResponse(
                "The model answered without running any code -- try a more explicit "
                "instruction to execute it.")

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = output_tokens_of(usage)
        cost, measured = _cost_micros(prompt_tokens, output_tokens, _MODEL)

        return runtime.ProviderResult(
            value={"code": ran_code or code, "output": output or "",
                   "outcome": outcome or "UNKNOWN",
                   "summary": "".join(summary_bits).strip() or None, "model": _MODEL},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={prompt_tokens} out={output_tokens}")


PROVIDERS = [_GeminiCodeExecution()]
