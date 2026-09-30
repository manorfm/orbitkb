"""Opaque fingerprint of the exact input used to generate a component."""

import hashlib
import json


def component_input_digest(cache_identity: str | None, prompt: str, schema: dict) -> str | None:
    if not cache_identity:
        return None
    payload = json.dumps(
        ["component:v1", cache_identity, prompt, schema],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
