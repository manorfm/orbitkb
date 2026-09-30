"""Configuration key rules shared by source analyzers."""

import re

PROPERTY_CONFIGURATION_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
SENSITIVE_CONFIGURATION_KEY = re.compile(
    r"(?:password|secret|token|api[_-]?key|credential|private[_-]?key)", re.IGNORECASE,
)
