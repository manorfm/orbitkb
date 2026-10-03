"""Regex-based Spring/Kotlin/Java static analysis, replacing tree-sitter-kotlin/-java.

That native parser had a proven, reproducible memory-corruption bug (SIGSEGV/SIGBUS,
and even indefinite hangs) on real Kotlin/Spring services -- subprocess isolation
could only contain the damage, not eliminate it. This module and `jvm_scanner.py`
remove the native parser
from the JVM analysis path entirely: `jvm_scanner.py` locates class/function
boundaries by regex + brace-counting (no tree, no native code, so nothing left to
corrupt), and this module holds the Spring-specific domain logic built on top of it
-- dependency injection, routes, message contracts, error contracts -- reusing the
already-text-based Spring helpers `engine.py` had before this rewrite (they only ever
read a `Node`'s declaration text and its `start_point.row`, never walked its tree).

Kept in its own module, mirroring `orbitkb/discovery/jvm_stack.py` and
`orbitkb/discovery/jvm_ast.py`: JVM/Kotlin-specific concerns get their own file
rather than growing `engine.py`, which stays the cross-stack facade.
"""
from __future__ import annotations

import re
import types
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from orbitkb.analysis import engine
from orbitkb.analysis.cloud_detection import (
    cloud_edge_kind_and_fact,
    jvm_client_declarations,
)
from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import (
    ClassMatch,
    FunctionMatch,
    find_calls,
    find_classes,
    find_functions,
    find_matching_brace,
    find_matching_paren,
    mask_non_code,
    mask_ranges,
    split_top_level,
)
from orbitkb.analysis.jvm_security_analyzer import method_security_requirement
from orbitkb.analysis.jvm_spring_syntax import (
    SPRING_ROUTE_ANNOTATION_TO_METHOD,
    kotlin_supertypes,
    spring_annotation_calls,
    spring_route_prefix,
)
from orbitkb.analysis.models import (
    AnalysisResult,
    ApiHeader,
    CloudFact,
    EntryPoint,
    Evidence,
    FlowBoundary,
    FlowEdge,
    Injection,
    Symbol,
)
from orbitkb.analysis.route_paths import join_route

_REQUEST_HEADER_RE = re.compile(r'@RequestHeader\s*\(\s*(?:(?:name|value)\s*=\s*)?"(?P<name>[^"]+)"')
_RESPONSE_HEADER_CALL_RE = re.compile(r'\.header\s*\(\s*"(?P<name>[^"]+)"')
_SCHEDULED_CRON_RE = re.compile(r'^\(\s*cron\s*=\s*"([^"\n]+)"')
_DIRECT_LOCAL_CALL_RE = re.compile(
    r"(?m)^[ \t]*val[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*"
    r"(?P<callee>[A-Za-z_]\w*\.[A-Za-z_]\w*)[ \t]*\("
)
_MONGO_EXECUTE_RE = re.compile(r'(?<![\w.])(?P<template>[A-Za-z_]\w*)\.execute\s*\(')
_MONGO_CALLBACK_PARAMETER_RE = re.compile(r'\s*(?P<collection>[A-Za-z_]\w*)\s*->')

# `ResponseEntity.BodyBuilder`'s own named header setters -- a fixed, well-known
# Spring API surface, not a guess: calling `.eTag(...)` always sets the `ETag`
# header, regardless of the (often computed) argument, same as the generic
# `.header("X", ...)` case above already only records the literal name.
_NAMED_RESPONSE_HEADER_BUILDERS = {
    "eTag": "ETag",
    "cacheControl": "Cache-Control",
    "lastModified": "Last-Modified",
    "location": "Location",
    "contentType": "Content-Type",
}


def _add_scheduled_job(
    result: AnalysisResult, annotations: tuple[tuple[str, str], ...], symbol: str, name: str, evidence: Evidence,
) -> None:
    for annotation_name, arguments in annotations:
        if annotation_name != "Scheduled":
            continue
        match = _SCHEDULED_CRON_RE.match(arguments)
        if match is None or "${" in match.group(1) or "#{" in match.group(1):
            continue
        result.entrypoints.append(EntryPoint("job", "SCHEDULED", name, symbol, evidence))
        result.contracts[symbol] = {
            "schedule": match.group(1), "concurrency": "unknown", "idempotency": "unknown",
        }
        return


