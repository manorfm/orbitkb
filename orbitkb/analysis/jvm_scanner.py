"""Regex + brace-counting Kotlin/Java class and function boundary detection,
replacing tree-sitter-kotlin/-java. That native parser has a proven, reproducible
memory-corruption bug (SIGSEGV/SIGBUS, and even indefinite hangs -- see
orbitkb/discovery/isolation.py) on real Kotlin/Spring services; no amount of
subprocess isolation actually eliminates it, only contains the damage. Like every
other stack's call resolution (scan_helpers.resolve_local_calls), this trades exact
AST precision for a heuristic that can never crash. The project's README already
lists a full AST/LSP code graph as a deliberate non-goal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CLASS_KEYWORD_RE = re.compile(r"\bclass\s+(?P<name>[A-Za-z_]\w*)")
_ANNOTATION_LINE_RE = re.compile(r"^[ \t]*(?:@[\w.]+(?:\([^\n]*\))?[ \t]*)+$")
# `override`/`suspend` are function/property modifiers, not class ones, but a class
# declaration never has them right before it either way -- one shared list works for
# both `find_classes`'s and `find_functions`'s use of `_preceding_annotations_and_modifiers`.
_MODIFIER_LINE_RE = re.compile(
    r"^[ \t]*(?:public|private|protected|internal|open|abstract|final|sealed|data|inner"
    r"|annotation|static|override|suspend)[ \t]*$"
)


@dataclass(frozen=True)
class ClassMatch:
    name: str
    annotations: str
    header: str
    body_start: int
    body_end: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class FunctionMatch:
    name: str
    modifiers: str
    text: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    # Offset into `.text` where the body/expression starts -- i.e. where call-site
    # scanning (`find_calls`) should start, so a call-shaped fragment inside the
    # signature itself (`fun reserve(...)` matching its own name before `(`) is
    # never mistaken for the function calling itself.
    body_offset: int


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _skip_string(text: str, i: int) -> int:
    n = len(text)
    if text[i : i + 3] == '"""':
        end = text.find('"""', i + 3)
        return end + 3 if end != -1 else n
    i += 1
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return n


def _skip_char_literal(text: str, i: int) -> int:
    n = len(text)
    i += 1
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "'":
            return i + 1
        i += 1
    return n


def find_matching_brace(text: str, open_index: int) -> int:
    """Given the index of an opening `{` in `text`, return the index of its matching
    `}` -- skipping braces inside string/char literals and comments. Returns
    `len(text) - 1` if unmatched (malformed/truncated source): callers treat that as
    "to end of text" rather than raising, since a heuristic scanner over real-world
    source must degrade gracefully, not blow up on the one file it can't fully follow.
    """
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "'":
            i = _skip_char_literal(text, i)
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl if nl != -1 else n
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def _find_header_end(text: str, start: int) -> int:
    """From `start`, scan forward for the `{` that opens a class/function body,
    skipping over any `(...)`/`[...]` along the way (parameter lists, superclass
    constructor calls, annotation arguments, array types). Returns -1 if a top-level
    `;` or `=` is hit first (an abstract/interface member, or a Kotlin expression-body
    function), or a top-level newline is hit with neither `{` nor a supertype `:`
    clause following it -- a brace-less Kotlin class (primary-constructor-only, e.g.
    `class Foo(val x: String)`) declared with more code after it, where a bare
    bracket-depth scan would otherwise run on into that next declaration and mistake
    ITS `{` for this one's body -- or the text ends first.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "'":
            i = _skip_char_literal(text, i)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch == "{":
            return i
        elif depth == 0 and ch in ";=":
            return -1
        elif depth == 0 and ch == "\n":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] not in "{:":
                return -1
        i += 1
    return -1


_INLINE_PREFIX_TOKEN = (
    r"@[\w.]+(?:\([^\n]*?\))?|public|private|protected|internal|open|abstract"
    r"|final|sealed|data|inner|annotation|static|override|suspend"
)
_INLINE_PREFIX_RE = re.compile(rf"(?:{_INLINE_PREFIX_TOKEN})(?:\s+(?:{_INLINE_PREFIX_TOKEN}))*")


def _preceding_annotations_and_modifiers(text: str, keyword_start: int) -> str:
    """Annotations/modifiers immediately before `keyword_start` -- on the same line
    (`@Primary class Foo`) and/or on preceding lines of their own -- the same
    evidence tree-sitter's `modifiers` node gave.
    """
    line_start = text.rfind("\n", 0, keyword_start) + 1
    lines: list[str] = []
    same_line_prefix = text[line_start:keyword_start].strip()
    if same_line_prefix and _INLINE_PREFIX_RE.fullmatch(same_line_prefix):
        lines.append(same_line_prefix)
    cursor = line_start
    while True:
        prev_end = cursor - 1
        if prev_end < 0:
            break
        prev_start = text.rfind("\n", 0, prev_end) + 1
        line = text[prev_start:prev_end]
        if _ANNOTATION_LINE_RE.match(line) or _MODIFIER_LINE_RE.match(line):
            lines.append(line.strip())
            cursor = prev_start
            continue
        break
    return "\n".join(reversed(lines))


def _find_declaration_end_without_body(text: str, start: int) -> int:
    """For a class with no `{...}` at all -- Kotlin allows a class with only a
    primary constructor and nothing else, e.g. a `@ConfigurationProperties` holder
    (`class Foo(val x: String)`, no body needed) -- the position where the
    declaration ends: a top-level `;` or newline, outside any `(...)`/`[...]`.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "'":
            i = _skip_char_literal(text, i)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in ";\n":
            return i
        i += 1
    return n


