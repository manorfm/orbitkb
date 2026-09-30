from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from orbitkb.generation.backend_base import GenerationError, GenerationOutcome, LLMUsage

TIMEOUT_SECONDS = 180


class CodexBackend:
    """Headless Codex CLI backend.

    Uses the default ChatGPT OAuth session (`codex login`) by default, so generation
    counts against the user's Codex subscription rather than metered API billing.
    Pass api_key=True to opt into CODEX_API_KEY billing instead.
    """

    name = "codex"

    def __init__(self, model: str | None = None, api_key: bool = False):
        self.model = model
        self.api_key = api_key

    @property
    def cache_identity(self) -> str | None:
        return f"codex:{self.model}" if self.model else None

    def generate(self, prompt: str, schema: dict, cwd: Path) -> GenerationOutcome:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            output_path = Path(tmp) / "output.txt"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            cmd = [
                "codex", "exec",
                "--json",
                "--output-schema", str(schema_path),
                "-o", str(output_path),
                "-s", "read-only",
                "-C", str(cwd),
                "--skip-git-repo-check",
            ]
            if self.model:
                cmd += ["-m", self.model]
            cmd.append(prompt)

            env = None
            if not self.api_key:
                import os
                env = {k: v for k, v in os.environ.items() if k != "CODEX_API_KEY"}

            try:
                result = subprocess.run(
                    cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                    stdin=subprocess.DEVNULL, env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise GenerationError(f"codex timed out after {TIMEOUT_SECONDS}s") from exc

            if result.returncode != 0:
                raise GenerationError(f"codex exited {result.returncode}: {result.stderr[-2000:]}")

            if not output_path.exists():
                raise GenerationError(f"codex produced no output file. stdout: {result.stdout[-2000:]}")

            raw = output_path.read_text(encoding="utf-8").strip()
            try:
                structured = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GenerationError(f"codex final message was not valid JSON: {exc}\n{raw[-2000:]}") from exc
            return GenerationOutcome(structured=structured, usage=self._extract_usage(result.stdout))

    @staticmethod
    def _extract_usage(stdout: str) -> LLMUsage:
        """Best-effort only: `codex exec --json` streams one JSON event per line to
        stdout; this looks for the last line carrying a top-level `usage` object and
        never raises when none is found or it's shaped differently than expected —
        the same never-verified-against-a-real-payload caveat as ClaudeBackend's."""
        usage = LLMUsage()
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            candidate = event.get("usage")
            if not isinstance(candidate, dict):
                continue
            cost = event.get("total_cost_usd")
            usage = LLMUsage(
                input_tokens=candidate.get("input_tokens"),
                output_tokens=candidate.get("output_tokens"),
                cached_input_tokens=candidate.get("cached_input_tokens"),
                cost_usd=cost if isinstance(cost, (int, float)) else None,
            )
        return usage