def _listener_channel(arguments: str, key: str, kotlin: bool) -> str | None:
    parts = split_top_level(arguments[1:-1])
    for index, part in enumerate(parts):
        name, separator, value = part.partition("=")
        if separator and name.strip() != key:
            continue
        if not separator:
            if kotlin or index != 0:
                continue
            value = name
        value = value.strip()
        if value.startswith("[") and value.endswith("]") and kotlin:
            values = split_top_level(value[1:-1])
        elif value.startswith("{") and value.endswith("}") and not kotlin:
            values = split_top_level(value[1:-1])
        else:
            values = [value]
        literals = [re.fullmatch(r'"([^"\n]+)"', item.strip()) for item in values]
        if literals and all(literals):
            channels = [literal.group(1) for literal in literals if literal]
            if all("${" not in channel and "#{" not in channel for channel in channels):
                return channels[0]
    return None


def _add_message_listeners(
    result: AnalysisResult, annotations: tuple[tuple[str, str], ...],
    symbol: str, function: FunctionMatch, evidence: Evidence, *, kotlin: bool,
) -> None:
    language = "kotlin" if kotlin else "java"
    for annotation_name, arguments in annotations:
        if annotation_name == "RabbitListener":
            channel = _listener_channel(arguments, "queues", kotlin)
            transport = "rabbitmq"
        elif annotation_name == "KafkaListener":
            channel = _listener_channel(arguments, "topics", kotlin)
            transport = "kafka"
        else:
            continue
        if channel is None:
            continue
        result.entrypoints.append(EntryPoint("message", "CONSUME", channel, symbol, evidence))
        result.contracts[symbol] = engine._message_contract(channel, function.text, language, transport=transport)


def _endpoint_headers(method: str, path: str, function_match: FunctionMatch, evidence: Evidence) -> list[ApiHeader]:
    """Header names one HTTP endpoint reads (`@RequestHeader`, from its own
    signature) or writes (a `ResponseEntity` header builder call, from its
    body) -- names only, never values (see `ApiHeader`).
    """
    headers = [
        ApiHeader(method, path, "request", match.group("name"), evidence)
        for match in _REQUEST_HEADER_RE.finditer(function_match.text[: function_match.body_offset])
    ]
    body = function_match.text[function_match.body_offset :]
    seen: set[str] = set()
    for match in _RESPONSE_HEADER_CALL_RE.finditer(body):
        name = match.group("name")
        if name not in seen:
            seen.add(name)
            headers.append(ApiHeader(method, path, "response", name, evidence))
    for builder_method, header_name in _NAMED_RESPONSE_HEADER_BUILDERS.items():
        if header_name not in seen and re.search(rf"\.{builder_method}\s*\(", body):
            seen.add(header_name)
            headers.append(ApiHeader(method, path, "response", header_name, evidence))
    return headers


class _LineEvidence:
    """Adapter exposing just enough of a tree-sitter `Node`'s shape
    (`.start_point`/`.end_point`) for the shared Spring contract helpers in
    `engine.py` -- they only ever read `.start_point.row`, having already been
    text-based rather than tree-walking. There is no real Node to hand them here,
    since `jvm_scanner.py` locates declarations by regex + brace-counting, not by
    building a parse tree.
    """
    __slots__ = ("start_point", "end_point")

    def __init__(self, start_line: int, end_line: int | None = None) -> None:
        self.start_point = types.SimpleNamespace(row=start_line - 1)
        self.end_point = types.SimpleNamespace(row=(end_line if end_line is not None else start_line) - 1)


@dataclass(frozen=True)
class DeclaredMember:
    """A class-level property or constructor/field parameter: a name, an optional
    declared type, its raw source text, and the line it's evidenced on. The shape
    shared by a Kotlin primary-constructor parameter, a Java field, and a Java
    constructor parameter alike -- each is just a named, possibly-typed declaration
    site for dependency-injection and `@Value`/`@ConfigurationProperties` binding
    detection, so one rich type models all three instead of three parallel tuples.
    """
    name: str | None
    type_name: str | None
    raw_text: str
    line: int

    def evidence(self, path: Path, root: Path) -> Evidence:
        return Evidence(path.relative_to(root).as_posix(), self.line, self.line)


def _last_type_token(type_text: str) -> str:
    """The innermost/last type reference in a possibly-generic type expression
    (`Map<String, FooService>` -> `FooService`) -- tree-sitter's `user_type` nodes
    walked in document order and took the last one for exactly this reason.
    """
    tokens = re.findall(r"[A-Za-z_][\w.]*", type_text)
    return tokens[-1] if tokens else type_text.strip()