def find_classes(source: str) -> list[ClassMatch]:
    """Every top-level or nested `class`/`interface`/`object` declaration, in source
    order. Nested classes are included flat (matching tree-sitter's `_walk()` over
    the whole file), since callers already key everything off `f"{class_name}.{member}"`.
    """
    classes: list[ClassMatch] = []
    for match in _CLASS_KEYWORD_RE.finditer(source):
        name = match.group("name")
        annotations = _preceding_annotations_and_modifiers(source, match.start())
        header_end = _find_header_end(source, match.end())
        if header_end == -1:
            # No `{...}` body at all (Kotlin: primary-constructor-only declaration).
            # body_end < body_start is a deliberately empty range: find_functions()
            # over it naturally returns nothing, since there is no body to hold any.
            decl_end = _find_declaration_end_without_body(source, match.end())
            classes.append(ClassMatch(
                name=name,
                annotations=annotations,
                header=source[match.end() : decl_end],
                body_start=decl_end,
                body_end=decl_end - 1,
                start_line=_line_of(source, match.start()),
                end_line=_line_of(source, decl_end),
            ))
            continue
        body_end = find_matching_brace(source, header_end)
        header = source[match.end() : header_end]
        classes.append(ClassMatch(
            name=name,
            annotations=annotations,
            header=header,
            body_start=header_end,
            body_end=body_end,
            start_line=_line_of(source, match.start()),
            end_line=_line_of(source, body_end),
        ))
    return classes


_KOTLIN_FUNCTION_RE = re.compile(r"\bfun\s+(?:[A-Za-z_][\w.<>]*\.)?(?P<name>[A-Za-z_]\w*)\s*\(")
_JAVA_METHOD_RE = re.compile(
    r"(?P<returns>[A-Za-z_][\w<>\[\],.?]*(?:\s+[A-Za-z_][\w<>\[\],.?]*)*)"
    r"\s+(?P<name>[A-Za-z_]\w*)\s*\((?![^)]*\bclass\b)"
)
_JAVA_MODIFIER_KEYWORDS_RE = re.compile(
    r"\b(?:public|private|protected|static|final|synchronized|abstract|native|strictfp|default)\b"
)


def find_functions(text: str, search_start: int, search_end: int, kotlin: bool) -> list[FunctionMatch]:
    """Every function/method declared directly in `text[search_start:search_end]`
    (typically one class's body) -- flat, not recursing into nested classes' own
    members a second time (callers walk nested classes separately via `find_classes`).
    """
    functions: list[FunctionMatch] = []
    pattern = _KOTLIN_FUNCTION_RE if kotlin else _JAVA_METHOD_RE
    pos = search_start
    while pos < search_end:
        match = pattern.search(text, pos, search_end)
        if match is None:
            break
        if not kotlin and match.start() > 0 and text[match.start() - 1] == "@":
            # The "returns" group started right after '@' -- it actually matched an
            # annotation's own name (`@Transactional` -> "Transactional" looks like a
            # bare type to this regex), not a real return type. Skip past the whole
            # annotation identifier (not just one character) before retrying.
            end_of_word = match.start()
            while end_of_word < len(text) and (text[end_of_word].isalnum() or text[end_of_word] == "_"):
                end_of_word += 1
            pos = end_of_word
            continue
        name = match.group("name")
        if not kotlin and not _JAVA_MODIFIER_KEYWORDS_RE.sub("", match.group("returns")).strip():
            # No real return type after stripping modifiers -- this is a constructor
            # (same name as the class), not a method; tree-sitter's method_declaration
            # never matched constructor_declaration either.
            pos = match.end()
            continue
        paren_close = find_matching_paren(text, match.end() - 1)
        if paren_close == -1:
            pos = match.end()
            continue
        after_params = paren_close + 1
        # Skip a Kotlin return-type annotation (": Type") before the body/expression.
        cursor = after_params
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
        if kotlin and cursor < len(text) and text[cursor] == ":":
            cursor += 1
            depth = 0
            while cursor < len(text):
                ch = text[cursor]
                if ch in "<(":
                    depth += 1
                elif ch in ">)":
                    depth = max(0, depth - 1)
                elif depth == 0 and ch in "{=":
                    break
                cursor += 1
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor < len(text) and text[cursor] == "{":
            end = find_matching_brace(text, cursor)
            func_text = text[match.start() : end + 1]
            pos = end + 1
            body_offset = cursor - match.start()
        elif kotlin and cursor < len(text) and text[cursor] == "=":
            # Expression-body function: ends at the next top-level statement
            # terminator -- a newline outside any bracket/string, matching how a
            # one-liner `fun foo() = bar()` is written in practice.
            end = _end_of_expression_body(text, cursor + 1, search_end)
            func_text = text[match.start() : end]
            pos = end
            body_offset = (cursor + 1) - match.start()
        else:
            # Abstract/interface declaration with no body at all.
            end = text.find("\n", after_params)
            end = end if end != -1 else search_end
            func_text = text[match.start() : end]
            pos = end
            body_offset = len(func_text)
        modifiers = _preceding_annotations_and_modifiers(text, match.start())
        functions.append(FunctionMatch(
            name=name,
            modifiers=modifiers,
            text=func_text,
            start_offset=match.start(),
            end_offset=pos,
            start_line=_line_of(text, match.start()),
            end_line=_line_of(text, max(match.start(), pos - 1)),
            body_offset=body_offset,
        ))
    return functions


