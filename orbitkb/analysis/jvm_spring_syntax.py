"""Spring syntax shared by source parsing and cross-file recognizers."""

from __future__ import annotations

import re
from collections.abc import Iterator

from orbitkb.analysis.configuration_syntax import PROPERTY_CONFIGURATION_KEY
from orbitkb.analysis.jvm_scanner import split_top_level
from orbitkb.discovery.scan_helpers import find_matching_paren

SPRING_ROUTE_ANNOTATION_TO_METHOD: dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
}

_LITERAL_ROUTE_PATH = re.compile(r'"([^"\n]*)"')


def single_literal_route_path(value: str) -> str | None:
    """Read one literal path from a scalar or one-element Java/Kotlin array."""
    value = value.strip()
    if value.startswith(("[", "{")) and value.endswith(("]", "}")):
        if (value[0], value[-1]) not in {("[", "]"), ("{", "}")}:
            return None
        elements = split_top_level(value[1:-1])
        if len(elements) != 1:
            return None
        value = elements[0].strip()
    literal = _LITERAL_ROUTE_PATH.fullmatch(value)
    if literal is None or "${" in literal.group(1) or "#{" in literal.group(1):
        return None
    return literal.group(1)


def spring_annotation_calls(modifiers: str) -> Iterator[tuple[str, str]]:
    """Yield annotation calls, skipping `@` text inside another annotation's arguments."""
    cursor = 0
    while cursor < len(modifiers):
        annotation = re.search(r"@[A-Za-z_]\w*", modifiers[cursor:])
        if annotation is None:
            return
        annotation_name = annotation.group()[1:]
        after_name = cursor + annotation.end()
        argument_start = after_name
        while argument_start < len(modifiers) and modifiers[argument_start] in " \t":
            argument_start += 1
        if argument_start >= len(modifiers) or modifiers[argument_start] != "(":
            yield annotation_name, ""
            cursor = after_name
            continue
        argument_end = find_matching_paren(modifiers, argument_start)
        if argument_end == -1:
            return
        yield annotation_name, modifiers[argument_start : argument_end + 1]
        cursor = argument_end + 1


def spring_route_prefix(annotations: str) -> tuple[str | None, bool]:
    """Return a literal class/interface prefix and whether a declared path is unresolved."""
    for annotation_name, arguments in spring_annotation_calls(annotations):
        if annotation_name == "RequestMapping":
            paths = []
            for index, part in enumerate(split_top_level(arguments[1:-1])):
                key, separator, value = part.partition("=")
                if separator:
                    if key.strip() not in {"value", "path"}:
                        continue
                elif index == 0:
                    value = key
                else:
                    continue
                path = single_literal_route_path(value)
                if path is None:
                    return None, True
                paths.append(path)
            if paths:
                return (paths[0], False) if len(set(paths)) == 1 else (None, True)
    return None, False


def kotlin_supertypes(class_text: str) -> tuple[str, ...]:
    """Read the supertype list after the class header's constructor."""
    constructor_depth = 0
    for index, char in enumerate(class_text):
        if char == "(":
            constructor_depth += 1
        elif char == ")":
            constructor_depth -= 1
        elif char == ":" and constructor_depth == 0:
            supertypes = class_text[index + 1 :]
            return tuple(
                match.group(1)
                for item in supertypes.split(",")
                if (match := re.match(r"\s*([\w.]+)", item))
            )
    return ()


def spring_placeholder_literal(group_name: str) -> str:
    """One whole-string `${key}` or `${key:default}` placeholder.

    Accept Java's plain string, Kotlin's backslash escape, and Kotlin's
    multi-dollar string. Composed values do not prove a single configuration key.
    """
    return rf'\$*"\\?\$\{{(?P<{group_name}>{PROPERTY_CONFIGURATION_KEY.pattern})(?::[^{{}}"]*)?\}}"'