def _parse_kotlin_parameter(param_text: str) -> tuple[str | None, str | None]:
    """(name, type) for one Kotlin primary-constructor parameter's raw text (as split
    by `split_top_level`). `name` is None without a `val`/`var` -- not a class
    property, the same distinction the original tree-sitter-based text search
    (`re.search(r"(?:val|var)\\s+(\\w+)", ...)`) made on `class_parameter` text.
    """
    name_match = re.search(r"\b(?:val|var)\s+(\w+)", param_text)
    type_match = re.search(r":\s*([\w.<>\[\],?\s]+?)(?:\s*=|$)", param_text)
    name = name_match.group(1) if name_match else None
    type_name = _last_type_token(type_match.group(1)) if type_match else None
    return name, type_name


def _kotlin_primary_constructor_members(header: str, class_start_line: int) -> list[DeclaredMember]:
    """Every parameter in a Kotlin class's primary constructor, parsed out of
    `ClassMatch.header`. Evidence uses the class's own declaration line for every
    parameter rather than each parameter's own line -- a deliberate precision
    trade-off (see `jvm_scanner.py`'s module docstring), not a bug.
    """
    paren_open = header.find("(")
    if paren_open == -1:
        return []
    paren_close = find_matching_paren(header, paren_open)
    if paren_close == -1:
        return []
    members: list[DeclaredMember] = []
    for raw_param in split_top_level(header[paren_open + 1 : paren_close], ","):
        if not raw_param.strip():
            continue
        name, type_name = _parse_kotlin_parameter(raw_param)
        members.append(DeclaredMember(name, type_name, raw_param, class_start_line))
    return members


_JAVA_PRIMITIVE_TYPES = r"boolean|byte|char|short|int|long|float|double"
_JAVA_FIELD_RE = re.compile(
    r"(?m)^[ \t]*(?:@\w+(?:\([^\n]*?\))?[ \t\n]*)*"
    r"(?:(?:public|private|protected|static|final|transient|volatile)[ \t]+)*"
    rf"(?P<type>(?:{_JAVA_PRIMITIVE_TYPES})\b(?:\[\])?|[A-Z][\w.<>\[\],?]*(?:[ \t]+[A-Z][\w.<>\[\],?]*)*)"
    r"[ \t]+(?P<name>[a-z_]\w*)[ \t]*(?:=[^;]*)?;"
)


def _java_class_field_members(
    class_body_text: str, function_ranges: list[tuple[int, int]], class_start_line: int,
) -> list[DeclaredMember]:
    """Every `private final Foo bar;`-style field in a Java class body -- method
    bodies are masked out first so a local variable declaration that happens to look
    like a field is never mistaken for one.
    """
    masked = mask_ranges(class_body_text, function_ranges)
    members = []
    for match in _JAVA_FIELD_RE.finditer(masked):
        line = class_start_line + class_body_text.count("\n", 0, match.start())
        declared_type = match.group("type").split("<", 1)[0].rsplit(".", 1)[-1]
        members.append(DeclaredMember(match.group("name"), declared_type, match.group(0), line))
    return members


def _java_constructor_param_members(
    class_body_text: str, class_name: str, class_start_line: int,
) -> list[DeclaredMember]:
    """Every parameter of every constructor declared directly in a Java class body --
    tree-sitter's `constructor_declaration`/`formal_parameter` nodes, replaced with a
    name-anchored regex (a constructor is the one declaration whose name is the
    class's own name, followed by its body). No type is extracted: constructor
    parameters (unlike fields) are only ever used here for `@Value` binding detection,
    which never needs one.
    """
    members: list[DeclaredMember] = []
    pattern = re.compile(rf"(?<![\w.]){re.escape(class_name)}\s*\(")
    for match in pattern.finditer(class_body_text):
        paren_open = match.end() - 1
        paren_close = find_matching_paren(class_body_text, paren_open)
        if paren_close == -1:
            continue
        after = class_body_text[paren_close + 1 :].lstrip()
        if not (after.startswith("{") or after.startswith("throws")):
            continue  # a call (`super(...)`, `this(...)`), not a declaration
        line = class_start_line + class_body_text.count("\n", 0, match.start())
        for raw_param in split_top_level(class_body_text[paren_open + 1 : paren_close], ","):
            if not raw_param.strip():
                continue
            name_match = re.search(r"([A-Za-z_]\w*)\s*$", raw_param.strip())
            if name_match:
                members.append(DeclaredMember(name_match.group(1), None, raw_param, line))
    return members


