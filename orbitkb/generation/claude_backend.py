from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orbitkb.generation.backend_base import GenerationError, GenerationOutcome, LLMUsage

TIMEOUT_SECONDS = 180

# A headless batch job calls `generate()` once per unit across a whole service
# (an endpoint, a component, a persistence summary, ...), so leaving this unset
# would fall back to whatever model the `claude` CLI's own ambient config
# defaults to -- Opus, on an account configured that way -- multiplying an
# expensive-per-call model across every unit instead of the cheaper one this
# structured-extraction task actually needs.
DEFAULT_MODEL = "sonnet"


class ClaudeBackend:
    """Headless Claude CLI CLI backend.

    Uses the normal OAuth/subscription session by default (no --bare, no API key),
    so generation cost counts against the user's existing Claude CLI plan rather
    than metered API billing. Pass bare=True to opt into ANTHROPIC_API_KEY billing
    instead (useful for CI where no interactive login is available).
    """

    name = "claude"

    def __init__(self, model: str | None = None, bare: bool = False):
        self.model = model or DEFAULT_MODEL
        self.bare = bare

    def generate(self, prompt: str, schema: dict, cwd: Path) -> GenerationOutcome:
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--allowedTools", "",
            "--permission-mode", "dontAsk",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.bare:
            cmd.append("--bare")

        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise GenerationError(f"claude timed out after {TIMEOUT_SECONDS}s") from exc

        if result.returncode != 0:
            raise GenerationError(f"claude exited {result.returncode}: {result.stderr[-2000:]}")

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GenerationError(f"claude returned non-JSON stdout: {exc}\n{result.stdout[-2000:]}") from exc

        if payload.get("is_error"):
            raise GenerationError(f"claude reported an error: {payload.get('result')}")

        structured = payload.get("structured_output")
        if structured is None:
            raise GenerationError(f"claude response had no structured_output: {result.stdout[-2000:]}")
        return GenerationOutcome(structured=structured, usage=self._extract_usage(payload))

    @staticmethod
    def _extract_usage(payload: dict) -> LLMUsage:
        """Best-effort only: besides is_error/result/structured_output, no other key
        of `claude -p --output-format json`'s payload has ever been verified against
        a real invocation in this codebase. Every field stays None instead of raising
        when it's absent or shaped differently than expected here."""
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        cost = payload.get("total_cost_usd")
        return LLMUsage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cache_read_input_tokens"),
            cost_usd=cost if isinstance(cost, (int, float)) else None,
        )
