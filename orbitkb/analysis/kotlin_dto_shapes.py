"""Fields declared by Kotlin data-class primary constructors."""

from __future__ import annotations

import re

from orbitkb.analysis.jvm_scanner import (
    find_classes,
    find_matching_paren,
    split_top_level,
)

_PROPERTY = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*:\s*(.+)", re.DOTALL)
_VALIDATION = re.compile(r"@(?:field:|get:)?(NotNull|NotBlank|NotEmpty|Positive|Negative|Size|Pattern)\b")


def kotlin_data_class_shapes(source: str) -> dict[str, list[dict]]:
    shapes: dict[str, list[dict]] = {}
    for declaration in find_classes(source):
        if "data" not in declaration.annotations.split():
            continue
        opening = declaration.header.find("(")
        closing = find_matching_paren(declaration.header, opening) if opening >= 0 else -1
        if closing < 0:
            continue
        fields = []
        for parameter in split_top_level(declaration.header[opening + 1:closing]):
            property_match = _PROPERTY.search(parameter)
            if property_match is None:
                continue
            name, type_and_default = property_match.groups()
            parts = split_top_level(type_and_default, "=")
            type_name = parts[0].strip()
            if not type_name:
                continue
            fields.append({
                "name": name,
                "type": type_name.removesuffix("?"),
                "required": not type_name.endswith("?") and len(parts) == 1,
                "validations": _VALIDATION.findall(parameter),
            })
        if fields:
            shapes[declaration.name] = fields
    return shapes