def _classify_spring_edges(
    edges: list[FlowEdge], receivers: engine._SpringPersistenceReceivers,
    redis_publishers: frozenset[str], cloud_declarations: dict[str, tuple],
    mongo_callback_writes: set[tuple[int, str]],
) -> tuple[list[FlowEdge], list[CloudFact]]:
    classified = []
    cloud_facts: list[CloudFact] = []
    for edge in edges:
        cloud_kind, cloud_fact = cloud_edge_kind_and_fact(edge.target, edge.evidence, cloud_declarations)
        redis_receiver, separator, redis_method = edge.target.rpartition(".")
        redis_publish = bool(separator and redis_receiver in redis_publishers and redis_method == "convertAndSend")
        repository_kind = engine._spring_repository_call_kind(edge.target, receivers.repositories)
        template_kind = (
            engine._spring_jdbc_template_call_kind(edge.target, receivers.jdbc_templates)
            or engine._spring_mongo_template_call_kind(edge.target, receivers.mongo_templates)
            or engine._entity_manager_call_kind(edge.target, receivers.entity_managers)
        )
        callback_write = (edge.evidence.start_line, edge.target) in mongo_callback_writes
        kind = (
            repository_kind or template_kind or ("writes" if callback_write else None)
            or ("publishes" if redis_publish else None) or cloud_kind
        )
        # Generic name matching is disabled for JVM persistence: `repository.save`
        # is an operation only with a local repository dependency.
        if kind is None and edge.kind in {"reads", "writes"}:
            kind = "invokes"
        boundary_kind = (
            "redis_pubsub" if redis_publish else "persistence" if template_kind or callback_write
            else edge.boundary_kind
        )
        classified.append(FlowEdge(
            edge.source, edge.target, kind or edge.kind, edge.evidence, edge.confidence, edge.origin,
            boundary_kind=boundary_kind,
        ))
        if cloud_fact is not None:
            cloud_facts.append(cloud_fact)
    return classified, cloud_facts


def _mongo_execute_callback_writes(
    function: FunctionMatch, mongo_templates: frozenset[str], *, kotlin: bool,
) -> set[tuple[int, str]]:
    """Find collection.updateOne inside a proven MongoTemplate.execute callback."""
    if not mongo_templates:
        return set()
    parameter_names = engine._declared_parameter_types(
        function.text[:function.body_offset], kotlin=kotlin,
    )
    body = function.text[function.body_offset:]
    visible = mask_non_code(body)
    executions = []
    for execute in _MONGO_EXECUTE_RE.finditer(visible):
        name = execute.group("template")
        if name not in mongo_templates or name in parameter_names:
            continue
        if kotlin and re.search(rf"\b(?:val|var)\s+{re.escape(name)}\b", visible[:execute.start()]):
            continue
        executions.append(execute)
    if not executions:
        return set()
    call_counts: dict[tuple[int, str], int] = {}
    for callee, offset in find_calls(visible):
        line = function.start_line + function.text.count("\n", 0, function.body_offset + offset)
        key = (line, callee)
        call_counts[key] = call_counts.get(key, 0) + 1
    writes: set[tuple[int, str]] = set()
    for execute in executions:
        arguments_end = find_matching_paren(body, execute.end() - 1)
        if arguments_end < 0:
            continue
        cursor = arguments_end + 1
        while cursor < len(visible) and visible[cursor].isspace():
            cursor += 1
        if cursor >= len(visible) or visible[cursor] != "{":
            continue
        callback_end = find_matching_brace(body, cursor)
        if callback_end < 0:
            continue
        parameter = _MONGO_CALLBACK_PARAMETER_RE.match(visible, cursor + 1)
        if parameter is None:
            continue
        callback_start = parameter.end()
        target = f'{parameter.group("collection")}.updateOne'
        for callee, offset in find_calls(visible[callback_start:callback_end]):
            if callee != target:
                continue
            call_offset = function.body_offset + callback_start + offset
            line = function.start_line + function.text.count("\n", 0, call_offset)
            if call_counts.get((line, target)) == 1:
                writes.add((line, target))
    return writes


def _jvm_edges_for_text(
    symbol: str, function_match: FunctionMatch, path: Path, root: Path,
    local_classes: frozenset[str] = frozenset(),
) -> list[FlowEdge]:
    engine.logger.debug("  analyzing function: %s (%s)", symbol, path.name)
    edges = []
    # Scanning starts at body_offset, not 0: the signature itself can contain a
    # call-shaped fragment (`fun reserve(...)` matching find_calls' own pattern),
    # which tree-sitter never saw since it only ever walked the body Node.
    body_text = function_match.text[function_match.body_offset :]
    for callee, offset in find_calls(body_text):
        if callee in local_classes:
            continue
        line = function_match.start_line + function_match.text.count("\n", 0, function_match.body_offset + offset)
        edges.append(FlowEdge(symbol, callee, engine._call_kind(callee), Evidence(path.relative_to(root).as_posix(), line, line)))
    return edges