def find_matching_paren(text: str, open_index: int) -> int:
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "'":
            i = _skip_char_literal(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _end_of_expression_body(text: str, start: int, limit: int) -> int:
    """Where a Kotlin expression-body function (`fun foo() = ...`) ends: the next
    top-level newline, UNLESS the following line continues the same fluent chain
    (starts with `.` or `?.`) -- a `WebClient`-style multi-line chain
    (`= client.get()\\n    .retrieve()\\n    .bodyToMono(...)`) is one expression,
    not one statement per line -- or the expression simply hasn't started yet (the
    common `fun foo(): T =\\n    firstRealToken...` style, `=` followed immediately
    by a newline with no content of its own): a newline with nothing but whitespace
    before it since `start` is leading whitespace, not the expression's end.
    """
    depth = 0
    i = start
    seen_content = False
    while i < limit:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            seen_content = True
            continue
        if ch == "'":
            i = _skip_char_literal(text, i)
            seen_content = True
            continue
        if ch in "([{":
            depth += 1
            seen_content = True
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
            seen_content = True
        elif ch == "\n" and depth == 0:
            j = i + 1
            while j < limit and text[j] in " \t\r\n":
                j += 1
            if j < limit and (not seen_content or text[j] == "." or text[j : j + 2] == "?."):
                i = j
                continue
            return i
        elif ch not in " \t\r":
            seen_content = True
        i += 1
    return limit


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split `text` on `sep`, ignoring separators nested inside `()`/`[]`/`<>` or
    string/char literals -- e.g. a Kotlin primary constructor's parameter list, where
    a generic type argument's own comma (`Map<String, Int>`) must not split the
    parameter.
    """
    if not text.strip():
        return []
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "'":
            i = _skip_char_literal(text, i)
            continue
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth = max(0, depth - 1)
        elif depth == 0 and text[i : i + len(sep)] == sep:
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


_KEYWORDS_NOT_CALLS = frozenset({
    "if", "when", "while", "for", "catch", "switch", "synchronized", "return",
    "fun", "class", "interface", "object", "constructor", "super", "this",
    "throw", "try", "finally", "else", "do", "val", "var", "in", "is", "as",
})
_CALL_RE = re.compile(r"\b((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)\s*\(")


def mask_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    """Blank out each `(start, end)` span in `text` with spaces (keeping newlines, so
    line numbers computed against the result still line up), so a field-declaration
    regex scanning a class body doesn't match a look-alike local variable declaration
    inside a method body -- `ranges` is typically `find_functions()`'s spans.
    """
    chars = list(text)
    for start, end in ranges:
        for i in range(max(start, 0), min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def find_calls(text: str) -> list[tuple[str, int]]:
    """(callee_text, offset) for each apparent call site in `text` -- a dotted
    identifier chain immediately followed by `(`, skipping control-flow keywords and
    object construction (`new Foo(...)`, which is instantiation, not a call our edge
    model tracks).
    """
    calls: list[tuple[str, int]] = []
    for match in _CALL_RE.finditer(text):
        callee = match.group(1)
        if callee.rsplit(".", 1)[-1] in _KEYWORDS_NOT_CALLS:
            continue
        if text[: match.start()].rstrip().endswith("new"):
            continue
        calls.append((callee, match.start()))
    return calls