def _generated_data_class_copy_lines(function: FunctionMatch) -> set[int]:
    """Find direct, unshadowed calls to Kotlin's generated data-class copy."""
    if re.search(r"\bcopy\s*:", function.text[:function.body_offset]):
        return set()
    body = mask_non_code(function.text[function.body_offset:])
    calls = [(callee, offset) for callee, offset in find_calls(body)]
    lines = {
        offset: function.start_line + function.text.count("\n", 0, function.body_offset + offset)
        for callee, offset in calls if callee == "copy"
    }
    counts = Counter(lines.values())
    base_depth = 1 if body.lstrip().startswith("{") else 0
    generated = set()
    for offset, line in lines.items():
        prefix = body[:offset]
        if counts[line] != 1 or prefix.count("{") - prefix.count("}") != base_depth:
            continue
        if re.search(r"\b(?:val|var|fun)\s+copy\b", prefix):
            continue
        generated.add(line)
    return generated


def _direct_kotlin_local_assignments(function_match: FunctionMatch) -> tuple[tuple[str, str, int], ...]:
    body = function_match.text[function_match.body_offset:]
    assignments = []
    for match in _DIRECT_LOCAL_CALL_RE.finditer(body):
        closing = find_matching_paren(body, match.end() - 1)
        if closing < 0:
            continue
        tail = body[closing + 1:]
        if tail.split("\n", 1)[0].strip() not in {"", ";"}:
            continue
        if re.match(r"[^\S\n]*\n[ \t]*(?:\.|\?\.|!!)", tail):
            continue
        line = function_match.start_line + function_match.text.count("\n", 0, function_match.body_offset + match.start())
        assignments.append((match.group("name"), match.group("callee"), line))
    return tuple(assignments)


def _kotlin_extension_imports(text: str, function_match: FunctionMatch) -> tuple[tuple[str, str], ...]:
    """Map calls on typed parameters to explicitly imported extension declarations."""
    imports = parse_jvm_imports(text)
    parameters = engine._declared_parameter_types(function_match.text[:function_match.body_offset], kotlin=True)
    body = function_match.text[function_match.body_offset:]
    resolved = []
    for call, _ in find_calls(body):
        receiver, separator, name = call.rpartition(".")
        receiver_type = parameters.get(receiver) if separator else None
        imported = imports.get(name)
        if not receiver_type or not imported or "." not in imported:
            continue
        package, _, declared_name = imported.rpartition(".")
        resolved.append((call, f"{package}.{_last_type_token(receiver_type)}.{declared_name}"))
    return tuple(dict.fromkeys(resolved))


def _kotlin_top_level_extensions(
    text: str, classes: list[ClassMatch], path: Path, root: Path,
    local_classes: frozenset[str],
) -> tuple[list[Symbol], list[FlowEdge], list[FlowBoundary]]:
    package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*$", text)
    if package_match is None:
        return [], [], []
    package = package_match.group(1)
    symbols: list[Symbol] = []
    edges: list[FlowEdge] = []
    boundaries: list[FlowBoundary] = []
    for function in find_functions(text, 0, len(text), kotlin=True):
        if any(class_match.body_start <= function.start_offset <= class_match.body_end for class_match in classes):
            continue
        declaration = re.match(r"fun\s+([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(", function.text)
        if declaration is None:
            continue
        receiver_type, name = declaration.groups()
        symbol = f"{package}.{receiver_type}.{name}"
        evidence = Evidence(path.relative_to(root).as_posix(), function.start_line, function.end_line)
        symbols.append(Symbol(symbol, f"{package}.{receiver_type}", name, evidence))
        edges.extend(_jvm_edges_for_text(symbol, function, path, root, local_classes))
        boundaries.extend(_boundaries_for_text(symbol, function.text, evidence))
    return symbols, edges, boundaries


def _spring_handler_route(
    annotations: tuple[tuple[str, str], ...], prefix: str | None,
) -> tuple[str, str] | None:
    for annotation_name, arguments in annotations:
        method = SPRING_ROUTE_ANNOTATION_TO_METHOD.get(annotation_name)
        if method is None:
            continue
        path = ""
        if arguments:
            for index, part in enumerate(split_top_level(arguments[1:-1])):
                key, separator, value = part.partition("=")
                if separator:
                    if key.strip() not in {"value", "path"}:
                        continue
                elif index == 0:
                    value = key
                else:
                    continue
                literal = re.fullmatch(r'\s*"([^"\n]*)"\s*', value)
                if literal is None or "${" in literal.group(1) or "#{" in literal.group(1):
                    return None
                path = literal.group(1)
                break
        return method, join_route(prefix, path) or "/"
    return None


def _boundaries_for_text(symbol: str, text: str, evidence: Evidence) -> list[FlowBoundary]:
    patterns = {
        "branch": r"\bif\b|\bwhen\b",
        "async": r"\bawait\b|\basync\b|\bgo\s+",
        "retry": r"\bretry\b|\bbackoff\b",
        "error": r"\bthrow\b|\bcatch\b|\bexcept\b|\breturn\s+err\b",
        "transaction": r"@Transactional|\btransaction\b",
    }
    return [FlowBoundary(symbol, kind, evidence) for kind, pattern in patterns.items() if re.search(pattern, text)]


class _KotlinSpringAnalyzer:
    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        source = path.read_bytes()
        text = source.decode("utf-8", errors="ignore")
        cloud_declarations = jvm_client_declarations(text)
        result = AnalysisResult()
        classes = find_classes(text)
        function_names = {function.name for function in find_functions(text, 0, len(text), kotlin=True)}
        local_classes = frozenset(class_match.name for class_match in classes) - function_names
        for class_match in classes:
            class_name = class_match.name
            implements = kotlin_supertypes(class_match.header)
            annotations = class_match.annotations
            configuration_prefix = engine._spring_configuration_properties_prefix(annotations)
            route_prefix, unresolved_route_prefix = spring_route_prefix(annotations)
            qualifiers = engine._qualifiers(annotations)
            primary = "@Primary" in annotations
            class_body_text = text[class_match.body_start : class_match.body_end + 1]
            # Kotlin's primary-constructor injections live in the header, not the body
            # (`class Foo(private val x: AmqpTemplate)`) -- both need scanning here.
            publishers = engine._spring_amqp_publishers(class_match.header + class_body_text)
            kafka_publishers = engine._spring_kafka_publishers(class_match.header + class_body_text)
            for member in _kotlin_primary_constructor_members(class_match.header, class_match.start_line):
                evidence = member.evidence(path, root)
                if binding := engine._configuration_properties_binding(configuration_prefix, class_name, member.name, evidence):
                    result.configuration_bindings.append(binding)
                if member.type_name:
                    injection_symbol = f"{class_name}.{member.name}" if member.name else class_name
                    result.edges.append(FlowEdge(injection_symbol, member.type_name, "injects", evidence))
                    result.injections.append(Injection(
                        injection_symbol, member.type_name, engine._first_qualifier(member.raw_text), evidence,
                    ))
                    if binding := engine._spring_value_property_binding(class_name, member.name, member.raw_text, evidence):
                        result.configuration_bindings.append(binding)
            persistence_receivers = engine._spring_persistence_receivers(result.injections, class_name)
            rest_template_receivers = engine._spring_injected_receivers(result.injections, class_name, "RestTemplate")
            web_client_receivers = engine._spring_injected_receivers(result.injections, class_name, "WebClient")
            redis_publishers = engine._spring_injected_receivers(
                result.injections, class_name, "StringRedisTemplate", "RedisTemplate",
            )
            functions = find_functions(text, class_match.body_start, class_match.body_end, kotlin=True)
            generated_copy = "data" in annotations.split() and not any(
                function.name == "copy" for function in functions
            )
            for function_match in functions:
                symbol = f"{class_name}.{function_match.name}"
                evidence = Evidence(path.relative_to(root).as_posix(), function_match.start_line, function_match.end_line)
                imports = _kotlin_extension_imports(text, function_match)
                signature = function_match.text[:function_match.body_offset]
                result.symbols.append(Symbol(
                    symbol, class_name, function_match.name, evidence,
                    implements, imports, qualifiers, primary,
                    parameters=tuple(engine._declared_parameter_types(signature, kotlin=True).items()),
                    return_type=engine._spring_return_type(signature, kotlin=True),
                    local_assignments=_direct_kotlin_local_assignments(function_match),
                ))
                edges = _jvm_edges_for_text(symbol, function_match, path, root, local_classes)
                if generated_copy:
                    copy_lines = _generated_data_class_copy_lines(function_match)
                    edges = [edge for edge in edges if not (edge.target == "copy" and edge.evidence.start_line in copy_lines)]
                classified_edges, cloud_facts = _classify_spring_edges(
                    edges, persistence_receivers, redis_publishers, cloud_declarations,
                    _mongo_execute_callback_writes(function_match, persistence_receivers.mongo_templates, kotlin=True),
                )
                result.edges.extend(classified_edges)
                result.cloud_facts.extend(cloud_facts)
                result.boundaries.extend(_boundaries_for_text(symbol, function_match.modifiers + "\n" + function_match.text, evidence))
                line_evidence = _LineEvidence(function_match.start_line, function_match.end_line)
                result.error_contracts.extend(engine._spring_raised_error_contracts(
                    symbol, function_match.text, path, root, line_evidence,
                ))
                result.error_contracts.extend(engine._spring_timeout_fallback_contracts(
                    symbol, function_match.text, path, root, line_evidence, kotlin=True,
                ))
                result.static_service_calls.extend(engine._spring_rest_template_service_calls(
                    symbol, function_match.text, rest_template_receivers, path, root, line_evidence,
                ))
                result.static_service_calls.extend(engine._spring_web_client_service_calls(
                    symbol, function_match.text, web_client_receivers, path, root, line_evidence,
                ))
                modifier_text = function_match.modifiers
                annotations = tuple(spring_annotation_calls(modifier_text))
                _add_scheduled_job(result, annotations, symbol, function_match.name, evidence)
                if requirement := method_security_requirement(symbol, modifier_text, evidence):
                    result.security_requirements.append(requirement)
                result.resilience_policies.extend(engine._spring_resilience_policies(
                    symbol, function_match.text, modifier_text, web_client_receivers, path, root, line_evidence,
                ))
                result.message_contracts.extend(
                    engine._spring_publish_contracts(function_match.text, publishers, path, root, line_evidence, kotlin=True)
                )
                result.message_contracts.extend(
                    engine._spring_kafka_publish_contracts(function_match.text, kafka_publishers, path, root, line_evidence, kotlin=True)
                )
                result.error_contracts.extend(engine._spring_error_contracts(
                    symbol, function_match.text, modifier_text, evidence, kotlin=True,
                ))
                if not unresolved_route_prefix and (handler_route := _spring_handler_route(annotations, route_prefix)):
                    http_method, route = handler_route
                    result.entrypoints.append(EntryPoint("http", http_method, route, symbol, evidence))
                    result.contracts[symbol] = engine._spring_http_contract(function_match.text, modifier_text, kotlin=True)
                    result.api_headers.extend(_endpoint_headers(http_method, route, function_match, evidence))
                _add_message_listeners(result, annotations, symbol, function_match, evidence, kotlin=True)
        symbols, edges, boundaries = _kotlin_top_level_extensions(text, classes, path, root, local_classes)
        result.symbols.extend(symbols)
        result.edges.extend(edges)
        result.boundaries.extend(boundaries)
        return result


class _JavaSpringAnalyzer:
    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        source = path.read_bytes()
        text = source.decode("utf-8", errors="ignore")
        cloud_declarations = jvm_client_declarations(text)
        result = AnalysisResult()
        for class_match in find_classes(text):
            class_name = class_match.name
            implements = engine._java_interfaces(class_match.header)
            annotations = class_match.annotations
            configuration_prefix = engine._spring_configuration_properties_prefix(annotations)
            route_prefix, unresolved_route_prefix = spring_route_prefix(annotations)
            qualifiers = engine._qualifiers(annotations)
            primary = "@Primary" in annotations
            class_body_text = text[class_match.body_start : class_match.body_end + 1]
            publishers = engine._spring_amqp_publishers(class_match.header + class_body_text)
            kafka_publishers = engine._spring_kafka_publishers(class_match.header + class_body_text)
            functions = find_functions(text, class_match.body_start, class_match.body_end, kotlin=False)
            function_ranges = [
                (f.start_offset - class_match.body_start, f.end_offset - class_match.body_start) for f in functions
            ]
            for member in _java_class_field_members(class_body_text, function_ranges, class_match.start_line):
                evidence = member.evidence(path, root)
                if binding := engine._spring_value_property_binding(class_name, member.name, member.raw_text, evidence):
                    result.configuration_bindings.append(binding)
                if binding := engine._configuration_properties_binding(configuration_prefix, class_name, member.name, evidence):
                    result.configuration_bindings.append(binding)
                consumer = f"{class_name}.{member.name}"
                result.edges.append(FlowEdge(consumer, member.type_name, "injects", evidence))
                result.injections.append(Injection(consumer, member.type_name, engine._first_qualifier(member.raw_text), evidence))
            for member in _java_constructor_param_members(class_body_text, class_name, class_match.start_line):
                evidence = member.evidence(path, root)
                if binding := engine._spring_value_property_binding(class_name, member.name, member.raw_text, evidence):
                    result.configuration_bindings.append(binding)
            persistence_receivers = engine._spring_persistence_receivers(result.injections, class_name)
            rest_template_receivers = engine._spring_injected_receivers(result.injections, class_name, "RestTemplate")
            web_client_receivers = engine._spring_injected_receivers(result.injections, class_name, "WebClient")
            redis_publishers = engine._spring_injected_receivers(
                result.injections, class_name, "StringRedisTemplate", "RedisTemplate",
            )
            for function_match in functions:
                symbol = f"{class_name}.{function_match.name}"
                evidence = Evidence(path.relative_to(root).as_posix(), function_match.start_line, function_match.end_line)
                result.symbols.append(Symbol(
                    symbol, class_name, function_match.name, evidence, implements, (), qualifiers, primary,
                    tuple(engine._declared_parameter_types(
                        function_match.text[:function_match.body_offset], kotlin=False,
                    ).items()),
                ))
                edges = _jvm_edges_for_text(symbol, function_match, path, root)
                classified_edges, cloud_facts = _classify_spring_edges(
                    edges, persistence_receivers, redis_publishers, cloud_declarations,
                    _mongo_execute_callback_writes(function_match, persistence_receivers.mongo_templates, kotlin=False),
                )
                result.edges.extend(classified_edges)
                result.cloud_facts.extend(cloud_facts)
                result.boundaries.extend(_boundaries_for_text(symbol, function_match.modifiers + "\n" + function_match.text, evidence))
                line_evidence = _LineEvidence(function_match.start_line, function_match.end_line)
                result.error_contracts.extend(engine._spring_raised_error_contracts(
                    symbol, function_match.text, path, root, line_evidence,
                ))
                result.error_contracts.extend(engine._spring_timeout_fallback_contracts(
                    symbol, function_match.text, path, root, line_evidence, kotlin=False,
                ))
                result.static_service_calls.extend(engine._spring_rest_template_service_calls(
                    symbol, function_match.text, rest_template_receivers, path, root, line_evidence,
                ))
                result.static_service_calls.extend(engine._spring_web_client_service_calls(
                    symbol, function_match.text, web_client_receivers, path, root, line_evidence,
                ))
                modifier_text = function_match.modifiers
                annotations = tuple(spring_annotation_calls(modifier_text))
                _add_scheduled_job(result, annotations, symbol, function_match.name, evidence)
                if requirement := method_security_requirement(symbol, modifier_text, evidence):
                    result.security_requirements.append(requirement)
                result.resilience_policies.extend(engine._spring_resilience_policies(
                    symbol, function_match.text, modifier_text, web_client_receivers, path, root, line_evidence,
                ))
                result.message_contracts.extend(
                    engine._spring_publish_contracts(function_match.text, publishers, path, root, line_evidence)
                )
                result.message_contracts.extend(
                    engine._spring_kafka_publish_contracts(function_match.text, kafka_publishers, path, root, line_evidence)
                )
                result.error_contracts.extend(engine._spring_error_contracts(
                    symbol, function_match.text, modifier_text, evidence, kotlin=False,
                ))
                if not unresolved_route_prefix and (handler_route := _spring_handler_route(annotations, route_prefix)):
                    http_method, route = handler_route
                    result.entrypoints.append(EntryPoint("http", http_method, route, symbol, evidence))
                    result.contracts[symbol] = engine._spring_http_contract(function_match.text, modifier_text)
                    result.api_headers.extend(_endpoint_headers(http_method, route, function_match, evidence))
                _add_message_listeners(result, annotations, symbol, function_match, evidence, kotlin=False)
        return result


class JvmSpringAnalyzer:
    """Selects the Kotlin or Java analyzer while keeping the public stack identifier
    (`StaticAnalysisEngine`'s `"jvm-spring"` entry) stable. Public: unlike the
    per-stack analyzers in `engine.py`, this one is instantiated from outside this
    module.
    """

    def __init__(self) -> None:
        self._kotlin = _KotlinSpringAnalyzer()
        self._java = _JavaSpringAnalyzer()

    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        return self._java.analyze(path, root) if path.suffix == ".java" else self._kotlin.analyze(path, root)
