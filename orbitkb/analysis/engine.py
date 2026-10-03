"""Tree-sitter based, transport-aware static analysis.

This module deliberately produces a small flow model, not a generic code graph.
It parses source locally (zero LLM tokens) and records only entrypoints plus the
calls, persistence operations and messages reachable from their declared handler.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Mapping
from urllib.parse import urlparse

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_typescript
import yaml
from tree_sitter import Language, Node, Parser

from orbitkb.analysis.cloud_detection import (
    CLOUD_OPERATION_KIND_TO_FLOW_EDGE_KIND,
    cloud_edge_kind_and_fact,
    detect_cloud_facts,
    go_client_declarations,
    node_command_imports,
    node_stateful_client_declarations,
)
from orbitkb.analysis.cloud_taxonomy import (
    AWS_SDK_JS_V3_COMMANDS,
    AWS_SDK_JS_V3_MODULE_SERVICE,
    AWS_SERVICE_RESOURCE_TYPE,
)
from orbitkb.analysis.configuration_syntax import (
    PROPERTY_CONFIGURATION_KEY as _PROPERTY_CONFIGURATION_KEY,
)
from orbitkb.analysis.configuration_syntax import (
    SENSITIVE_CONFIGURATION_KEY as _SENSITIVE_CONFIGURATION_KEY,
)
from orbitkb.analysis.depth import DepthProvider, NoopDepthProvider
from orbitkb.analysis.frontends import (
    BUILTIN_MESSAGING_STACKS,
    AnalyzerFrontend,
    CombinedFrameworkAdapter,
    FlowClassifier,
    FrameworkAdapter,
    LanguageFrontend,
)
from orbitkb.analysis.http_destination import literal_public_http_destination
from orbitkb.analysis.jvm_feign import SpringFeignRecognizer
from orbitkb.analysis.jvm_grpc_analyzer import (
    jvm_grpc_client_bindings,
    jvm_grpc_handlers,
    kotlin_grpc_client_bindings,
    kotlin_grpc_handlers,
)
from orbitkb.analysis.jvm_security_analyzer import SpringSecurityAdapter
from orbitkb.analysis.jvm_spring_data import (
    SPRING_DATA_REPOSITORY_BASE_TYPES,
    SpringDataClassifier,
)
from orbitkb.analysis.jvm_spring_syntax import (
    spring_placeholder_literal as _spring_placeholder_literal,
)
from orbitkb.analysis.kotlin_dto_shapes import kotlin_data_class_shapes
from orbitkb.analysis.kotlin_expression_returns import enrich_kotlin_expression_returns
from orbitkb.analysis.models import (
    AnalysisResult,
    CloudFact,
    ConfigurationBinding,
    EntryPoint,
    ErrorContract,
    Evidence,
    ExternalHttpCall,
    FeatureFlag,
    FlowBoundary,
    FlowEdge,
    GrpcClientBinding,
    GrpcHandler,
    Injection,
    MessageContract,
    MigrationFact,
    NodeRouteCandidate,
    PersistenceFact,
    ResiliencePolicy,
    StaticServiceCall,
    Symbol,
)
from orbitkb.analysis.node_mongoose import (
    is_mongoose_query_sort,
    proven_mongoose_documents,
)
from orbitkb.analysis.node_router_mounts import resolve_node_router_mounts
from orbitkb.analysis.python_fastapi import proven_routes
from orbitkb.analysis.python_symbols import (
    local_bindings as python_local_bindings,
)
from orbitkb.analysis.python_symbols import (
    module_import_names as python_module_import_names,
)
from orbitkb.analysis.python_symbols import (
    module_name as python_module_name,
)
from orbitkb.analysis.python_symbols import (
    stable_local_imports as python_stable_local_imports,
)
from orbitkb.analysis.resolution import BoundedFlowResolver
from orbitkb.analysis.route_paths import join_route as _join_route
from orbitkb.discovery.go_imports import (
    has_standard_net_http_import,
    parse_go_import_declarations,
)
from orbitkb.discovery.node_http import express_receivers
from orbitkb.discovery.node_http import (
    express_route_prefixes as _express_route_prefixes,
)
from orbitkb.discovery.node_http import (
    fastify_receivers as fastify_receivers_for_source,
)
from orbitkb.discovery.node_http_routes import (
    literal_express_chained_route as _express_literal_chained_route,
)
from orbitkb.discovery.node_http_routes import (
    literal_fastify_route_definition as _fastify_literal_route_definition,
)
from orbitkb.discovery.node_imports import parse_node_named_imports
from orbitkb.discovery.node_local_handlers import (
    anonymous_commonjs_function,
    anonymous_default_function,
    local_mongoose_model_declarations,
    proven_commonjs_mongoose_model_export,
    proven_default_mongoose_model_export,
    proven_local_commonjs_flow_imports,
    proven_local_commonjs_mongoose_models,
    proven_local_esm_mongoose_models,
    proven_local_handler_imports,
    proven_local_mongoose_model_declarations,
)
from orbitkb.discovery.node_nest import (
    nest_decorator_path as _nest_decorator_path,
)
from orbitkb.discovery.node_nest import (
    nest_imports as nest_imports_for_source,
)
from orbitkb.discovery.node_nest import (
    nest_method_route as _nest_method_route,
)
from orbitkb.discovery.node_nest import (
    preceding_decorators as _preceding_decorators,
)
from orbitkb.discovery.scan_helpers import SKIP_DIRS
from orbitkb.security.redaction import redact_sensitive_values

_HTTP_METHOD_LITERALS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
STATIC_ANALYSIS_INPUT_VERSION = "98"

# Silent unless a caller (`orbitkb index/update --verbose`) explicitly raises this
# logger's level. A native crash (see _edges_for/_text) is not a catchable Python
# exception -- there's no traceback to inspect after the fact -- so the file and
# function named by the last flushed DEBUG line are what actually pinpoint it.
logger = logging.getLogger(__name__)

# Sibling-file test conventions SKIP_DIRS can't catch (Go/Node/Python test files
# live next to their source, not under a dedicated directory) -- same rationale
# as SKIP_DIRS's own "test"/"tests"/"__tests__" entries: test code isn't
# production behavior.
_TEST_FILE_SUFFIXES = (
    "_test.go",  # Go
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",  # Node/TS (Jest/Jasmine/Mocha)
)


def _is_test_file(path: Path) -> bool:
    name = path.name
    if name.endswith(_TEST_FILE_SUFFIXES):
        return True
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _string(node: Node, source: bytes) -> str | None:
    value = _text(node, source).strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return None


def _evidence(path: Path, root: Path, node: Node) -> Evidence:
    return Evidence(path.relative_to(root).as_posix(), node.start_point.row + 1, node.end_point.row + 1)


def _symbol(
    function: _Function,
    path: Path,
    root: Path,
    implements: tuple[str, ...] = (),
    imports: tuple[tuple[str, str], ...] = (),
    qualifiers: tuple[str, ...] = (),
    primary: bool = False,
) -> Symbol:
    owner, _separator, member = function.symbol.rpartition(".")
    return Symbol(
        function.symbol, owner, member or function.name, _evidence(path, root, function.declaration), implements, imports,
        qualifiers, primary,
    )


def _call_kind(target: str) -> str:
    name = target.lower()
    if any(word in name for word in ("publish", "produce", "sendmessage", "writemessages", "basicpublish")):
        return "publishes"
    if any(word in name for word in ("consume", "subscribe", "receive", "basicconsume")):
        return "consumes"
    receiver = name.rsplit(".", 1)[0] if "." in name else ""
    storage_receiver = any(word in receiver for word in (
        "repo", "repository", "dao", "database", "collection", "model", ".db", "store",
    ))
    if storage_receiver and any(word in name for word in ("save", "insert", "update", "delete", "create", "persist")):
        return "writes"
    if storage_receiver and any(word in name for word in ("find", "get", "query", "select", "load", "read")):
        return "reads"
    if any(word in name for word in ("validate", "authorize", "authenticate", "check")):
        return "validates"
    return "invokes"


_MONGOOSE_READ_METHODS = frozenset({
    "aggregate", "countdocuments", "distinct", "estimateddocumentcount", "exists",
    "find", "findbyid", "findone",
})
_MONGOOSE_WRITE_METHODS = frozenset({
    "bulkwrite", "create", "deletemany", "deleteone", "findbyidanddelete",
    "findbyidandupdate", "findoneanddelete", "findoneandupdate", "insertmany",
    "replaceone", "updatemany", "updateone",
})
_PRISMA_READ_METHODS = frozenset({
    "aggregate", "count", "findfirst", "findfirstorthrow", "findmany", "findunique",
    "finduniqueorthrow", "groupby",
})
_PRISMA_WRITE_METHODS = frozenset({
    "create", "createmany", "createmanyandreturn", "delete", "deletemany",
    "deletemanyandreturn", "update", "updatemany", "updatemanyandreturn", "upsert",
})
_GORM_READ_METHODS = frozenset({
    "count", "find", "findinbatches", "first", "last", "pluck", "row", "rows", "scan", "take",
})
_GORM_WRITE_METHODS = frozenset({
    "create", "delete", "exec", "save", "update", "updatecolumn", "updatecolumns", "updates",
})
_DATABASE_SQL_READ_METHODS = frozenset({"Query", "QueryContext", "QueryRow", "QueryRowContext"})
_DATABASE_SQL_WRITE_METHODS = frozenset({"Exec", "ExecContext"})
_SPRING_REPOSITORY_READ_METHODS = frozenset({
    "count", "existsById", "findAll", "findAllById", "findById", "getById", "getOne", "getReferenceById",
})
_SPRING_REPOSITORY_WRITE_METHODS = frozenset({
    "delete", "deleteAll", "deleteAllById", "deleteAllByIdInBatch", "deleteAllInBatch",
    "deleteById", "deleteInBatch", "flush", "save", "saveAll", "saveAndFlush",
})
_SPRING_JDBC_TEMPLATE_TYPES = frozenset({"JdbcTemplate", "NamedParameterJdbcTemplate"})
_SPRING_MONGO_TEMPLATE_TYPES = frozenset({"MongoTemplate", "ReactiveMongoTemplate"})
_SPRING_MONGO_READ_METHODS = frozenset({"aggregate", "count", "distinct", "exists", "find", "findById", "findOne"})
_SPRING_MONGO_WRITE_METHODS = frozenset({
    "findAndModify", "findAndReplace", "insert", "insertAll", "remove", "save", "updateFirst", "updateMulti", "upsert",
})
_ENTITY_MANAGER_READ_METHODS = frozenset({"find", "getReference"})
_ENTITY_MANAGER_WRITE_METHODS = frozenset({"flush", "merge", "persist", "remove"})
_REST_TEMPLATE_METHODS = {
    "getForEntity": "GET", "getForObject": "GET", "postForEntity": "POST",
    "postForObject": "POST", "put": "PUT", "delete": "DELETE",
}


def _mongoose_call_kind(target: str, model_variables: Mapping[str, str | None]) -> str | None:
    """Classify exact operations on a source-proven Mongoose model."""
    receiver, separator, method = target.rpartition(".")
    if not separator or receiver not in model_variables:
        return None
    if method.lower() in _MONGOOSE_READ_METHODS:
        return "reads"
    if method.lower() in _MONGOOSE_WRITE_METHODS:
        return "writes"
    return None


def _prisma_client_variables(source: str) -> frozenset[str]:
    """Return names locally constructed through the explicit Prisma client type."""
    return frozenset(re.findall(
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*new\s+PrismaClient\s*(?:<[^>]+>)?\s*\(", source,
    ))


def _prisma_call_kind(target: str, client_variables: frozenset[str]) -> str | None:
    """Classify exact model delegates on a locally constructed Prisma client."""
    client, separator, delegate_and_method = target.partition(".")
    delegate, separator, method = delegate_and_method.partition(".")
    if not separator or client not in client_variables or not delegate.isidentifier():
        return None
    if method.lower() in _PRISMA_READ_METHODS:
        return "reads"
    if method.lower() in _PRISMA_WRITE_METHODS:
        return "writes"
    return None


def _gorm_db_parameters(declaration: str) -> frozenset[str]:
    """Return direct parameters whose GORM database type is locally explicit."""
    return frozenset(re.findall(r"\b(\w+)\s+\*gorm\.DB\b", declaration))


def _gorm_call_kind(target: str, db_parameters: frozenset[str]) -> str | None:
    """Classify direct or simple fluent operations on an explicit GORM parameter."""
    receiver, separator, method = target.rpartition(".")
    root = receiver if receiver in db_parameters else _gorm_fluent_root(receiver)
    if not separator or root not in db_parameters:
        return None
    if method.lower() in _GORM_READ_METHODS:
        return "reads"
    if method.lower() in _GORM_WRITE_METHODS:
        return "writes"
    return None


def _gorm_fluent_root(receiver: str) -> str | None:
    """Return the root of a simple Go fluent chain without parsing arbitrary calls."""
    match = re.fullmatch(r"(\w+)(?:\.\w+\([^()]*\))*", receiver)
    return match.group(1) if match else None


def _database_sql_parameters(declaration: str) -> frozenset[str]:
    """Return direct parameters whose standard-library database type is explicit."""
    return frozenset(re.findall(r"\b(\w+)\s+\*sql\.(?:DB|Tx)\b", declaration))


def _database_sql_call_kind(target: str, parameters: frozenset[str]) -> str | None:
    """Classify exact `database/sql` query and execution calls on local parameters."""
    receiver, separator, method = target.rpartition(".")
    if not separator or receiver not in parameters:
        return None
    if method in _DATABASE_SQL_READ_METHODS:
        return "reads"
    if method in _DATABASE_SQL_WRITE_METHODS:
        return "writes"
    return None


def _spring_repository_receivers(injections: list[Injection], class_name: str) -> frozenset[str]:
    """Return locally injected members whose declared type is a Spring repository."""
    prefix = f"{class_name}."
    return frozenset(
        injection.consumer.removeprefix(prefix)
        for injection in injections
        if injection.consumer.startswith(prefix) and _is_spring_repository_type(injection.contract)
    )


def _is_spring_repository_type(contract: str) -> bool:
    type_name = contract.split("<", 1)[0].rsplit(".", 1)[-1]
    return type_name.endswith("Repository") or type_name in SPRING_DATA_REPOSITORY_BASE_TYPES


def _spring_repository_call_kind(target: str, receivers: frozenset[str]) -> str | None:
    """Classify exact standard operations on a locally injected repository member."""
    receiver, separator, method = target.rpartition(".")
    if not separator or receiver not in receivers:
        return None
    if method in _SPRING_REPOSITORY_READ_METHODS:
        return "reads"
    if method in _SPRING_REPOSITORY_WRITE_METHODS:
        return "writes"
    return None


def _spring_jdbc_template_receivers(injections: list[Injection], class_name: str) -> frozenset[str]:
    """Return locally injected members whose declared type is a Spring JDBC template."""
    prefix = f"{class_name}."
    return frozenset(
        injection.consumer.removeprefix(prefix)
        for injection in injections
        if injection.consumer.startswith(prefix)
        and injection.contract.split("<", 1)[0].rsplit(".", 1)[-1] in _SPRING_JDBC_TEMPLATE_TYPES
    )


def _spring_jdbc_template_call_kind(target: str, receivers: frozenset[str]) -> str | None:
    """Classify exact query/update operations on a local Spring JDBC template."""
    receiver, separator, method = target.rpartition(".")
    if not separator or receiver not in receivers:
        return None
    if method.startswith("query"):
        return "reads"
    if method in {"update", "batchUpdate"}:
        return "writes"
    return None


def _spring_mongo_template_receivers(injections: list[Injection], class_name: str) -> frozenset[str]:
    """Return locally injected members whose declared type is a Spring Mongo template."""
    prefix = f"{class_name}."
    return frozenset(
        injection.consumer.removeprefix(prefix)
        for injection in injections
        if injection.consumer.startswith(prefix)
        and injection.contract.split("<", 1)[0].rsplit(".", 1)[-1] in _SPRING_MONGO_TEMPLATE_TYPES
    )


def _spring_mongo_template_call_kind(target: str, receivers: frozenset[str]) -> str | None:
    """Classify exact Mongo operations on a locally injected Spring template."""
    receiver, separator, method = target.rpartition(".")
    if not separator or receiver not in receivers:
        return None
    if method in _SPRING_MONGO_READ_METHODS:
        return "reads"
    if method in _SPRING_MONGO_WRITE_METHODS:
        return "writes"
    if method == "execute":
        return "invokes"  # the callback determines read/write, not this call
    return None


def _entity_manager_receivers(injections: list[Injection], class_name: str) -> frozenset[str]:
    """Return locally injected members whose declared type is JPA EntityManager."""
    prefix = f"{class_name}."
    return frozenset(
        injection.consumer.removeprefix(prefix)
        for injection in injections
        if injection.consumer.startswith(prefix)
        and injection.contract.split("<", 1)[0].rsplit(".", 1)[-1] == "EntityManager"
    )


def _entity_manager_call_kind(target: str, receivers: frozenset[str]) -> str | None:
    """Classify exact JPA EntityManager operations on a local dependency."""
    receiver, separator, method = target.rpartition(".")
    if not separator or receiver not in receivers:
        return None
    if method in _ENTITY_MANAGER_READ_METHODS:
        return "reads"
    if method in _ENTITY_MANAGER_WRITE_METHODS:
        return "writes"
    return None


@dataclass(frozen=True)
class _SpringPersistenceReceivers:
    repositories: frozenset[str]
    jdbc_templates: frozenset[str]
    mongo_templates: frozenset[str]
    entity_managers: frozenset[str]


def _spring_persistence_receivers(
    injections: list[Injection], class_name: str,
) -> _SpringPersistenceReceivers:
    """Collect locally declared Spring persistence dependencies for one class."""
    return _SpringPersistenceReceivers(
        _spring_repository_receivers(injections, class_name),
        _spring_jdbc_template_receivers(injections, class_name),
        _spring_mongo_template_receivers(injections, class_name),
        _entity_manager_receivers(injections, class_name),
    )


def _spring_injected_receivers(
    injections: list[Injection], class_name: str, *type_names: str,
) -> frozenset[str]:
    """Return locally injected members with an explicitly declared type."""
    prefix = f"{class_name}."
    return frozenset(
        injection.consumer.removeprefix(prefix)
        for injection in injections
        if injection.consumer.startswith(prefix)
        and injection.contract.split("<", 1)[0].rsplit(".", 1)[-1] in type_names
    )


@dataclass(frozen=True)
class _Function:
    name: str
    symbol: str
    body: Node
    declaration: Node


class _FileAnalyzer:
    def __init__(self, language: Language):
        self._parser = Parser(language)
        self._tree = None

    def parse(self, source: bytes) -> Node:
        # A `Node` is a view into memory owned by its `Tree`. Returning only
        # `.root_node` drops the sole Python reference to the `Tree` in this
        # same expression, so CPython's refcounting frees it immediately --
        # before every subsequent `_walk()`/`_edges_for()` call over the
        # returned node and its descendants. Keeping it on `self` for the
        # analyzer's lifetime (one `parse()` call per file, per instance) is
        # what those callers were silently relying on the tree-sitter binding
        # to do internally -- which isn't guaranteed across versions, and
        # segfaulted for real on a fresh `pip install` with newer ones.
        self._tree = self._parser.parse(source)
        return self._tree.root_node

    @staticmethod
    def _edges_for(
        function: _Function, path: Path, root: Path, source: bytes,
        skip_call: Callable[[Node], bool] | None = None,
    ) -> list[FlowEdge]:
        logger.debug("  analyzing function: %s (%s)", function.symbol, path.name)
        edges = []
        for node in _walk(function.body):
            if node.type not in {"call_expression", "method_invocation"}:
                continue
            if skip_call is not None and skip_call(node):
                continue
            # Grammar field names differ (Kotlin exposes the callee as the first
            # named child while Go/TypeScript call it `function`). Normalize that
            # syntax detail at the parser boundary.
            if node.type == "method_invocation":
                target = ".".join(
                    _text(child, source)
                    for child in node.named_children
                    if child.type in {"identifier", "type_identifier"}
                )
            else:
                callee = node.child_by_field_name("function") or (node.named_children[0] if node.named_children else None)
                if callee is None:
                    continue
                target = _text(callee, source)
            if not target:
                continue
            edges.append(FlowEdge(function.symbol, target, _call_kind(target), _evidence(path, root, node)))
        return edges

    @staticmethod
    def _boundaries_for(function: _Function, path: Path, root: Path, source: bytes) -> list[FlowBoundary]:
        text = _text(function.declaration, source)
        patterns = {
            "branch": r"\bif\b|\bwhen\b",
            "async": r"\bawait\b|\basync\b|\bgo\s+",
            "retry": r"\bretry\b|\bbackoff\b",
            "error": r"\bthrow\b|\bcatch\b|\bexcept\b|\breturn\s+err\b",
            "transaction": r"@Transactional|\btransaction\b",
        }
        evidence = _evidence(path, root, function.declaration)
        return [FlowBoundary(function.symbol, kind, evidence) for kind, pattern in patterns.items() if re.search(pattern, text)]


class _GoAnalyzer(_FileAnalyzer):
    ROUTE_METHODS: ClassVar[frozenset[str]] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        source = path.read_bytes()
        tree = self.parse(source)
        source_text = source.decode("utf-8", errors="ignore")
        package = _go_package_name(source_text, path)
        imports = _go_imports(source_text)
        standard_http_imported = has_standard_net_http_import(source_text)
        cloud_declarations = go_client_declarations(source_text)
        functions: list[_Function] = []
        for node in _walk(tree):
            if node.type not in {"function_declaration", "method_declaration"}:
                continue
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            name = _text(name_node, source)
            receiver = ""
            if node.type == "method_declaration":
                receiver_node = node.child_by_field_name("receiver")
                receiver = _text(receiver_node, source) if receiver_node else ""
                receiver_match = re.search(r"\*?([A-Z][A-Za-z0-9_]*)", receiver)
                receiver = receiver_match.group(1) if receiver_match else ""
            symbol = f"{receiver}.{name}" if receiver else f"{package}.{name}"
            functions.append(_Function(name, symbol, body, node))

        edges: list[FlowEdge] = []
        cloud_facts: list[CloudFact] = []
        for fn in functions:
            fn_edges, fn_facts = self._edges_for_go(fn, path, root, source, cloud_declarations)
            edges.extend(fn_edges)
            cloud_facts.extend(fn_facts)
        result = AnalysisResult(
            edges=edges,
            cloud_facts=cloud_facts,
            symbols=[_symbol(fn, path, root, imports=imports) for fn in functions],
            message_contracts=[
                contract
                for function in functions
                for contract in (
                    *_go_amqp_publish_contracts(function, path, root, source),
                    *_go_kafka_publish_contracts(function, path, root, source),
                )
            ],
            error_contracts=[
                contract
                for function in functions
                for contract in _go_http_error_contracts(
                    function, path, root, source, standard_http_imported,
                )
            ],
        )
        for function in functions:
            entrypoint = _go_kafka_consumer_entrypoint(function, path, root, source)
            if entrypoint is not None:
                result.entrypoints.append(entrypoint)
                result.contracts[function.symbol] = {
                    "transport": "kafka", "direction": "consumes", "queue": entrypoint.name, "payload": None,
                }
        by_last_name = {fn.name: fn for fn in functions}
        groups = _go_route_groups(tree, source)
        for node in _walk(tree):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None:
                continue
            callee_text = _text(callee, source)
            method = callee_text.rsplit(".", 1)[-1].upper()
            args = arguments.named_children
            if callee_text.endswith(".Consume") and args and args[-1].type == "func_literal":
                channel = _string(args[0], source)
                body = args[-1].child_by_field_name("body")
                if channel and body:
                    symbol = f"message.consume:{channel}"
                    handler = _Function(channel, symbol, body, args[-1])
                    result.entrypoints.append(EntryPoint("message", "CONSUME", channel, symbol, _evidence(path, root, node)))
                    result.symbols.append(_symbol(handler, path, root, imports=imports))
                    handler_edges, handler_facts = self._edges_for_go(handler, path, root, source, cloud_declarations)
                    result.edges.extend(handler_edges)
                    result.cloud_facts.extend(handler_facts)
                    result.boundaries.extend(self._boundaries_for(handler, path, root, source))
                    result.contracts[symbol] = _message_contract(channel, _text(args[-1], source), "go")
            if callee_text == "http.HandleFunc" and standard_http_imported and len(args) == 2:
                route = _string(args[0], source)
                function = by_last_name.get(_text(args[1], source)) if args[1].type == "identifier" else None
                if route and route.startswith("/") and function and function.declaration.type == "function_declaration":
                    result.entrypoints.append(EntryPoint(
                        "http", "ANY", route, function.symbol, _evidence(path, root, node),
                    ))
                    result.contracts[function.symbol] = _go_http_contract(_text(function.declaration, source))
                continue
            if method not in self.ROUTE_METHODS or len(args) < 2:
                continue
            route = _string(args[0], source)
            receiver = callee_text.rsplit(".", 1)[0] if "." in callee_text else ""
            route = _join_route(groups.get(receiver), route)
            handler = _text(args[-1], source).rsplit(".", 1)[-1]
            function = by_last_name.get(handler)
            if route and function:
                result.entrypoints.append(EntryPoint("http", method, route, function.symbol, _evidence(path, root, node)))
                result.contracts[function.symbol] = _go_http_contract(_text(function.declaration, source))
        return result

    @staticmethod
    def _edges_for_go(
        function: _Function, path: Path, root: Path, source: bytes, cloud_declarations: dict,
    ) -> tuple[list[FlowEdge], list[CloudFact]]:
        db_parameters = _gorm_db_parameters(_text(function.declaration, source))
        sql_parameters = _database_sql_parameters(_text(function.declaration, source))
        edges: list[FlowEdge] = []
        cloud_facts: list[CloudFact] = []
        for edge in _FileAnalyzer._edges_for(function, path, root, source):
            cloud_kind, cloud_fact = cloud_edge_kind_and_fact(edge.target, edge.evidence, cloud_declarations)
            kind = (
                _gorm_call_kind(edge.target, db_parameters)
                or _database_sql_call_kind(edge.target, sql_parameters)
                or cloud_kind
                or edge.kind
            )
            edges.append(FlowEdge(edge.source, edge.target, kind, edge.evidence, edge.confidence, edge.origin))
            if cloud_fact is not None:
                cloud_facts.append(cloud_fact)
        return edges, cloud_facts


def _go_http_error_contracts(
    function: _Function, path: Path, root: Path, source: bytes, standard_http_imported: bool,
) -> list[ErrorContract]:
    """Extract explicit net/http error replies from a typed response writer."""
    if not standard_http_imported:
        return []
    declaration = _text(function.declaration, source)
    writer = _go_response_writer_name(declaration)
    if writer is None:
        return []
    patterns = (
        rf"\bhttp\s*\.\s*Error\s*\(\s*{re.escape(writer)}\s*,\s*(?P<detail>[^,]+),\s*(?P<status>http\s*\.\s*Status[A-Za-z]+|[45]\d\d)\s*\)",
        rf"\b{re.escape(writer)}\s*\.\s*WriteHeader\s*\(\s*(?P<status>http\s*\.\s*Status[A-Za-z]+|[45]\d\d)\s*\)",
    )
    contracts: list[ErrorContract] = []
    for pattern in patterns:
        for match in re.finditer(pattern, declaration):
            status = _go_literal_http_status(match.group("status"))
            if status is None:
                continue
            contracts.append(ErrorContract(
                source=function.symbol,
                role="maps",
                error_kind=_ERROR_KIND_BY_HTTP_STATUS.get(status, "unexpected" if status >= 500 else "unknown"),
                internal_type=None,
                protocol="http",
                transport_code=str(status),
                public_code=None,
                exposes_internal_detail=_go_http_error_exposes_internal_detail(match.groupdict().get("detail")),
                retryability="retryable" if status == 429 else "not_retryable",
                evidence=_declaration_match_evidence(
                    path, root, function.declaration, declaration, match.start(), match.end(),
                ),
            ))
    return contracts


def _go_http_error_exposes_internal_detail(detail: str | None) -> bool:
    """Recognize only a conventional error value sent directly through http.Error."""
    if detail is None:
        return False
    return re.fullmatch(r"\s*(?:err|error)\s*\.\s*Error\s*\(\s*\)\s*", detail) is not None


def _go_response_writer_name(declaration: str) -> str | None:
    signature = declaration.split("{", 1)[0]
    match = re.search(r"\b(\w+)\s+http\s*\.\s*ResponseWriter\b", signature)
    return match.group(1) if match else None


def _go_literal_http_status(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    name = re.sub(r"(?<!^)([A-Z])", r"_\1", value.replace("http.Status", "")).upper()
    return _HTTP_STATUS_CODES.get(name)


_GO_GRPC_UNIMPLEMENTED_SERVER = re.compile(
    r"\b(?:[A-Za-z_]\w*\.)?Unimplemented(?P<service>[A-Za-z_]\w*)Server\b",
)
_GO_GRPC_CLIENT_TYPE = re.compile(
    r"^(?P<package>[A-Za-z_]\w*)\.(?P<service>[A-Za-z_]\w*)Client$",
)
_GO_GRPC_CLIENT_FACTORY = re.compile(
    r"^(?P<package>[A-Za-z_]\w*)\.New(?P<service>[A-Za-z_]\w*)Client$",
)


def _go_grpc_handlers(files: list[Path], root: Path) -> list[GrpcHandler]:
    """Return Go methods on structs embedding one generated gRPC server base."""
    parser = Parser(Language(tree_sitter_go.language()))
    servers: dict[str, list[str]] = {}
    # (receiver_type, method_name, evidence), extracted eagerly while each file's `Tree` is
    # still referenced by `tree` below -- a struct and the method implementing it can live in
    # different files, so resolving against `servers` has to wait for every file to be scanned,
    # but a `Node` is a view into its `Tree`'s memory and `tree` gets reassigned every iteration,
    # so deferring `Node` access itself (not just the resolution) past this loop is a use-after-free.
    methods: list[tuple[str, str, Evidence]] = []
    for path in files:
        if path.suffix != ".go":
            continue
        source = path.read_bytes()
        tree = parser.parse(source)
        for node in _walk(tree.root_node):
            if node.type == "type_spec":
                name = node.child_by_field_name("name")
                struct_type = node.child_by_field_name("type")
                if name is None or struct_type is None or struct_type.type != "struct_type":
                    continue
                field_list = next(
                    (child for child in struct_type.named_children if child.type == "field_declaration_list"), None,
                )
                if field_list is None:
                    continue
                matches = []
                for field in field_list.named_children:
                    if field.child_by_field_name("name") is None:
                        matches.extend(_GO_GRPC_UNIMPLEMENTED_SERVER.finditer(_text(field, source)))
                if len(matches) == 1:
                    servers.setdefault(_text(name, source), []).append(matches[0].group("service"))
            elif node.type == "method_declaration":
                receiver = node.child_by_field_name("receiver")
                name = node.child_by_field_name("name")
                if receiver is None or name is None:
                    continue
                receiver_match = re.fullmatch(
                    r"\s*\(\s*(?:[A-Za-z_]\w*\s+)?\*?(?P<type>[A-Za-z_]\w*)\s*\)\s*",
                    _text(receiver, source),
                )
                if receiver_match is None:
                    continue
                methods.append((receiver_match.group("type"), _text(name, source), _evidence(path, root, node)))
    handlers: list[GrpcHandler] = []
    for receiver_type, method_name, evidence in methods:
        services = servers.get(receiver_type, [])
        if len(services) != 1:
            continue
        handlers.append(GrpcHandler(services[0], method_name, f"{receiver_type}.{method_name}", evidence))
    return handlers


def _go_grpc_client_bindings(files: list[Path], root: Path) -> list[GrpcClientBinding]:
    """Return generated Go client fields proven by a matching struct-literal factory."""
    parser = Parser(Language(tree_sitter_go.language()))
    fields: dict[tuple[str, str], list[tuple[str, str]]] = {}
    initializers: dict[tuple[str, str], list[tuple[str, str, Evidence]]] = {}
    # (owner, receiver_name), extracted eagerly for the same reason as in `_go_grpc_handlers`:
    # a `Node` is a view into its `Tree`'s memory, and `tree` below gets reassigned every file,
    # so a raw `Node` can't be deferred past this loop once a later file has been parsed.
    methods: list[tuple[str, str]] = []
    for path in files:
        if path.suffix != ".go":
            continue
        source = path.read_bytes()
        tree = parser.parse(source)
        for node in _walk(tree.root_node):
            if node.type == "type_spec":
                _go_grpc_client_fields(node, source, fields)
            elif node.type == "composite_literal":
                _go_grpc_client_initializers(node, source, path, root, initializers)
            elif node.type == "method_declaration":
                receiver = node.child_by_field_name("receiver")
                if receiver is None:
                    continue
                receiver_match = re.fullmatch(
                    r"\s*\(\s*(?P<name>[A-Za-z_]\w*)\s+\*?(?P<type>[A-Za-z_]\w*)\s*\)\s*",
                    _text(receiver, source),
                )
                if receiver_match is None:
                    continue
                methods.append((receiver_match.group("type"), receiver_match.group("name")))
    clients: dict[str, list[tuple[str, str, Evidence]]] = {}
    for key, typed_candidates in fields.items():
        initialized_candidates = initializers.get(key, [])
        if len(typed_candidates) != 1 or len(initialized_candidates) != 1:
            continue
        package, service = typed_candidates[0]
        initialized_package, initialized_service, evidence = initialized_candidates[0]
        if (package, service) == (initialized_package, initialized_service):
            owner, member = key
            clients.setdefault(owner, []).append((member, service, evidence))
    bindings: list[GrpcClientBinding] = []
    for owner, receiver_name in methods:
        for member, service, evidence in clients.get(owner, []):
            bindings.append(GrpcClientBinding(owner, f"{receiver_name}.{member}", service, evidence))
    return bindings


def _go_grpc_client_fields(
    type_spec: Node, source: bytes, fields: dict[tuple[str, str], list[tuple[str, str]]],
) -> None:
    name = type_spec.child_by_field_name("name")
    struct_type = type_spec.child_by_field_name("type")
    if name is None or struct_type is None or struct_type.type != "struct_type":
        return
    field_list = next(
        (child for child in struct_type.named_children if child.type == "field_declaration_list"), None,
    )
    if field_list is None:
        return
    owner = _text(name, source)
    for field in field_list.named_children:
        member = field.child_by_field_name("name")
        type_node = field.child_by_field_name("type")
        if member is None or type_node is None:
            continue
        match = _GO_GRPC_CLIENT_TYPE.fullmatch(_text(type_node, source))
        if match is not None:
            fields.setdefault((owner, _text(member, source)), []).append((
                match.group("package"), match.group("service"),
            ))


def _go_grpc_client_initializers(
    literal: Node,
    source: bytes,
    path: Path,
    root: Path,
    initializers: dict[tuple[str, str], list[tuple[str, str, Evidence]]],
) -> None:
    type_node = literal.child_by_field_name("type")
    body = literal.child_by_field_name("body")
    if type_node is None or body is None or type_node.type != "type_identifier":
        return
    owner = _text(type_node, source)
    for element in body.named_children:
        if element.type != "keyed_element":
            continue
        key = element.child_by_field_name("key")
        value = element.child_by_field_name("value")
        call = (
            next((child for child in value.named_children if child.type == "call_expression"), None)
            if value is not None else None
        )
        function = call.child_by_field_name("function") if call is not None else None
        match = (
            _GO_GRPC_CLIENT_FACTORY.fullmatch(_text(function, source))
            if function is not None else None
        )
        if key is None:
            continue
        if match is not None:
            initializers.setdefault((owner, _text(key, source)), []).append((
                match.group("package"), match.group("service"), _evidence(path, root, element),
            ))


def _node_promise_continuation(node: Node, source: bytes) -> bool:
    if node.type != "call_expression":
        return False
    callee = node.child_by_field_name("function")
    if callee is None or callee.type != "member_expression":
        return False
    receiver = callee.child_by_field_name("object")
    member = callee.child_by_field_name("property")
    return (
        receiver is not None and receiver.type == "call_expression"
        and member is not None and _text(member, source) in {"then", "catch", "finally"}
    )


class _NodeGraphqlAnalyzer(_FileAnalyzer):
    HTTP_ROUTE_METHODS: ClassVar[frozenset[str]] = frozenset(
        method.lower() for method in _HTTP_METHOD_LITERALS
    )

    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        if path.suffix in {".graphql", ".gql"}:
            return _GraphqlContractExtractor().analyze(path, root)
        if path.suffix == ".prisma":
            return AnalysisResult()
        source = path.read_bytes()
        source_text = source.decode("utf-8", errors="ignore")
        tree = self.parse(source)
        result = AnalysisResult()
        imported_handler_symbols = proven_local_handler_imports(tree, source, path, root)
        imports = (*_node_named_imports(source_text), *proven_local_commonjs_flow_imports(tree, source, path, root))
        launchdarkly_clients = _launchdarkly_client_variables(source_text, imports)
        graphql_error_constructors = _graphql_error_constructors(imports)
        local_mongoose_models = proven_local_mongoose_model_declarations(tree, source)
        mongoose_models = {
            **{alias: model_name for alias, (model_name, _collection, _line) in local_mongoose_models.items()},
            **proven_local_commonjs_mongoose_models(tree, source, path, root),
            **proven_local_esm_mongoose_models(tree, source, path, root),
        }
        prisma_clients = _prisma_client_variables(source_text)
        client_declarations = node_stateful_client_declarations(source_text)
        command_imports = node_command_imports(source_text)
        express_route_prefixes = _express_route_prefixes(source_text)
        _, express_routers = express_receivers(source_text)
        pending_routers = express_routers - express_route_prefixes.keys()
        fastify_receivers = fastify_receivers_for_source(source_text)
        error_handler_parameter_counts = {
            name: 4
            for name in _express_error_middleware_names(source_text, frozenset(express_route_prefixes))
        }
        for name in _fastify_error_handler_names(source_text, fastify_receivers):
            error_handler_parameter_counts.setdefault(name, 3)
        result.message_contracts.extend(_node_publish_contracts(tree, source, path, root))
        result.grpc_handlers.extend(_node_nest_grpc_handlers(tree, source, path, root, imports))
        result.grpc_client_bindings.extend(_node_nest_grpc_client_bindings(tree, source, path, root, imports))
        result.error_contracts.extend(_node_nest_exception_filter_contracts(tree, source, path, root, imports))
        for injection in _node_nest_constructor_injections(tree, source, path, root, imports):
            result.injections.append(injection)
            result.edges.append(FlowEdge(injection.consumer, injection.contract, "injects", injection.evidence))
        functions_by_name: dict[str, _Function] = {}
        named_functions = _node_named_functions(tree, source, path.stem)
        for function in named_functions:
            functions_by_name[function.name] = function
            self._record_function(
                result, function, path, root, source, imports, mongoose_models, prisma_clients,
                client_declarations, command_imports, launchdarkly_clients,
                error_handler_parameter_counts=error_handler_parameter_counts,
            )
        class_functions = _node_class_functions(tree, source)
        for function in class_functions:
            self._record_function(
                result, function, path, root, source, imports, mongoose_models, prisma_clients,
                client_declarations, command_imports, launchdarkly_clients,
            )
        class_methods = {function.symbol for function in class_functions}
        result.edges = [_node_resolve_self_call(edge, class_methods) for edge in result.edges]
        result.external_http_calls.extend(_node_external_http_calls(
            tree, source, path, root, (*named_functions, *class_functions),
        ))
        for function, method, route, contract in _nest_http_entrypoint_functions(tree, source, imports):
            self._record_function(
                result, function, path, root, source, imports, mongoose_models, prisma_clients,
                client_declarations, command_imports, launchdarkly_clients,
            )
            result.entrypoints.append(
                EntryPoint(
                    "http", method, route, function.symbol, _evidence(path, root, function.declaration),
                    contract=contract,
                )
            )
        route_prefixes = {
            **express_route_prefixes,
            **{receiver: "" for receiver in pending_routers},
            **{receiver: "" for receiver in fastify_receivers},
        }
        chained_express_receivers = express_route_prefixes.keys() | pending_routers
        for node in _walk(tree):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None:
                continue
            chained_route = _express_literal_chained_route(callee, source, chained_express_receivers)
            entrypoint_contract: dict | None = None
            if chained_route is not None:
                receiver, method, path_value, all_middleware = chained_route
                if method not in self.HTTP_ROUTE_METHODS:
                    continue
                http_methods = (method.upper(),)
                args = arguments.named_children
                handler_node = args[-1] if args else None
                entrypoint_contract = _express_route_middleware_contract([*all_middleware, *args], source)
            else:
                callee_text = _text(callee, source)
                if "." not in callee_text:
                    continue
                receiver, method = callee_text.rsplit(".", 1)
                if receiver not in route_prefixes:
                    continue
                args = arguments.named_children
                if method in self.HTTP_ROUTE_METHODS:
                    http_methods = (method.upper(),)
                    path_value = _string(args[0], source) if args else None
                    handler_node = args[-1] if len(args) > 1 else None
                    if receiver in express_routers or receiver in express_route_prefixes:
                        entrypoint_contract = _express_route_middleware_contract(args[1:], source)
                elif method == "route" and receiver in fastify_receivers:
                    route_definition = _fastify_literal_route_definition(args, source)
                    if route_definition is None:
                        continue
                    http_methods, path_value, handler_node = route_definition
                else:
                    continue
            if path_value is not None:
                path_value = _join_route(route_prefixes[receiver], path_value)
            handler = functions_by_name.get(_text(handler_node, source)) if handler_node is not None else None
            imported_handler_symbol = (
                imported_handler_symbols.get(_text(handler_node, source)) if handler_node is not None else None
            )
            if handler is None and handler_node is not None and handler_node.type in {"arrow_function", "function_expression"}:
                body = handler_node.child_by_field_name("body")
                if body is not None and path_value is not None:
                    route_method = http_methods[0].lower() if len(http_methods) == 1 else "route"
                    handler = _Function(
                        f"http.{route_method}:{path_value}",
                        f"{path.stem}.http.{route_method}:{path_value}", body, handler_node,
                    )
                    self._record_function(
                        result, handler, path, root, source, imports, mongoose_models, prisma_clients,
                        client_declarations, command_imports, launchdarkly_clients,
                    )
            handler_symbol = handler.symbol if handler is not None else imported_handler_symbol
            if path_value is None or handler_symbol is None:
                continue
            for http_method in http_methods:
                entrypoint = EntryPoint(
                    "http", http_method, path_value, handler_symbol,
                    _evidence(path, root, node),
                    contract=entrypoint_contract,
                )
                if receiver in pending_routers:
                    result.pending_node_routes.append(NodeRouteCandidate(receiver, entrypoint))
                else:
                    result.entrypoints.append(entrypoint)
        for parent in _walk(tree):
            if parent.type != "pair" or _text(parent.child_by_field_name("key"), source) not in {"Query", "Mutation", "Subscription"}:
                continue
            operation = _text(parent.child_by_field_name("key"), source)
            value = parent.child_by_field_name("value")
            if value is None or value.type != "object":
                continue
            for resolver in (node for node in value.named_children if node.type == "pair"):
                name_node = resolver.child_by_field_name("key")
                handler = resolver.child_by_field_name("value")
                if name_node is None or handler is None:
                    continue
                name = _text(name_node, source)
                symbol = f"{operation}.{name}"
                result.entrypoints.append(EntryPoint("graphql", operation.upper(), name, symbol, _evidence(path, root, resolver)))
                function = _Function(name, symbol, handler, resolver)
                result.symbols.append(_symbol(function, path, root, imports=imports))
                function_edges, function_cloud_facts = self._edges_for_node(
                    function, path, root, source, mongoose_models, prisma_clients, client_declarations, command_imports,
                )
                result.edges.extend(function_edges)
                result.cloud_facts.extend(function_cloud_facts)
                result.boundaries.extend(self._boundaries_for(function, path, root, source))
                result.error_contracts.extend(
                    _graphql_error_contracts(function, path, root, source, graphql_error_constructors)
                )
                result.feature_flags.extend(
                    _node_launchdarkly_feature_flags(function, path, root, source, launchdarkly_clients)
                )
        for node in _walk(tree):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None or not _text(callee, source).endswith(".consume"):
                continue
            args = arguments.named_children
            channel = _string(args[0], source) if args else None
            handler = args[1] if len(args) > 1 else None
            if channel is None or handler is None or handler.type not in {"arrow_function", "function_expression"}:
                continue
            symbol = f"message.consume:{channel}"
            result.entrypoints.append(EntryPoint("message", "CONSUME", channel, symbol, _evidence(path, root, node)))
            function = _Function(channel, symbol, handler, node)
            result.symbols.append(_symbol(function, path, root, imports=imports))
            function_edges, function_cloud_facts = self._edges_for_node(
                function, path, root, source, mongoose_models, prisma_clients, client_declarations, command_imports,
            )
            result.edges.extend(function_edges)
            result.cloud_facts.extend(function_cloud_facts)
            result.boundaries.extend(self._boundaries_for(function, path, root, source))
            result.contracts[symbol] = _message_contract(channel, _text(handler, source), "node")
        result.message_contracts.extend(_node_kafka_publish_contracts(tree, source, path, root))
        kafka_consumer = _node_kafka_consumer_handler(tree, source)
        if kafka_consumer is not None:
            topic, subscribe_node, call_node, handler = kafka_consumer
            symbol = f"message.consume:{topic}"
            result.entrypoints.append(EntryPoint("message", "CONSUME", topic, symbol, _evidence(path, root, call_node)))
            result.message_contracts.append(MessageContract(
                "consumes", topic, None, None, _evidence(path, root, subscribe_node),
            ))
            function = _Function(topic, symbol, handler, call_node)
            result.symbols.append(_symbol(function, path, root, imports=imports))
            function_edges, function_cloud_facts = self._edges_for_node(
                function, path, root, source, mongoose_models, prisma_clients, client_declarations, command_imports,
            )
            result.edges.extend(function_edges)
            result.cloud_facts.extend(function_cloud_facts)
            result.boundaries.extend(self._boundaries_for(function, path, root, source))
            result.contracts[symbol] = {"transport": "kafka", "direction": "consumes", "queue": topic, "payload": None}
        return result

    def _record_function(
        self,
        result: AnalysisResult,
        function: _Function,
        path: Path,
        root: Path,
        source: bytes,
        imports: tuple[tuple[str, str], ...],
        mongoose_models: Mapping[str, str | None],
        prisma_clients: frozenset[str],
        client_declarations: dict,
        command_imports: dict[str, tuple[str, str]],
        launchdarkly_clients: frozenset[str],
        *,
        error_handler_parameter_counts: dict[str, int] | None = None,
    ) -> None:
        """Store one Node handler and every bounded fact derived from it."""
        if any(symbol.name == function.symbol for symbol in result.symbols):
            return
        result.symbols.append(_symbol(function, path, root, imports=imports))
        function_edges, function_cloud_facts = self._edges_for_node(
            function, path, root, source, mongoose_models, prisma_clients, client_declarations, command_imports,
        )
        result.edges.extend(function_edges)
        result.cloud_facts.extend(function_cloud_facts)
        result.boundaries.extend(self._boundaries_for(function, path, root, source))
        result.error_contracts.extend(_node_http_error_contracts(
            function, path, root, source,
            error_handler_parameter_count=(
                error_handler_parameter_counts.get(function.name)
                if error_handler_parameter_counts is not None else None
            ),
        ))
        result.feature_flags.extend(
            _node_launchdarkly_feature_flags(function, path, root, source, launchdarkly_clients)
        )

    @staticmethod
    def _edges_for_node(
        function: _Function, path: Path, root: Path, source: bytes, mongoose_models: Mapping[str, str | None],
        prisma_clients: frozenset[str], client_declarations: dict, command_imports: dict[str, tuple[str, str]],
    ) -> tuple[list[FlowEdge], list[CloudFact]]:
        edges: list[FlowEdge] = []
        cloud_facts: list[CloudFact] = []
        mongoose_documents = proven_mongoose_documents(
            function.declaration, function.body, source, mongoose_models,
        )
        mongoose_document_saves = {f"{name}.save" for name in mongoose_documents}
        for edge in _FileAnalyzer._edges_for(
            function, path, root, source,
            skip_call=lambda node: (
                _node_promise_continuation(node, source)
                or is_mongoose_query_sort(node, source, mongoose_models)
            ),
        ):
            cloud_kind, cloud_fact = cloud_edge_kind_and_fact(edge.target, edge.evidence, client_declarations)
            mongoose_kind = _mongoose_call_kind(edge.target, mongoose_models)
            document = edge.target.rpartition(".")[0] if edge.target in mongoose_document_saves else None
            model_name = None
            if mongoose_kind:
                model_name = mongoose_models.get(edge.target.rpartition(".")[0])
            elif document is not None:
                model_name = mongoose_models.get(mongoose_documents[document])
            kind = (
                mongoose_kind
                or ("writes" if edge.target in mongoose_document_saves else None)
                or _prisma_call_kind(edge.target, prisma_clients)
                or cloud_kind
                or edge.kind
            )
            edges.append(FlowEdge(
                edge.source, edge.target, kind, edge.evidence, edge.confidence, edge.origin,
                model_name=model_name,
            ))
            if cloud_fact is not None:
                cloud_facts.append(cloud_fact)
        for node in _walk(function.body):
            if node.type != "new_expression":
                continue
            constructor = node.child_by_field_name("constructor")
            if constructor is None:
                continue
            local_name = _text(constructor, source)
            resolved = command_imports.get(local_name)
            if resolved is None:
                continue
            module_name, original_name = resolved
            service_name = AWS_SDK_JS_V3_MODULE_SERVICE.get(module_name)
            resource_type = AWS_SERVICE_RESOURCE_TYPE.get(service_name) if service_name else None
            operation = AWS_SDK_JS_V3_COMMANDS.get(original_name)
            if service_name is None or resource_type is None or operation is None:
                continue
            operation_kind, canonical_operation = operation
            evidence = _evidence(path, root, node)
            edges.append(FlowEdge(
                function.symbol, f"aws:{service_name}.{canonical_operation}",
                CLOUD_OPERATION_KIND_TO_FLOW_EDGE_KIND[operation_kind], evidence,
            ))
            cloud_facts.append(CloudFact(
                provider="aws", resource_type=resource_type, service_name=service_name,
                operation=canonical_operation, operation_kind=operation_kind, sdk="aws-sdk-js-v3",
                target_name=None, evidence=evidence,
            ))
        return edges, cloud_facts


_NODE_RESPONSE_PARAMETER_NAMES = frozenset({"res", "response", "reply"})
_NODE_HTTP_RESPONSE_STATUS = (
    r"\b{receiver}\s*\.\s*(?:status|code)\s*\(\s*(?P<status>[45]\d{{2}})\s*\)"
    r"\s*\.\s*(?:json|send|end)\s*\("
)
_NODE_HTTP_SEND_STATUS = (
    r"\b{receiver}\s*\.\s*sendStatus\s*\(\s*(?P<status>[45]\d{{2}})\s*\)"
)
_NODE_ERROR_IDENTIFIERS = frozenset({"err", "error", "exception"})
_NODE_INTERNAL_DETAIL_PROPERTIES = frozenset({"message", "stack", "cause"})


def _node_http_error_contracts(
    function: _Function, path: Path, root: Path, source: bytes,
    *,
    error_handler_parameter_count: int | None = None,
) -> list[ErrorContract]:
    """Extract explicit Express/Fastify error replies from a handler parameter.

    A method called ``status`` on an arbitrary dependency is not a public response.
    The receiver must be the conventional second handler parameter and the status
    must be immediately followed by an explicit response send operation.
    """
    receiver = _node_response_receiver(
        function, source, error_handler_parameter_count=error_handler_parameter_count,
    )
    if receiver is None:
        return []
    return _node_http_response_contracts(function, path, root, source, receiver)


def _node_http_response_contracts(
    function: _Function, path: Path, root: Path, source: bytes, receiver: str,
) -> list[ErrorContract]:
    """Extract literal HTTP responses after an adapter proves its response receiver."""
    patterns = (
        _NODE_HTTP_RESPONSE_STATUS,
        _NODE_HTTP_SEND_STATUS,
    )
    contracts: list[ErrorContract] = []
    for call in _walk(function.body):
        if call.type != "call_expression":
            continue
        response_call = _text(call, source)
        for pattern in patterns:
            match = re.search(pattern.format(receiver=re.escape(receiver)), response_call)
            if match is None:
                continue
            status = int(match.group("status"))
            contracts.append(ErrorContract(
                source=function.symbol,
                role="maps",
                error_kind=_ERROR_KIND_BY_HTTP_STATUS.get(status, "unexpected" if status >= 500 else "unknown"),
                internal_type=None,
                protocol="http",
                transport_code=str(status),
                public_code=None,
                exposes_internal_detail=_node_response_exposes_internal_detail(call, source),
                retryability="retryable" if status == 429 else "not_retryable",
                evidence=_evidence(path, root, call),
            ))
    return contracts


def _node_response_exposes_internal_detail(response_call: Node, source: bytes) -> bool:
    """Return whether a public response directly contains an error detail member.

    Inspect only the response call subtree: an error's message logged elsewhere in
    the handler is not a public exposure. The rule deliberately records a boolean,
    not the potentially sensitive value itself.
    """
    for member in _walk(response_call):
        if member.type != "member_expression":
            continue
        property_node = member.child_by_field_name("property")
        if property_node is None or not _node_member_is_direct_response_value(member, response_call):
            continue
        if (
            _node_member_root_identifier(member, source) in _NODE_ERROR_IDENTIFIERS
            and _text(property_node, source) in _NODE_INTERNAL_DETAIL_PROPERTIES
        ):
            return True
    return False


def _node_member_is_direct_response_value(member: Node, response_call: Node) -> bool:
    """Distinguish a public value from a value first passed to a sanitizer."""
    parent = member.parent
    if parent is None:
        return False
    if parent.type == "pair":
        return parent.child_by_field_name("value") == member
    return parent.type == "arguments" and parent.parent == response_call


def _node_member_root_identifier(member: Node, source: bytes) -> str | None:
    """Return the root object of a member chain such as ``error.cause.message``."""
    object_node = member.child_by_field_name("object")
    while object_node is not None and object_node.type == "member_expression":
        object_node = object_node.child_by_field_name("object")
    if object_node is None or object_node.type != "identifier":
        return None
    return _text(object_node, source)


def _node_response_receiver(
    function: _Function, source: bytes, *, error_handler_parameter_count: int | None = None,
) -> str | None:
    declaration = function.declaration
    if declaration.type == "variable_declarator":
        declaration = declaration.child_by_field_name("value") or declaration
    parameters = declaration.child_by_field_name("parameters")
    if parameters is None or len(parameters.named_children) < 2:
        return None
    parameter_names = [
        _text(identifier, source)
        for parameter in parameters.named_children
        if (identifier := next((node for node in _walk(parameter) if node.type == "identifier"), None)) is not None
    ]
    if len(parameter_names) >= 2 and parameter_names[1] in _NODE_RESPONSE_PARAMETER_NAMES:
        return parameter_names[1]
    if (
        error_handler_parameter_count is not None
        and len(parameter_names) == error_handler_parameter_count
        and parameter_names[0] in _NODE_ERROR_IDENTIFIERS
        and parameter_names[2] in _NODE_RESPONSE_PARAMETER_NAMES
        and (error_handler_parameter_count == 3 or parameter_names[3] == "next")
    ):
        return parameter_names[2]
    return None


_LAUNCHDARKLY_INITIALIZERS = frozenset({
    "launchdarkly-node-server-sdk.initialize",
    "node-server-sdk.initialize",
})
_LAUNCHDARKLY_FLAG_METHODS = "variation|boolVariation|stringVariation|numberVariation|jsonVariation"
_FEATURE_FLAG_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _launchdarkly_client_variables(source: str, imports: tuple[tuple[str, str], ...]) -> frozenset[str]:
    """Return variables initialized by an explicitly imported LaunchDarkly SDK."""
    initializers = [local for local, imported in imports if imported in _LAUNCHDARKLY_INITIALIZERS]
    if not initializers:
        return frozenset()
    initializer_pattern = "|".join(re.escape(name) for name in initializers)
    return frozenset(re.findall(
        rf"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?(?:{initializer_pattern})\s*\(", source,
    ))


def _node_launchdarkly_feature_flags(
    function: _Function,
    path: Path,
    root: Path,
    source: bytes,
    clients: frozenset[str],
) -> list[FeatureFlag]:
    """Extract literal reads on local LaunchDarkly clients without evaluating values."""
    if not clients:
        return []
    declaration = _text(function.declaration, source)
    receivers = "|".join(re.escape(client) for client in sorted(clients))
    pattern = re.compile(
        rf"\b(?:{receivers})\s*\.\s*(?:{_LAUNCHDARKLY_FLAG_METHODS})\s*\(\s*['\"]([^'\"]+)['\"]",
    )
    flags: list[FeatureFlag] = []
    for match in pattern.finditer(declaration):
        key = match.group(1)
        if _FEATURE_FLAG_KEY.fullmatch(key) is None:
            continue
        flags.append(FeatureFlag(
            source=function.symbol,
            key=key,
            provider="launchdarkly",
            evidence=_declaration_match_evidence(
                path, root, function.declaration, declaration, match.start(), match.end(),
            ),
        ))
    return flags


_GRAPHQL_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_GRAPHQL_ERROR_KIND = {
    "BAD_USER_INPUT": "validation",
    "CONFLICT": "conflict",
    "FORBIDDEN": "authorization",
    "NOT_FOUND": "not_found",
    "RATE_LIMITED": "rate_limit",
    "THROTTLED": "rate_limit",
    "UNAUTHENTICATED": "authorization",
}


def _graphql_error_constructors(imports: tuple[tuple[str, str], ...]) -> frozenset[str]:
    """Return local aliases explicitly imported as GraphQL's error type."""
    return frozenset(local for local, imported in imports if imported == "graphql.GraphQLError")


def _graphql_error_contracts(
    function: _Function,
    path: Path,
    root: Path,
    source: bytes,
    constructors: frozenset[str],
) -> list[ErrorContract]:
    """Extract thrown, literal GraphQL public codes from one resolver only."""
    contracts: list[ErrorContract] = []
    for node in _walk(function.body):
        if node.type != "new_expression" or node.parent is None or node.parent.type != "throw_statement":
            continue
        constructor = node.child_by_field_name("constructor")
        arguments = node.child_by_field_name("arguments")
        if constructor is None or arguments is None or _text(constructor, source) not in constructors:
            continue
        code_match = re.search(
            r"\bextensions\s*:\s*\{[^{}]*\bcode\s*:\s*[\"']([^\"']+)[\"']",
            _text(arguments, source),
        )
        if code_match is None or _GRAPHQL_ERROR_CODE.fullmatch(code_match.group(1)) is None:
            continue
        code = code_match.group(1)
        contracts.append(ErrorContract(
            source=function.symbol,
            role="raises",
            error_kind=_GRAPHQL_ERROR_KIND.get(code, "unexpected" if code == "INTERNAL_SERVER_ERROR" else "unknown"),
            internal_type="GraphQLError",
            protocol="graphql",
            transport_code=None,
            public_code=code,
            exposes_internal_detail=_graphql_error_exposes_internal_detail(arguments, source),
            retryability="retryable" if _GRAPHQL_ERROR_KIND.get(code) == "rate_limit" else "not_retryable",
            evidence=_evidence(path, root, node),
        ))
    return contracts


def _graphql_error_exposes_internal_detail(arguments: Node, source: bytes) -> bool:
    """Recognize only an error member sent directly as GraphQLError's message."""
    first_argument = next(iter(arguments.named_children), None)
    if first_argument is None or first_argument.type != "member_expression":
        return False
    property_node = first_argument.child_by_field_name("property")
    return (
        property_node is not None
        and _node_member_root_identifier(first_argument, source) in _NODE_ERROR_IDENTIFIERS
        and _text(property_node, source) in _NODE_INTERNAL_DETAIL_PROPERTIES
    )


class _GraphqlContractExtractor:
    """Extracts GraphQL schemas into compact, deterministic entrypoint contracts."""

    _TYPE_BLOCK = re.compile(r"\b(?:extend\s+)?type\s+(Query|Mutation|Subscription)\s*\{(?P<body>.*?)\}", re.DOTALL)
    _INPUT_BLOCK = re.compile(r"\binput\s+(\w+)\s*\{(?P<body>.*?)\}", re.DOTALL)
    _FIELD = re.compile(r"\b(\w+)\s*(?:\(([^)]*)\))?\s*:\s*([\[\]\w!]+)")
    _INPUT_FIELD = re.compile(r"\b(\w+)\s*:\s*([\[\]\w!]+)")
    _INTERFACE = re.compile(r"\binterface\s+(\w+)\s*\{", re.DOTALL)
    _IMPLEMENTS = re.compile(r"\btype\s+(\w+)\s+implements\s+([\w\s&]+)\s*\{", re.DOTALL)
    _UNION = re.compile(r"\bunion\s+(\w+)\s*=\s*([^\n]+)")

    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        source = path.read_text(encoding="utf-8", errors="ignore")
        return AnalysisResult(contracts=self.contracts(source))

    def contracts(self, source: str) -> dict[str, dict]:
        input_fields = {name: self._fields(body) for name, body in self._INPUT_BLOCK.findall(source)}
        return_options = self._return_options(source)
        contracts = {}
        for operation, body in self._TYPE_BLOCK.findall(source):
            for name, arguments, return_type in self._FIELD.findall(body):
                contracts[f"{operation}.{name}"] = {
                    "arguments": self._arguments(arguments, input_fields),
                    "returns": {**self._type_shape(return_type), **return_options.get(self._base_type(return_type), {})},
                }
        return contracts

    def _arguments(self, text: str, input_fields: dict[str, list[dict]]) -> list[dict]:
        return [
            {"name": name, **self._type_shape(type_name), "fields": input_fields.get(self._base_type(type_name), [])}
            for name, type_name in self._INPUT_FIELD.findall(text)
        ]

    def _fields(self, text: str) -> list[dict]:
        return [{"name": name, **self._type_shape(type_name)} for name, type_name in self._INPUT_FIELD.findall(text)]

    def _return_options(self, source: str) -> dict[str, dict]:
        options: dict[str, set[str]] = {name: set() for name in self._INTERFACE.findall(source)}
        for type_name, interfaces in self._IMPLEMENTS.findall(source):
            for interface in interfaces.split("&"):
                if interface.strip() in options:
                    options[interface.strip()].add(type_name)
        for union, members in self._UNION.findall(source):
            options[union] = {member.strip() for member in members.split("|") if member.strip()}
        return {name: {"possible_types": sorted(members)} for name, members in options.items() if members}

    @staticmethod
    def _base_type(type_name: str) -> str:
        return type_name.replace("[", "").replace("]", "").rstrip("!")

    def _type_shape(self, type_name: str) -> dict:
        return {"type": self._base_type(type_name), "required": type_name.endswith("!")}


class _PythonAnalyzer:
    """Analyze Python CLI entrypoints and handler calls."""

    def analyze(self, path: Path, root: Path) -> AnalysisResult:
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            return AnalysisResult()
        result = AnalysisResult()
        module = python_module_name(path, root)
        imports = python_stable_local_imports(tree, path, root)
        imported_names = python_module_import_names(tree)
        functions = [
            (function, "")
            for function in tree.body if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        functions.extend(
            (method, f"{node.name}.")
            for node in tree.body if isinstance(node, ast.ClassDef)
            for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"get", "post", "put", "patch", "delete", "head", "options"}
                    for decorator in method.decorator_list)
        )
        for function, owner in functions:
            symbol = f"{module}.{owner}{function.name}"
            evidence = Evidence(path.relative_to(root).as_posix(), function.lineno, function.end_lineno or function.lineno)
            local_names = python_local_bindings(function)
            visible_imports = tuple(sorted((name, target) for name, target in imports.items()
                                           if name not in local_names))
            result.symbols.append(Symbol(symbol, module, f"{owner}{function.name}", evidence,
                                         imports=visible_imports,
                                         bound_names=tuple(sorted(local_names | imported_names))))
            if not owner and function.name == "main":
                result.entrypoints.append(EntryPoint("cli", "COMMAND", path.stem, symbol, evidence))
            for call in (node for statement in function.body for node in ast.walk(statement) if isinstance(node, ast.Call)):
                target = _python_call_name(call.func)
                if target:
                    boundary_kind = (
                        "unresolved_call" if "." in target or target in imported_names else None
                    )
                    result.edges.append(
                        FlowEdge(symbol, target, _call_kind(target), Evidence(
                            path.relative_to(root).as_posix(), call.lineno, call.end_lineno or call.lineno,
                        ), boundary_kind=boundary_kind)
                    )
        return result


def _python_call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _go_http_contract(declaration: str) -> dict:
    variables = dict(re.findall(r"\bvar\s+(\w+)\s+([\w\[\]*]+)", declaration))
    decoded = re.search(r"\.Decode\s*\(\s*&?(\w+)\s*\)", declaration)
    request_name = decoded.group(1) if decoded else None
    request_type = variables.get(request_name or "")
    contract = {
        "request": {"name": request_name, "type": request_type, "required": True} if request_type else None,
        "returns": None,
        "validations": [],
        "authorization": [],
        "parameters": _go_bound_parameters(declaration),
    }
    if statuses := _go_response_statuses(declaration):
        contract["response_statuses"] = statuses
    return contract


def _message_contract(channel: str, declaration: str, language: str, *, transport: str = "rabbitmq") -> dict:
    patterns = {
        "java": r"\(\s*([\w<>]+)\s+(\w+)",
        "kotlin": r"\(\s*(\w+)\s*:\s*([\w?]+)",
        "node": r"\(?\s*(\w+)\s*:\s*([\w?]+)",
        "go": r"func\s*\(\s*(\w+)\s+([\w*]+)",
    }
    match = re.search(patterns[language], declaration)
    if language == "java" and match:
        type_name, name = match.groups()
    elif match:
        name, type_name = match.groups()
    else:
        name = type_name = None
    contract = {
        "transport": transport,
        "direction": "consumes",
        "queue": channel,
        "payload": {"name": name, "type": type_name.rstrip("?").lstrip("*") if type_name else None, "required": True} if type_name else None,
    }
    if transport == "rabbitmq" and re.search(r"\bidempot(?:ent|ency)\b", declaration, re.I):
        contract["idempotency"] = "detected"
    if transport == "rabbitmq" and re.search(r"\btimeout\b", declaration, re.I):
        contract["timeout"] = "detected"
    return contract


def _node_publish_contracts(tree: Node, source: bytes, path: Path, root: Path) -> list[MessageContract]:
    contracts = []
    for node in _walk(tree):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None or not _text(callee, source).endswith(".publish"):
            continue
        args = arguments.named_children
        channel = _string(args[0], source) if args else None
        routing_key = _string(args[1], source) if len(args) > 1 else None
        if channel:
            payload_type = _node_payload_type(node, args[2], source) if len(args) > 2 else None
            version = _message_header_version(_text(args[3], source)) if len(args) > 3 else None
            contracts.append(MessageContract("publishes", channel, routing_key, payload_type, _evidence(path, root, node), version))
    return contracts


# kafkajs's producer `send({ topic, messages })` is an object-literal call,
# not RabbitMQ's positional (channel, routingKey, payload) shape — `.send` alone
# is far too generic a method name to gate on (many unrelated APIs share it,
# e.g. Express's `res.send()`), so the literal presence of both `topic:` and
# `messages:` keys in the same object argument is the real structural proof,
# not the method name. The receiver must also be a producer built from an
# imported kafkajs Kafka client.
_NODE_KAFKA_SEND_RE = re.compile(
    r"\.\s*send\s*\(\s*\{[^{}]*?\btopic\s*:\s*['\"]([^'\"]+)['\"][^{}]*?\bmessages\s*:", re.DOTALL,
)


def _node_local_name_shadows_call(node: Node, source: bytes, name: str) -> bool:
    current = node.parent
    while current is not None and current.type != "program":
        if current.type in {"function_declaration", "function_expression", "arrow_function", "method_definition"}:
            parameters = current.child_by_field_name("parameters")
            if parameters is not None:
                for parameter in parameters.named_children:
                    parameter_name = (
                        parameter.named_children[0]
                        if parameter.type in {"required_parameter", "optional_parameter"} and parameter.named_children
                        else parameter
                    )
                    if parameter_name.type == "identifier" and _text(parameter_name, source) == name:
                        return True
        if current.type == "statement_block":
            for declaration in current.named_children:
                if declaration.type not in {"lexical_declaration", "variable_declaration"}:
                    continue
                for variable in declaration.named_children:
                    identifier = variable.child_by_field_name("name")
                    if identifier is not None and identifier.type == "identifier" and _text(identifier, source) == name:
                        return True
        current = current.parent
    return False


def _node_kafka_publish_contracts(tree: Node, source: bytes, path: Path, root: Path) -> list[MessageContract]:
    contracts = []
    producers = _node_kafka_producers(tree, source)
    for node in _walk(tree):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            continue
        receiver = callee.child_by_field_name("object")
        method = callee.child_by_field_name("property")
        if receiver is None or method is None or (
            receiver.type != "identifier" or _text(receiver, source) not in producers
            or _text(method, source) != "send"
            or _node_local_name_shadows_call(node, source, _text(receiver, source))
        ):
            continue
        call_text = _text(node, source)
        match = _NODE_KAFKA_SEND_RE.search(call_text)
        if match is None:
            continue
        version = _message_header_version(call_text)
        contracts.append(MessageContract("publishes", match.group(1), None, None, _evidence(path, root, node), version))
    return contracts


@dataclass(frozen=True)
class _NodeTopicCall:
    source: str
    target: str
    evidence: Evidence
    arguments: tuple[tuple[str, str] | None, ...]
    sink_parameter: int | None = None


def _node_kafka_clients(tree: Node, source: bytes, client_method: str) -> set[str]:
    declarations = [node for statement in tree.named_children
                    if statement.type == "lexical_declaration"
                    and _text(statement, source).lstrip().startswith("const ")
                    for node in statement.named_children if node.type == "variable_declarator"]
    kafka_imported = False
    for statement in tree.named_children:
        if statement.type != "import_statement":
            continue
        module = statement.child_by_field_name("source")
        if module is not None and _string(module, source) == "kafkajs" and re.search(
            r"\{\s*Kafka\s*\}", _text(statement, source)
        ):
            kafka_imported = True
    for declaration in declarations:
        name = declaration.child_by_field_name("name")
        value = declaration.child_by_field_name("value")
        if name is None or value is None or name.type != "object_pattern" or value.type != "call_expression":
            continue
        callee = value.child_by_field_name("function")
        arguments = value.child_by_field_name("arguments")
        if callee is None or _text(callee, source) != "require" or arguments is None:
            continue
        args = arguments.named_children
        if len(args) == 1 and _string(args[0], source) == "kafkajs" and any(
            field.type == "shorthand_property_identifier_pattern" and _text(field, source) == "Kafka"
            for field in name.named_children
        ):
            kafka_imported = True
    if not kafka_imported:
        return set()
    clients = set()
    for declaration in declarations:
        name = declaration.child_by_field_name("name")
        value = declaration.child_by_field_name("value")
        if name is None or name.type != "identifier" or value is None or value.type != "new_expression":
            continue
        constructor = value.child_by_field_name("constructor")
        if constructor is not None and _text(constructor, source) == "Kafka":
            clients.add(_text(name, source))
    clients_of_kind = set()
    for declaration in declarations:
        name = declaration.child_by_field_name("name")
        value = declaration.child_by_field_name("value")
        if name is None or name.type != "identifier" or value is None or value.type != "call_expression":
            continue
        callee = value.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            continue
        receiver = callee.child_by_field_name("object")
        member = callee.child_by_field_name("property")
        arguments = value.child_by_field_name("arguments")
        if receiver is not None and member is not None and arguments is not None and (
            _text(receiver, source) in clients and _text(member, source) == client_method
            and (client_method != "producer" or not arguments.named_children)
        ):
            clients_of_kind.add(_text(name, source))
    return clients_of_kind


def _node_kafka_producers(tree: Node, source: bytes) -> set[str]:
    return _node_kafka_clients(tree, source, "producer")


def _node_declared_parameters(function: _Function, source: bytes) -> tuple[str | None, ...]:
    declaration = function.declaration
    if declaration.type == "variable_declarator":
        declaration = declaration.child_by_field_name("value") or declaration
    parameters = declaration.child_by_field_name("parameters")
    if parameters is None:
        return ()
    names = []
    for parameter in parameters.named_children:
        if parameter.type == "identifier":
            names.append(_text(parameter, source))
        elif parameter.type in {"required_parameter", "optional_parameter"} and (
            parameter.named_children and parameter.named_children[0].type == "identifier"
        ):
            names.append(_text(parameter.named_children[0], source))
        else:
            names.append(None)
    return tuple(names)


def _node_unmodified_parameters(function: _Function, source: bytes) -> tuple[str | None, ...]:
    names = _node_declared_parameters(function, source)
    reassigned = set()
    for node in _walk(function.body):
        if node.type in {"assignment_expression", "augmented_assignment_expression"}:
            target = node.child_by_field_name("left")
        elif node.type == "update_expression":
            target = node.named_children[0] if node.named_children else None
        else:
            continue
        if target is not None and target.type == "identifier":
            reassigned.add(_text(target, source))
    return tuple(name if name not in reassigned else None for name in names)


def _node_topic_argument(node: Node, source: bytes) -> tuple[str, str] | None:
    if node.type == "identifier":
        return ("parameter", _text(node, source))
    if node.type == "string":
        value = _string(node, source)
        if value is not None and "\\" not in value:
            return ("literal", value)
    return None


def _node_kafka_sink_parameter(
    node: Node, source: bytes, producers: set[str], parameters: tuple[str | None, ...],
) -> int | None:
    callee = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if callee is None or arguments is None or callee.type != "member_expression":
        return None
    receiver = callee.child_by_field_name("object")
    member = callee.child_by_field_name("property")
    if receiver is None or member is None or _text(receiver, source) not in producers or _text(member, source) != "send":
        return None
    args = arguments.named_children
    if len(args) != 1 or args[0].type != "object":
        return None
    fields: dict[str, Node] = {}
    for field in args[0].named_children:
        if field.type == "shorthand_property_identifier":
            key, value = _text(field, source), field
        elif field.type == "pair":
            key_node = field.child_by_field_name("key")
            value = field.child_by_field_name("value")
            if key_node is None or key_node.type not in {"property_identifier", "string"} or value is None:
                return None
            key = _text(key_node, source).strip("\"'")
        else:
            return None
        if key in fields:
            return None
        fields[key] = value
    topic = fields.get("topic")
    if topic is None or "messages" not in fields or topic.type not in {"identifier", "shorthand_property_identifier"}:
        return None
    name = _text(topic, source)
    return parameters.index(name) if name in parameters else None


def _node_forwarded_kafka_topic_contracts(
    result: AnalysisResult, files: list[Path], root: Path, stack: str,
) -> list[MessageContract]:
    """Follow literal topic arguments only through proven local calls to a Kafka send."""
    language = (Language(tree_sitter_javascript.language()) if stack == "node-js"
                else Language(tree_sitter_typescript.language_typescript()))
    parser = Parser(language)
    edge_index: dict[tuple[str, Evidence], list[FlowEdge]] = {}
    for edge in result.edges:
        edge_index.setdefault((edge.source, edge.evidence), []).append(edge)
    functions: dict[str, list[tuple[str | None, ...]]] = {}
    calls: list[_NodeTopicCall] = []
    for path in files:
        if path.suffix not in {".js", ".jsx", ".ts", ".tsx"}:
            continue
        source = path.read_bytes()
        tree = parser.parse(source).root_node
        producers = _node_kafka_producers(tree, source)
        for function in (*_node_named_functions(tree, source, path.stem), *_node_class_functions(tree, source)):
            parameters = _node_unmodified_parameters(function, source)
            functions.setdefault(function.symbol, []).append(parameters)
            for node in _walk(function.body):
                if node.type != "call_expression":
                    continue
                arguments = node.child_by_field_name("arguments")
                if arguments is None:
                    continue
                evidence = _evidence(path, root, node)
                edges = edge_index.get((function.symbol, evidence), ())
                if len(edges) != 1 or edges[0].origin != "static" or edges[0].confidence != "high":
                    continue
                calls.append(_NodeTopicCall(
                    function.symbol, edges[0].target, evidence,
                    tuple(_node_topic_argument(arg, source) for arg in arguments.named_children),
                    _node_kafka_sink_parameter(node, source, producers, parameters),
                ))
    parameters_by_symbol = {symbol: declarations[0] for symbol, declarations in functions.items()
                            if len(declarations) == 1}
    topic_parameters: dict[str, set[int]] = {}
    for call in calls:
        if call.sink_parameter is not None and call.source in parameters_by_symbol:
            topic_parameters.setdefault(call.source, set()).add(call.sink_parameter)
    changed = True
    while changed:
        changed = False
        for call in calls:
            if call.source not in parameters_by_symbol or len(topic_parameters.get(call.target, ())) != 1:
                continue
            index = next(iter(topic_parameters[call.target]))
            argument = call.arguments[index] if index < len(call.arguments) else None
            if argument is None or argument[0] != "parameter":
                continue
            parameters = parameters_by_symbol[call.source]
            if argument[1] not in parameters:
                continue
            own_index = parameters.index(argument[1])
            known = topic_parameters.setdefault(call.source, set())
            if own_index not in known:
                known.add(own_index)
                changed = True
    return list(dict.fromkeys(
        MessageContract("publishes", argument[1], None, None, call.evidence)
        for call in calls
        if call.source in parameters_by_symbol and call.target in parameters_by_symbol
        and len(topic_parameters.get(call.target, ())) == 1
        for index in topic_parameters[call.target]
        if index < len(call.arguments)
        if (argument := call.arguments[index]) is not None and argument[0] == "literal"
    ))


def _node_bind_consumer_argument_types(
    result: AnalysisResult, files: list[Path], root: Path, stack: str,
) -> None:
    """Bind a consumer receiver only when observed bootstrap arguments agree."""
    if not any(entry.kind == "message" and entry.method == "CONSUME" for entry in result.entrypoints):
        return
    language = (Language(tree_sitter_javascript.language()) if stack == "node-js"
                else Language(tree_sitter_typescript.language_typescript()))
    parser = Parser(language)
    parsed: list[tuple[Path, bytes, Node, dict[str, str], dict[str, str]]] = []
    definitions: dict[str, list[tuple[Path, bytes, _Function]]] = {}
    for path in files:
        if path.suffix not in {".js", ".jsx", ".ts", ".tsx"}:
            continue
        source = path.read_bytes()
        tree = parser.parse(source).root_node
        commonjs_imports = proven_local_commonjs_flow_imports(tree, source, path, root)
        groups: dict[str, set[str]] = {}
        instance_groups: dict[str, set[str]] = {}
        for alias, target in commonjs_imports:
            instance_groups.setdefault(alias, set()).add(target)
        for alias, target in (*commonjs_imports, *_node_named_imports(source.decode("utf-8", errors="ignore"))):
            groups.setdefault(alias, set()).add(target)
        imports = {alias: next(iter(targets)) for alias, targets in groups.items() if len(targets) == 1}
        instances = {alias: next(iter(targets)) for alias, targets in instance_groups.items() if len(targets) == 1}
        parsed.append((path, source, tree, imports, instances))
        for function in _node_named_functions(tree, source, path.stem):
            definitions.setdefault(function.symbol, []).append((path, source, function))

    consumers: dict[str, tuple[Path, bytes, _Function, tuple[EntryPoint, ...]]] = {}
    for symbol, declarations in definitions.items():
        if len(declarations) != 1:
            continue
        path, source, function = declarations[0]
        evidence = _evidence(path, root, function.declaration)
        entrypoints = tuple(
            entry for entry in result.entrypoints
            if entry.kind == "message" and entry.method == "CONSUME"
            and entry.evidence.file_path == evidence.file_path
            and evidence.start_line <= entry.evidence.start_line <= entry.evidence.end_line <= evidence.end_line
        )
        if entrypoints:
            consumers[symbol] = (path, source, function, entrypoints)
    if not consumers:
        return

    owner_files: dict[str, set[str]] = {}
    symbol_counts: dict[str, int] = {}
    for symbol in result.symbols:
        owner_files.setdefault(symbol.owner, set()).add(symbol.evidence.file_path)
        symbol_counts[symbol.name] = symbol_counts.get(symbol.name, 0) + 1
    observed: dict[tuple[str, str], list[str | None]] = {}
    for path, source, tree, imports, instances in parsed:
        for call in _walk(tree):
            if call.type != "call_expression":
                continue
            callee = call.child_by_field_name("function")
            arguments = call.child_by_field_name("arguments")
            if callee is None or callee.type != "identifier" or arguments is None:
                continue
            alias = _text(callee, source)
            target = imports.get(alias) or f"{path.stem}.{alias}"
            if target not in consumers or _node_local_name_shadows_call(call, source, alias):
                continue
            _, target_source, function, _ = consumers[target]
            parameters = _node_unmodified_parameters(function, target_source)
            args = arguments.named_children
            for index, parameter in enumerate(parameters):
                if parameter is None:
                    continue
                argument = args[index] if index < len(args) else None
                argument_name = _text(argument, source) if argument is not None and argument.type == "identifier" else None
                owner = instances.get(argument_name) if argument_name is not None else None
                if (argument_name is None or _node_local_name_shadows_call(call, source, argument_name)
                        or len(owner_files.get(owner, ())) != 1 or "." in owner):
                    owner = None
                observed.setdefault((target, parameter), []).append(owner)

    bound: dict[str, list[tuple[str, str]]] = {}
    for (target, parameter), candidates in observed.items():
        if candidates and candidates[0] is not None and set(candidates) == {candidates[0]}:
            _, _, _, entrypoints = consumers[target]
            for symbol in (target, *(entry.symbol for entry in entrypoints)):
                if symbol_counts.get(symbol) == 1:
                    bound.setdefault(symbol, []).append((parameter, candidates[0]))
    if bound:
        result.symbols = [
            replace(symbol, parameters=(*symbol.parameters, *bound.get(symbol.name, ())))
            for symbol in result.symbols
        ]


def _node_call_scope(node: Node) -> tuple[int, int]:
    current = node.parent
    while current is not None and current.type not in {
        "function_declaration", "function_expression", "arrow_function", "method_definition", "program",
    }:
        current = current.parent
    return (current.start_byte, current.end_byte) if current is not None else (0, 0)


def _node_kafka_consumer_handler(tree: Node, source: bytes) -> tuple[str, Node, Node, Node] | None:
    """Pair one literal subscription and one handler on the same Kafka consumer."""
    consumers = _node_kafka_clients(tree, source, "consumer")
    subscriptions: list[tuple[str, str | None, Node]] = []
    handlers: list[tuple[str, Node, Node | None]] = []
    for node in _walk(tree):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or callee.type != "member_expression" or arguments is None:
            continue
        receiver = callee.child_by_field_name("object")
        member = callee.child_by_field_name("property")
        if receiver is None or member is None or _text(receiver, source) not in consumers:
            continue
        method = _text(member, source)
        args = arguments.named_children
        if method == "subscribe":
            topics = (
                [
                    _string(field.child_by_field_name("value"), source)
                    for field in args[0].named_children
                    if field.type == "pair" and _text(field.child_by_field_name("key"), source) == "topic"
                ]
                if len(args) == 1 and args[0].type == "object" else []
            )
            subscriptions.append((_text(receiver, source), topics[0] if len(topics) == 1 else None, node))
            continue
        if method != "run":
            continue
        if len(args) != 1 or args[0].type != "object":
            handlers.append((_text(receiver, source), node, None))
            continue
        handler_property = next(
            (
                child for child in args[0].named_children
                if child.type == "pair" and _text(child.child_by_field_name("key"), source) in {"eachMessage", "eachBatch"}
            ),
            None,
        )
        if handler_property is None:
            handlers.append((_text(receiver, source), node, None))
            continue
        handler = handler_property.child_by_field_name("value")
        if handler is None or handler.type not in {"arrow_function", "function_expression"}:
            handlers.append((_text(receiver, source), node, None))
            continue
        handlers.append((_text(receiver, source), node, handler))
    if len(subscriptions) == len(handlers) == 1 and (
        subscriptions[0][0] == handlers[0][0]
        and subscriptions[0][1] is not None and handlers[0][2] is not None
        and _node_call_scope(subscriptions[0][2]) == _node_call_scope(handlers[0][1])
        and subscriptions[0][2].start_byte < handlers[0][1].start_byte
    ):
        _, topic, subscribe_node = subscriptions[0]
        _, run_node, handler = handlers[0]
        assert topic is not None and handler is not None
        return topic, subscribe_node, run_node, handler
    return None


def _node_payload_type(call: Node, payload: Node, source: bytes) -> str | None:
    payload_name = _text(payload, source).strip()
    if not re.fullmatch(r"\w+", payload_name):
        return None
    enclosing = call.parent
    while enclosing is not None:
        if enclosing.type in {"arrow_function", "function_declaration", "function_expression", "method_definition"}:
            parameters = enclosing.child_by_field_name("parameters")
            if parameters is not None:
                types = {
                    name: type_name.rstrip("?")
                    for name, type_name in re.findall(r"\b(\w+)\s*\??\s*:\s*([\w.$<>\[\]?]+)", _text(parameters, source))
                }
                if payload_type := types.get(payload_name):
                    return payload_type
        enclosing = enclosing.parent
    return None


def _go_amqp_publish_contracts(function: _Function, path: Path, root: Path, source: bytes) -> list[MessageContract]:
    declaration = _text(function.declaration, source)
    publishers = set(re.findall(r"\b(\w+)\s+\*?amqp\.Channel\b", declaration))
    if not publishers:
        return []
    parameter_types = _go_declared_parameter_types(declaration)
    contracts = []
    for node in _walk(function.body):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None:
            continue
        callee_text = _text(callee, source)
        receiver, _separator, method = callee_text.rpartition(".")
        args = arguments.named_children
        positions = {"Publish": (0, 1, 4), "PublishWithContext": (1, 2, 5)}.get(method)
        if receiver not in publishers or positions is None or len(args) <= positions[2]:
            continue
        exchange = _string(args[positions[0]], source)
        routing_key = _string(args[positions[1]], source)
        if exchange is None or routing_key is None:
            continue
        payload = re.search(r"\bBody\s*:\s*(\w+)", _text(args[positions[2]], source))
        payload_type = parameter_types.get(payload.group(1)) if payload else None
        version = _message_header_version(_text(args[positions[2]], source))
        contracts.append(MessageContract("publishes", exchange, routing_key, payload_type, _evidence(path, root, node), version))
    return contracts


def _go_kafka_publish_contracts(function: _Function, path: Path, root: Path, source: bytes) -> list[MessageContract]:
    """`writer.WriteMessages(ctx, kafka.Message{Topic: "orders", ...})` —
    segmentio/kafka-go's producer shape. Only the case where `Topic` is a
    literal inside the message struct itself is resolved; a writer whose
    topic instead comes from its own construction (`kafka.NewWriter(...)`)
    with no per-call override is a known, documented gap, not a guess."""
    declaration = _text(function.declaration, source)
    writers = set(re.findall(r"\b(\w+)\s+\*?kafka\.Writer\b", declaration))
    if not writers:
        return []
    parameter_types = _go_declared_parameter_types(declaration)
    contracts = []
    for node in _walk(function.body):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None:
            continue
        callee_text = _text(callee, source)
        receiver, _separator, method = callee_text.rpartition(".")
        if receiver not in writers or method != "WriteMessages":
            continue
        args = arguments.named_children
        if not args:
            continue
        message_text = _text(args[-1], source)
        topic_match = re.search(r'\bTopic\s*:\s*"([^"]+)"', message_text)
        if topic_match is None:
            continue
        value_match = re.search(r"\bValue\s*:\s*(?:\[\]byte\()?(\w+)", message_text)
        payload_type = parameter_types.get(value_match.group(1)) if value_match else None
        version = _message_header_version(message_text)
        contracts.append(MessageContract("publishes", topic_match.group(1), None, payload_type, _evidence(path, root, node), version))
    return contracts


def _go_kafka_consumer_entrypoint(function: _Function, path: Path, root: Path, source: bytes) -> EntryPoint | None:
    """kafka-go has no callback-based consumer like RabbitMQ's `.Consume()` —
    idiomatic usage constructs a `*kafka.Reader` bound to a literal `Topic`
    and calls `.ReadMessage(...)` in a loop within the *same* function, so
    that containing function itself is the entrypoint, not a synthesized
    handler."""
    declaration = _text(function.declaration, source)
    reader_topics = dict(re.findall(
        r'\b(\w+)\s*:?=\s*kafka\.NewReader\s*\(\s*kafka\.ReaderConfig\{[^}]*?\bTopic\s*:\s*"([^"]+)"',
        declaration, re.DOTALL,
    ))
    if not reader_topics:
        return None
    body_text = _text(function.body, source)
    for reader_var, topic in reader_topics.items():
        if re.search(rf"\b{re.escape(reader_var)}\s*\.\s*ReadMessage\s*\(", body_text):
            return EntryPoint("message", "CONSUME", topic, function.symbol, _evidence(path, root, function.declaration))
    return None


def _go_declared_parameter_types(declaration: str) -> dict[str, str]:
    parameters = re.search(r"func\s+(?:\([^)]*\)\s+)?\w+\s*\(([^)]*)\)", declaration, re.DOTALL)
    if parameters is None:
        return {}
    return {
        name: type_name
        for name, type_name in re.findall(r"\b(\w+)\s+(\*?[\w.\[\]]+)", parameters.group(1))
    }


def _message_header_version(source: str) -> str | None:
    header = r'["\']?(?:schema_version|schemaVersion|x-schema-version|x-version)["\']?'
    patterns = (
        rf"setHeader\s*\(\s*{header}\s*,\s*[\"']([^\"']+)",
        rf"{header}\s*:\s*[\"']([^\"']+)",
    )
    for pattern in patterns:
        if match := re.search(pattern, source):
            return match.group(1)
    return None


def _call_text(source: str, start_offset: int) -> str:
    opening = source.find("(", start_offset)
    if opening < 0:
        return source[start_offset:]
    depth = 0
    quote = ""
    for offset in range(opening, len(source)):
        char = source[offset]
        if quote:
            if char == quote and source[offset - 1] != "\\":
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start_offset : offset + 1]
    return source[start_offset:]


def _spring_amqp_publishers(class_source: str) -> set[str]:
    java_fields = re.findall(r"\b(?:RabbitTemplate|AmqpTemplate)\s+(\w+)", class_source)
    kotlin_properties = re.findall(r"\b(?:val|var)\s+(\w+)\s*:\s*(?:RabbitTemplate|AmqpTemplate)", class_source)
    return set(java_fields) | set(kotlin_properties)


def _spring_kafka_publishers(class_source: str) -> set[str]:
    java_fields = re.findall(r"\bKafkaTemplate\s*(?:<[^>]*>)?\s+(\w+)", class_source)
    kotlin_properties = re.findall(r"\b(?:val|var)\s+(\w+)\s*:\s*KafkaTemplate\s*(?:<[^>]*>)?", class_source)
    return set(java_fields) | set(kotlin_properties)


def _spring_kafka_publish_contracts(
    declaration: str,
    publishers: set[str],
    path: Path,
    root: Path,
    node: Node,
    *,
    kotlin: bool = False,
) -> list[MessageContract]:
    """`kafkaTemplate.send(topic, payload)` or `send(topic, key, payload)` —
    the optional middle key argument is skipped, never captured, since only
    the topic (routing) and payload matter for the contract. Unlike
    RabbitMQ's convertAndSend, Kafka's send has no fixed arity, so the
    pattern tolerates either shape rather than requiring exactly 3 args."""
    if not publishers:
        return []
    receivers = "|".join(re.escape(name) for name in sorted(publishers))
    pattern = rf'\b(?:{receivers})\s*\.\s*send\s*\(\s*"([^"]+)"\s*,(?:\s*[\w."\']+\s*,)?\s*(\w+)\s*\)'
    parameter_types = _declared_parameter_types(declaration, kotlin)
    contracts = []
    for match in re.finditer(pattern, declaration):
        topic, payload = match.groups()
        contracts.append(MessageContract(
            "publishes", topic, None, parameter_types.get(payload),
            _declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
            _message_header_version(_call_text(declaration, match.start())),
        ))
    return contracts


def _spring_publish_contracts(
    declaration: str,
    publishers: set[str],
    path: Path,
    root: Path,
    node: Node,
    *,
    kotlin: bool = False,
) -> list[MessageContract]:
    if not publishers:
        return []
    receivers = "|".join(re.escape(name) for name in sorted(publishers))
    pattern = rf'\b(?:{receivers})\s*\.\s*convertAndSend\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(\w+)'
    parameter_types = _declared_parameter_types(declaration, kotlin)
    contracts = []
    for match in re.finditer(pattern, declaration):
        exchange, routing_key, payload = match.groups()
        contracts.append(MessageContract(
            "publishes", exchange, routing_key, parameter_types.get(payload),
            _declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
            _message_header_version(_call_text(declaration, match.start())),
        ))
    return contracts


def _declared_parameter_types(declaration: str, kotlin: bool) -> dict[str, str]:
    parameters = re.search(r"\((.*?)\)", declaration, re.DOTALL)
    if parameters is None:
        return {}
    if kotlin:
        return {name: type_name.rstrip("?") for name, type_name in re.findall(r"(\w+)\s*:\s*([\w.<>?]+)", parameters.group(1))}
    return {
        name: type_name
        for type_name, name in re.findall(r"(?:@\w+\s+)*([\w<>?]+)\s+(\w+)", parameters.group(1))
    }


def _declaration_match_evidence(
    path: Path,
    root: Path,
    node: Node,
    declaration: str,
    start_offset: int,
    end_offset: int,
) -> Evidence:
    start_line = node.start_point.row + declaration.count("\n", 0, start_offset) + 1
    end_line = node.start_point.row + declaration.count("\n", 0, end_offset) + 1
    return Evidence(path.relative_to(root).as_posix(), start_line, end_line)


def _java_interfaces(class_text: str) -> tuple[str, ...]:
    match = re.search(r"\bimplements\s+([^\{]+)", class_text)
    return tuple(item.strip() for item in match.group(1).split(",")) if match else ()


def _spring_http_contract(declaration: str, annotations: str, kotlin: bool = False) -> dict:
    request = _spring_request(declaration, kotlin)
    return_type = _spring_return_type(declaration, kotlin)
    validations = re.findall(r"@(Valid|Validated|NotNull|NotBlank|NotEmpty|Positive|Negative|Size|Pattern)\b", declaration)
    authorization = re.findall(r"@(PreAuthorize|Secured|RolesAllowed)\b", annotations)
    contract = {
        "request": request,
        "returns": {"type": return_type, "required": True} if return_type else None,
        "validations": validations,
        "authorization": authorization,
        "parameters": _spring_bound_parameters(declaration, kotlin),
    }
    if statuses := _spring_response_statuses(f"{annotations}\n{declaration}"):
        contract["response_statuses"] = statuses
    return contract


_HTTP_STATUS_CODES = {
    "OK": 200, "CREATED": 201, "ACCEPTED": 202, "NO_CONTENT": 204,
    "BAD_REQUEST": 400, "NOT_FOUND": 404, "CONFLICT": 409,
    "INTERNAL_SERVER_ERROR": 500, "SERVICE_UNAVAILABLE": 503, "GATEWAY_TIMEOUT": 504,
}

_ERROR_KIND_BY_HTTP_STATUS = {
    400: "validation", 401: "authorization", 403: "authorization", 404: "not_found",
    409: "conflict", 422: "validation", 429: "rate_limit",
}


def _spring_error_contracts(
    symbol: str, declaration: str, annotations: str, evidence: Evidence, *, kotlin: bool,
) -> list[ErrorContract]:
    """Extract only explicit Spring exception-to-status mappings.

    An exception handler without a literal transport status may be completed by a
    global response mapper or dynamic code. It remains absent here rather than
    becoming a guessed error contract.
    """
    class_suffix = r"::class" if kotlin else r"\.class"
    match = re.search(
        rf"@ExceptionHandler\s*\(\s*(?:value\s*=\s*)?(?:\{{\s*)?([\w.]+){class_suffix}", annotations,
    )
    statuses = _spring_response_statuses(annotations)
    if match is None or not statuses:
        return []
    status = statuses[0]
    code = status["code"]
    internal_type = match.group(1).rsplit(".", 1)[-1]
    exception_parameter = _spring_error_handler_parameter_name(declaration, internal_type, kotlin=kotlin)
    return [ErrorContract(
        source=symbol,
        role="maps",
        error_kind="timeout" if _is_timeout_exception_type(internal_type) else _ERROR_KIND_BY_HTTP_STATUS.get(code, "unexpected" if code >= 500 else "unknown"),
        internal_type=internal_type,
        protocol="http",
        transport_code=str(code),
        public_code=None,
        exposes_internal_detail=_spring_response_exposes_internal_detail(declaration, exception_parameter),
        retryability="retryable" if code == 429 else "not_retryable",
        evidence=evidence,
    )]


def _spring_error_handler_parameter_name(
    declaration: str, error_type: str, *, kotlin: bool,
) -> str | None:
    """Return the handler parameter only when its declared type matches the annotation."""
    pattern = (
        rf"\b(?P<name>\w+)\s*:\s*{re.escape(error_type)}\b"
        if kotlin
        else rf"\b{re.escape(error_type)}\s+(?P<name>\w+)\b"
    )
    match = re.search(pattern, declaration)
    return match.group("name") if match is not None else None


def _spring_response_exposes_internal_detail(declaration: str, exception_parameter: str | None) -> bool:
    """Recognize a handler returning its error detail directly through a Spring response."""
    if exception_parameter is None:
        return False
    direct_detail = (
        rf"{re.escape(exception_parameter)}\s*\.\s*"
        r"(?:getMessage|getCause|getStackTrace)\s*\(\s*\)"
        rf"|{re.escape(exception_parameter)}\s*\.\s*(?:message|cause|stackTrace)\b"
    )
    response_patterns = (
        rf"\breturn\s+ProblemDetail\s*\.\s*forStatusAndDetail\s*\(\s*[^,]+,\s*(?:{direct_detail})\s*\)",
        rf"\breturn\s+ResponseEntity\s*\.\s*"
        rf"(?:status\s*\([^)]*\)|internalServerError\s*\(\s*\))\s*\.\s*body\s*\(\s*(?:{direct_detail})\s*\)",
    )
    return any(re.search(pattern, declaration) is not None for pattern in response_patterns)


def _spring_raised_error_contracts(
    symbol: str, declaration: str, path: Path, root: Path, node: Node,
) -> list[ErrorContract]:
    """Extract explicit local ``throw new`` statements without guessing status.

    A normal domain exception has no transport semantics until an indexed mapper
    proves one. ``ResponseStatusException`` is the narrow exception because its
    literal ``HttpStatus`` argument is part of the source fact itself.
    """
    contracts: list[ErrorContract] = []
    for match in re.finditer(r"\bthrow\s+new\s+([\w.]+)\s*\([^)]*\)", declaration):
        statuses = _spring_response_statuses(match.group(0))
        status = statuses[0] if statuses else None
        code = status["code"] if status is not None else None
        contracts.append(ErrorContract(
            source=symbol,
            role="raises",
            error_kind=_ERROR_KIND_BY_HTTP_STATUS.get(code, "unexpected" if code and code >= 500 else "unknown"),
            internal_type=match.group(1).rsplit(".", 1)[-1],
            protocol="http" if code is not None else "internal",
            transport_code=str(code) if code is not None else None,
            public_code=None,
            exposes_internal_detail=False,
            retryability="retryable" if code == 429 else ("not_retryable" if code is not None else "unknown"),
            evidence=_declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
        ))
    return contracts


_TIMEOUT_EXCEPTION_TYPES = frozenset({
    "timeoutexception", "sockettimeoutexception", "connecttimeoutexception",
    "readtimeoutexception", "webclientrequestexception",
})


def _is_timeout_exception_type(error_type: str) -> bool:
    return error_type.rsplit(".", 1)[-1].casefold() in _TIMEOUT_EXCEPTION_TYPES


def _spring_timeout_fallback_contracts(
    symbol: str,
    declaration: str,
    path: Path,
    root: Path,
    node: Node,
    *,
    kotlin: bool,
) -> list[ErrorContract]:
    """Extract explicit local timeout handling without inferring generic fallbacks."""
    catch_pattern = (
        r'\bcatch\s*\(\s*\w+\s*:\s*(?P<type>[\w.]+)\s*\)'
        if kotlin
        else r'\bcatch\s*\(\s*(?P<type>[\w.]+)(?:\s+\w+)?\s*\)'
    )
    patterns = (
        (re.compile(catch_pattern), True),
        (re.compile(
            r'\.onError(?:Resume|Return)\s*\(\s*(?P<type>[\w.]+)'
            r'(?:\.class|::class\.java)\b',
        ), False),
    )
    contracts: list[ErrorContract] = []
    for pattern, is_catch in patterns:
        for match in pattern.finditer(declaration):
            error_type = match.group("type").rsplit(".", 1)[-1]
            if not _is_timeout_exception_type(error_type):
                continue
            protocol, transport_code = _timeout_fallback_transport(declaration, match.end()) if is_catch else ("internal", None)
            contracts.append(ErrorContract(
                source=symbol,
                role="handles",
                error_kind="timeout",
                internal_type=error_type,
                protocol=protocol,
                transport_code=transport_code,
                public_code=None,
                exposes_internal_detail=False,
                retryability="unknown",
                evidence=_declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
            ))
    return contracts


def _timeout_fallback_transport(declaration: str, catch_end: int) -> tuple[str, str | None]:
    """Return HTTP success only for an explicit ResponseEntity success in a catch block."""
    body = _braced_block_after(declaration, catch_end)
    if body is None:
        return "internal", None
    success_response = re.search(
        r'\breturn\s+ResponseEntity\s*\.\s*(?:ok\s*\(|status\s*\(\s*HttpStatus\.OK\s*\))',
        body,
    )
    return ("http", "200") if success_response else ("internal", None)


def _braced_block_after(source: str, start: int) -> str | None:
    """Return the balanced brace block after an offset, without parsing arbitrary Java."""
    block_start = source.find("{", start)
    if block_start < 0:
        return None
    depth = 0
    for index in range(block_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[block_start : index + 1]
    return None


def _spring_response_statuses(source: str) -> list[dict]:
    names = re.findall(r"(?:ResponseStatus|HttpStatus\.)\s*\(?\s*HttpStatus\.([A-Z_]+)", source)
    names += re.findall(r"HttpStatus\.([A-Z_]+)", source)
    return _status_values(names)


def _go_response_statuses(source: str) -> list[dict]:
    names = re.findall(r"WriteHeader\s*\(\s*http\.Status([A-Za-z]+)\s*\)", source)
    normalized = [re.sub(r"(?<!^)([A-Z])", r"_\1", name).upper() for name in names]
    return _status_values(normalized)


def _status_values(names: list[str]) -> list[dict]:
    return [{"code": _HTTP_STATUS_CODES[name], "name": name} for name in dict.fromkeys(names) if name in _HTTP_STATUS_CODES]


def _spring_bound_parameters(declaration: str, kotlin: bool) -> list[dict]:
    bindings = []
    for annotation, kind in (("PathVariable", "path"), ("RequestParam", "query"), ("RequestHeader", "header")):
        pattern = rf'@{annotation}\s*\(\s*(?:value\s*=\s*)?"([^"]+)"[^)]*\)\s+([\w<>?]+)\s+(\w+)'
        for name, type_name, variable in re.findall(pattern, declaration):
            bindings.append({"kind": kind, "name": name, "variable": variable, "type": type_name.rstrip("?"), "required": kind == "path"})
    return bindings


def _go_bound_parameters(declaration: str) -> list[dict]:
    bindings = []
    for pattern, kind in ((r'\.PathValue\s*\(\s*"([^"]+)"', "path"), (r'\.Query\s*\(\s*\)\.Get\s*\(\s*"([^"]+)"', "query"), (r'\.Header\.Get\s*\(\s*"([^"]+)"', "header")):
        bindings.extend({"kind": kind, "name": name, "variable": None, "type": None, "required": kind == "path"} for name in re.findall(pattern, declaration))
    return bindings


def _spring_request(declaration: str, kotlin: bool) -> dict | None:
    if kotlin:
        match = re.search(r"@RequestBody\s+(\w+)\s*:\s*([\w<>?]+)", declaration)
        return {"name": match.group(1), "type": match.group(2).rstrip("?"), "required": not match.group(2).endswith("?")} if match else None
    match = re.search(r"@RequestBody\s+([\w<>]+)\s+(\w+)", declaration)
    return {"name": match.group(2), "type": match.group(1), "required": True} if match else None


def _spring_return_type(declaration: str, kotlin: bool) -> str | None:
    if kotlin:
        match = re.search(r"\)\s*:\s*([\w<>?]+)", declaration)
    else:
        match = re.search(r"\b([A-Z][\w<>]*)\s+\w+\s*\(", declaration)
    return match.group(1).rstrip("?") if match else None


def _qualifiers(source: str) -> tuple[str, ...]:
    return tuple(re.findall(r'@(?:Qualifier|Service|Component|Repository)\s*\(\s*"([^"]+)"', source))


def _first_qualifier(source: str) -> str | None:
    qualifiers = _qualifiers(source)
    return qualifiers[0] if qualifiers else None


def _go_package_name(source: str, path: Path) -> str:
    match = re.search(r"(?m)^\s*package\s+(\w+)", source)
    return match.group(1) if match else path.parent.name


def _go_imports(source: str) -> tuple[tuple[str, str], ...]:
    imports = []
    for alias, module in parse_go_import_declarations(source):
        package = module.rstrip("/").rsplit("/", 1)[-1]
        local_name = alias or package
        if local_name not in {"_", "."}:
            imports.append((local_name, package))
    return tuple(imports)


def _go_route_groups(tree: Node, source: bytes) -> dict[str, str]:
    groups = {}
    for node in _walk(tree):
        if node.type != "short_var_declaration":
            continue
        match = re.search(r'(\w+)\s*:=\s*\w+\.Group\s*\(\s*"([^"]+)"', _text(node, source))
        if match:
            groups[match.group(1)] = match.group(2)
    return groups


def _node_named_imports(source: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (local_name, f"{module_name}.{original_name}")
        for local_name, module_name, original_name in parse_node_named_imports(source)
    )


def _express_error_middleware_names(source: str, receivers: frozenset[str]) -> frozenset[str]:
    """Return named middleware registered directly through a proven Express receiver."""
    return frozenset(
        handler
        for receiver, handler in re.findall(r"\b(\w+)\.use\s*\(\s*(\w+)\s*\)", source)
        if receiver in receivers
    )


def _express_route_middleware_contract(arguments: list[Node], source: bytes) -> dict | None:
    """Return directly registered Express middleware names, excluding the handler.

    Express executes every argument before the final handler as middleware. The
    analyzer deliberately records only direct identifiers: factories, inline
    callbacks and computed expressions may be valid middleware but cannot be
    named without guessing. This is route metadata, not a claim about the
    middleware's authorization or validation semantics.
    """
    middleware = [
        {"symbol": _text(argument, source)}
        for argument in arguments[:-1]
        if argument.type == "identifier"
    ]
    return {"route_middlewares": middleware} if middleware else None


def _fastify_error_handler_names(source: str, receivers: frozenset[str]) -> frozenset[str]:
    """Return named handlers registered directly through a proven Fastify receiver."""
    return frozenset(
        handler
        for receiver, handler in re.findall(r"\b(\w+)\.setErrorHandler\s*\(\s*(\w+)\s*\)", source)
        if receiver in receivers
    )


def _node_named_functions(tree: Node, source: bytes, module_name: str) -> list[_Function]:
    """Extract local function declarations and assigned functions with stable symbols."""
    functions: list[_Function] = []
    for node in _walk(tree):
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
        elif node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if value_node is None or value_node.type not in {"arrow_function", "function_expression"}:
                continue
            body = value_node.child_by_field_name("body")
        else:
            continue
        if name_node is None or body is None:
            continue
        name = _text(name_node, source)
        functions.append(_Function(name, f"{module_name}.{name}", body, node))
    anonymous_default = anonymous_default_function(tree)
    if anonymous_default is not None:
        body = anonymous_default.child_by_field_name("body")
        if body is not None:
            functions.append(_Function("default", f"{module_name}.default", body, anonymous_default))
    anonymous_commonjs = anonymous_commonjs_function(tree, source)
    if anonymous_commonjs is not None:
        body = anonymous_commonjs.child_by_field_name("body")
        if body is not None:
            functions.append(_Function("exports", f"{module_name}.exports", body, anonymous_commonjs))
    return functions


def _node_class_functions(tree: Node, source: bytes) -> list[_Function]:
    """Return directly declared class methods so injected calls can resolve locally."""
    functions: list[_Function] = []
    for class_node in _walk(tree):
        if class_node.type != "class_declaration":
            continue
        class_name = class_node.child_by_field_name("name")
        class_body = class_node.child_by_field_name("body")
        if class_name is None or class_body is None:
            continue
        for method in class_body.named_children:
            if method.type != "method_definition":
                continue
            name = method.child_by_field_name("name")
            body = method.child_by_field_name("body")
            if name is None or body is None or _text(name, source) == "constructor":
                continue
            method_name = _text(name, source)
            functions.append(_Function(
                method_name, f"{_text(class_name, source)}.{method_name}", body, method,
            ))
    return functions


def _node_resolve_self_call(edge: FlowEdge, class_methods: set[str]) -> FlowEdge:
    if edge.source not in class_methods or not edge.target.startswith("this."):
        return edge
    owner = edge.source.rpartition(".")[0]
    target = f"{owner}.{edge.target.removeprefix('this.')}"
    return replace(edge, target=target) if target in class_methods else edge


def _node_http_constants_and_clients(tree: Node, source: bytes) -> tuple[dict[str, str], set[str]]:
    constants: dict[str, str] = {}
    clients: set[str] = set()
    duplicates: set[str] = set()
    for statement in tree.named_children:
        if statement.type == "import_statement":
            module = statement.child_by_field_name("source")
            if module is not None and _string(module, source) == "axios":
                match = re.fullmatch(
                    r"\s*import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]axios['\"]\s*;?\s*",
                    _text(statement, source),
                )
                if match:
                    clients.add(match.group(1))
            continue
        if statement.type != "lexical_declaration" or not _text(statement, source).lstrip().startswith("const "):
            continue
        for declaration in statement.named_children:
            if declaration.type != "variable_declarator":
                continue
            name = declaration.child_by_field_name("name")
            value = declaration.child_by_field_name("value")
            if name is None or name.type != "identifier" or value is None:
                continue
            identifier = _text(name, source)
            if value.type == "string":
                literal = _string(value, source)
                if literal is not None and "\\" not in literal:
                    if identifier in constants:
                        duplicates.add(identifier)
                    constants[identifier] = literal
            elif value.type == "call_expression":
                callee = value.child_by_field_name("function")
                arguments = value.child_by_field_name("arguments")
                if callee is not None and _text(callee, source) == "require" and arguments is not None:
                    args = arguments.named_children
                    if len(args) == 1 and _string(args[0], source) == "axios":
                        clients.add(identifier)
    for name in duplicates:
        constants.pop(name, None)
    for node in _walk(tree):
        if node.type not in {"assignment_expression", "augmented_assignment_expression"}:
            continue
        target = node.child_by_field_name("left")
        if target is None:
            continue
        receiver = target.child_by_field_name("object") if target.type == "member_expression" else target
        if receiver is not None and receiver.type == "identifier":
            clients.discard(_text(receiver, source))
    return constants, clients


def _node_literal_http_url(node: Node, source: bytes, constants: dict[str, str]) -> str | None:
    if node.type == "string":
        value = _string(node, source)
        return value if value is not None and "\\" not in value else None
    if node.type != "template_string":
        return None
    parts = node.named_children
    if len(parts) != 2 or parts[0].type != "template_substitution" or parts[1].type != "string_fragment":
        return None
    expression = parts[0].named_children
    if len(expression) != 1 or expression[0].type != "identifier":
        return None
    base = constants.get(_text(expression[0], source))
    suffix = _text(parts[1], source)
    return base + suffix if base is not None and "\\" not in suffix else None


def _node_external_http_calls(
    tree: Node, source: bytes, path: Path, root: Path, functions: tuple[_Function, ...],
) -> list[ExternalHttpCall]:
    constants, clients = _node_http_constants_and_clients(tree, source)
    if not clients:
        return []
    symbols = [function.symbol for function in functions]
    calls = []
    for function in functions:
        if symbols.count(function.symbol) != 1:
            continue
        shadowed = set(_node_declared_parameters(function, source))
        shadowed.update(
            _text(name, source)
            for node in _walk(function.body) if node.type == "variable_declarator"
            if (name := node.child_by_field_name("name")) is not None and name.type == "identifier"
        )
        for node in _walk(function.body):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None or callee.type != "member_expression":
                continue
            receiver = callee.child_by_field_name("object")
            method = callee.child_by_field_name("property")
            if receiver is None or method is None or receiver.type != "identifier":
                continue
            alias, verb = _text(receiver, source), _text(method, source)
            if alias not in clients or alias in shadowed or verb.upper() not in _HTTP_METHOD_LITERALS:
                continue
            args = arguments.named_children
            url = _node_literal_http_url(args[0], source, constants) if args else None
            destination = literal_public_http_destination(url) if url is not None else None
            if destination is not None:
                scheme, host, port, target_path = destination
                calls.append(ExternalHttpCall(
                    function.symbol, scheme, host, port, verb.upper(), target_path, _evidence(path, root, node),
                ))
    return calls


_NEST_CACHE_DECORATORS = {
    "cache-manager.CacheKey": "CacheKey",
    "cache-manager.CacheTTL": "CacheTTL",
}

_NEST_RATE_LIMIT_DECORATORS = {"throttler.Throttle": "Throttle"}


def _node_nest_exception_filter_contracts(
    tree: Node, source: bytes, path: Path, root: Path, imports: tuple[tuple[str, str], ...],
) -> list[ErrorContract]:
    """Extract direct HTTP replies from literal Nest ``@Catch`` exception filters."""
    nest_imports = {
        local: imported.rsplit(".", 1)[-1]
        for local, imported in imports
        if imported.startswith("common.")
    }
    if "Catch" not in nest_imports.values():
        return []
    contracts: list[ErrorContract] = []
    for class_node in _walk(tree):
        if class_node.type != "class_declaration":
            continue
        class_name = class_node.child_by_field_name("name")
        class_body = class_node.child_by_field_name("body")
        if class_name is None or class_body is None:
            continue
        class_decorators = [
            *_preceding_decorators(class_node),
            *(child for child in class_node.named_children if child.type == "decorator"),
        ]
        if not any(
            _nest_direct_decorator(decorator, source, nest_imports, "Catch")
            for decorator in class_decorators
        ):
            continue
        for method in class_body.named_children:
            name = method.child_by_field_name("name") if method.type == "method_definition" else None
            body = method.child_by_field_name("body") if method.type == "method_definition" else None
            parameters = method.child_by_field_name("parameters") if method.type == "method_definition" else None
            if name is None or body is None or parameters is None or _text(name, source) != "catch":
                continue
            parameter_names = [
                _text(identifier, source)
                for parameter in parameters.named_children
                if (identifier := next((node for node in _walk(parameter) if node.type == "identifier"), None)) is not None
            ]
            if len(parameter_names) != 2 or parameter_names[0] not in _NODE_ERROR_IDENTIFIERS:
                continue
            response = re.search(
                rf"\b(?:const|let)\s+(?P<receiver>\w+)\s*=\s*{re.escape(parameter_names[1])}"
                r"\s*\.\s*switchToHttp\s*\(\s*\)\s*\.\s*getResponse\s*\(\s*\)",
                _text(method, source),
            )
            if response is None or response.group("receiver") not in _NODE_RESPONSE_PARAMETER_NAMES:
                continue
            function = _Function("catch", f"{_text(class_name, source)}.catch", body, method)
            contracts.extend(_node_http_response_contracts(
                function, path, root, source, response.group("receiver"),
            ))
    return contracts


def _node_nest_grpc_handlers(
    tree: Node, source: bytes, path: Path, root: Path, imports: tuple[tuple[str, str], ...],
) -> list[GrpcHandler]:
    """Return Nest gRPC handlers with explicit service and RPC literals only."""
    nest_imports = {
        local: imported.rsplit(".", 1)[-1]
        for local, imported in imports
        if imported.startswith("common.")
    }
    imported_names = dict(imports)
    if "Controller" not in nest_imports.values() or "microservices.GrpcMethod" not in imported_names.values():
        return []
    handlers: list[GrpcHandler] = []
    for class_node in _walk(tree):
        if class_node.type != "class_declaration":
            continue
        class_name = class_node.child_by_field_name("name")
        class_body = class_node.child_by_field_name("body")
        if class_name is None or class_body is None:
            continue
        if _nest_decorator_path(_preceding_decorators(class_node), source, nest_imports, "Controller") is None:
            continue
        decorators: list[Node] = []
        for child in class_body.named_children:
            if child.type == "decorator":
                decorators.append(child)
                continue
            if child.type != "method_definition":
                decorators.clear()
                continue
            grpc_method = _nest_grpc_method(list(decorators), source, imported_names)
            decorators.clear()
            name = child.child_by_field_name("name")
            if grpc_method is None or name is None:
                continue
            service, rpc = grpc_method
            handlers.append(GrpcHandler(
                service, rpc, f"{_text(class_name, source)}.{_text(name, source)}", _evidence(path, root, child),
            ))
    return handlers


def _node_nest_grpc_client_bindings(
    tree: Node, source: bytes, path: Path, root: Path, imports: tuple[tuple[str, str], ...],
) -> list[GrpcClientBinding]:
    """Return literal Nest ``ClientGrpc.getService`` assignments in Nest classes."""
    nest_imports = {
        local: imported.rsplit(".", 1)[-1]
        for local, imported in imports
        if imported.startswith("common.")
    }
    client_types = {local for local, imported in imports if imported == "microservices.ClientGrpc"}
    if not client_types or not {"Controller", "Injectable"}.intersection(nest_imports.values()):
        return []
    bindings: list[GrpcClientBinding] = []
    for class_node in _walk(tree):
        if class_node.type != "class_declaration":
            continue
        class_name = class_node.child_by_field_name("name")
        class_body = class_node.child_by_field_name("body")
        if class_name is None or class_body is None:
            continue
        class_decorators = _preceding_decorators(class_node)
        is_controller = _nest_decorator_path(class_decorators, source, nest_imports, "Controller") is not None
        is_injectable = any(
            _nest_direct_decorator(decorator, source, nest_imports, "Injectable")
            for decorator in class_decorators
        )
        if not is_controller and not is_injectable:
            continue
        clients = _node_constructor_members_of_types(class_body, source, client_types)
        if not clients:
            continue
        for node in _walk(class_body):
            if node.type != "assignment_expression":
                continue
            member = _node_this_member(node.child_by_field_name("left"), source)
            value = node.child_by_field_name("right")
            if member is None or value is None or value.type != "call_expression":
                continue
            function = value.child_by_field_name("function")
            arguments = value.child_by_field_name("arguments")
            args = arguments.named_children if arguments is not None else []
            if (
                function is None
                or len(args) != 1
                or not any(_text(function, source) == f"this.{client}.getService" for client in clients)
            ):
                continue
            service = _string(args[0], source)
            if service is not None:
                bindings.append(GrpcClientBinding(
                    _text(class_name, source), member, service, _evidence(path, root, node),
                ))
    return bindings


def _node_constructor_members_of_types(
    class_body: Node, source: bytes, accepted_types: set[str],
) -> set[str]:
    members: set[str] = set()
    for method in class_body.named_children:
        name = method.child_by_field_name("name") if method.type == "method_definition" else None
        if name is None or _text(name, source) != "constructor":
            continue
        parameters = method.child_by_field_name("parameters")
        if parameters is None:
            continue
        for parameter in parameters.named_children:
            if not any(child.type == "accessibility_modifier" for child in parameter.named_children):
                continue
            pattern = parameter.child_by_field_name("pattern")
            annotation = parameter.child_by_field_name("type")
            type_nodes = annotation.named_children if annotation is not None else []
            if (
                pattern is not None
                and pattern.type == "identifier"
                and len(type_nodes) == 1
                and type_nodes[0].type == "type_identifier"
                and _text(type_nodes[0], source) in accepted_types
            ):
                members.add(_text(pattern, source))
    return members


def _node_this_member(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "member_expression":
        return None
    object_node = node.child_by_field_name("object")
    property_node = node.child_by_field_name("property")
    if object_node is None or property_node is None or object_node.type != "this":
        return None
    return _text(property_node, source)


def _node_nest_constructor_injections(
    tree: Node, source: bytes, path: Path, root: Path, imports: tuple[tuple[str, str], ...],
) -> list[Injection]:
    """Return direct typed constructor members on literal Nest controllers/services.

    The member must be declared through a TypeScript accessibility modifier and a
    nominal type. Tokens, factories and ordinary constructor locals remain outside
    this structural subset.
    """
    nest_imports = {
        local: imported.rsplit(".", 1)[-1]
        for local, imported in imports
        if imported.startswith("common.")
    }
    if "Controller" not in nest_imports.values():
        return []
    injections: list[Injection] = []
    for class_node in _walk(tree):
        if class_node.type != "class_declaration":
            continue
        class_name = class_node.child_by_field_name("name")
        class_body = class_node.child_by_field_name("body")
        if class_name is None or class_body is None:
            continue
        class_decorators = _preceding_decorators(class_node)
        is_controller = _nest_decorator_path(class_decorators, source, nest_imports, "Controller") is not None
        is_injectable = any(
            _nest_direct_decorator(decorator, source, nest_imports, "Injectable")
            for decorator in class_decorators
        )
        if not is_controller and not is_injectable:
            continue
        for method in class_body.named_children:
            name = method.child_by_field_name("name") if method.type == "method_definition" else None
            if name is None or _text(name, source) != "constructor":
                continue
            parameters = method.child_by_field_name("parameters")
            if parameters is None:
                continue
            for parameter in parameters.named_children:
                if not any(child.type == "accessibility_modifier" for child in parameter.named_children):
                    continue
                pattern = parameter.child_by_field_name("pattern")
                annotation = parameter.child_by_field_name("type")
                type_nodes = annotation.named_children if annotation is not None else []
                if (
                    pattern is None
                    or pattern.type != "identifier"
                    or len(type_nodes) != 1
                    or type_nodes[0].type != "type_identifier"
                ):
                    continue
                injections.append(Injection(
                    f"{_text(class_name, source)}.{_text(pattern, source)}",
                    _text(type_nodes[0], source), None, _evidence(path, root, parameter),
                ))
    return injections


def _nest_http_entrypoint_functions(
    tree: Node, source: bytes, imports: tuple[tuple[str, str], ...],
) -> list[tuple[_Function, str, str, dict | None]]:
    """Extract literal Nest controller routes using imported decorator identities."""
    source_text = source.decode("utf-8", errors="ignore")
    nest_imports = nest_imports_for_source(source_text)
    decorator_imports = dict(imports)
    if "Controller" not in nest_imports.values():
        return []
    entrypoints: list[tuple[_Function, str, str, dict | None]] = []
    for class_node in _walk(tree):
        if class_node.type != "class_declaration":
            continue
        class_decorators = _preceding_decorators(class_node)
        controller_prefix = _nest_decorator_path(class_decorators, source, nest_imports, "Controller")
        if controller_prefix is None:
            continue
        class_name_node = class_node.child_by_field_name("name")
        class_body = class_node.child_by_field_name("body")
        if class_name_node is None or class_body is None:
            continue
        class_name = _text(class_name_node, source)
        controller_guards = _nest_route_decorator_registrations(
            class_decorators, source, nest_imports, "UseGuards", "controller",
        )
        controller_pipes = _nest_route_decorator_registrations(
            class_decorators, source, nest_imports, "UsePipes", "controller",
        )
        controller_cache = _nest_known_route_decorators(
            class_decorators, source, decorator_imports, _NEST_CACHE_DECORATORS, "controller",
        )
        controller_rate_limits = _nest_known_route_decorators(
            class_decorators, source, decorator_imports, _NEST_RATE_LIMIT_DECORATORS, "controller",
        )
        decorators: list[Node] = []
        for child in class_body.named_children:
            if child.type == "decorator":
                decorators.append(child)
                continue
            if child.type != "method_definition":
                decorators.clear()
                continue
            route_decorators = list(decorators)
            route = _nest_method_route(route_decorators, source, nest_imports)
            decorators.clear()
            if route is None:
                continue
            method_name = child.child_by_field_name("name")
            body = child.child_by_field_name("body")
            if method_name is None or body is None:
                continue
            name = _text(method_name, source)
            function = _Function(name, f"{class_name}.{name}", body, child)
            method, path = route
            guards = [
                *controller_guards,
                *_nest_route_decorator_registrations(
                    route_decorators, source, nest_imports, "UseGuards", "handler",
                ),
            ]
            pipes = [
                *controller_pipes,
                *_nest_route_decorator_registrations(
                    route_decorators, source, nest_imports, "UsePipes", "handler",
                ),
            ]
            cache_decorators = [
                *controller_cache,
                *_nest_known_route_decorators(
                    route_decorators, source, decorator_imports, _NEST_CACHE_DECORATORS, "handler",
                ),
            ]
            rate_limit_decorators = [
                *controller_rate_limits,
                *_nest_known_route_decorators(
                    route_decorators, source, decorator_imports, _NEST_RATE_LIMIT_DECORATORS, "handler",
                ),
            ]
            contract: dict = {}
            if guards:
                contract["route_guards"] = guards
            if pipes:
                contract["validation_pipes"] = pipes
            if cache_decorators:
                contract["cache_decorators"] = cache_decorators
            if rate_limit_decorators:
                contract["rate_limit_decorators"] = rate_limit_decorators
            if request := _nest_body_dto_contract(child, source, nest_imports):
                contract["request"] = request
            if parameters := _nest_bound_parameters(child, source, nest_imports):
                contract["parameters"] = parameters
            entrypoints.append((function, method, _join_route(controller_prefix, path), contract or None))
    return entrypoints


def _nest_grpc_method(
    decorators: list[Node], source: bytes, imports: dict[str, str],
) -> tuple[str, str] | None:
    for decorator in decorators:
        call = next((child for child in decorator.named_children if child.type == "call_expression"), None)
        if call is None:
            continue
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or arguments is None or imports.get(_text(function, source)) != "microservices.GrpcMethod":
            continue
        args = arguments.named_children
        if len(args) != 2:
            continue
        service, rpc = (_string(argument, source) for argument in args)
        if service is not None and rpc is not None:
            return service, rpc
    return None


def _nest_route_decorator_registrations(
    decorators: list[Node], source: bytes, imported_names: dict[str, str], expected: str, scope: str,
) -> list[dict[str, str]]:
    """Return direct identifiers registered by one imported Nest decorator.

    A literal registration is useful route context but does not prove behavior.
    Factories, inline expressions and indirect decorators are left out, so static
    output never invents security or validation semantics.
    """
    guards: list[dict[str, str]] = []
    for decorator in decorators:
        call = next((child for child in decorator.named_children if child.type == "call_expression"), None)
        if call is None:
            continue
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or arguments is None or imported_names.get(_text(function, source)) != expected:
            continue
        guards.extend(
            {"symbol": _text(argument, source), "scope": scope}
            for argument in arguments.named_children
            if argument.type == "identifier"
        )
    return guards


def _nest_known_route_decorators(
    decorators: list[Node], source: bytes, imports: dict[str, str], known: dict[str, str], scope: str,
) -> list[dict[str, str]]:
    """Return direct route decorators from a small, module-qualified allowlist."""
    registrations: list[dict[str, str]] = []
    for decorator in decorators:
        call = next((child for child in decorator.named_children if child.type == "call_expression"), None)
        if call is None:
            continue
        function = call.child_by_field_name("function")
        imported_name = imports.get(_text(function, source)) if function is not None else None
        if decorator_name := known.get(imported_name or ""):
            registrations.append({"decorator": decorator_name, "scope": scope})
    return registrations


def _nest_body_dto_contract(
    method: Node, source: bytes, imported_names: dict[str, str],
) -> dict[str, str | bool] | None:
    """Return one direct, whole-body Nest DTO declaration, when unambiguous.

    ``@Body() input: CreateOrderDto`` names the body DTO without claiming its
    fields or runtime validation. Property reads, decorator arguments, generics
    and multiple whole-body parameters are intentionally unresolved.
    """
    parameters = method.child_by_field_name("parameters")
    if parameters is None:
        return None
    body_parameters: list[dict[str, str | bool]] = []
    for parameter in parameters.named_children:
        decorators = [child for child in parameter.named_children if child.type == "decorator"]
        body_decorator = next(
            (
                decorator
                for decorator in decorators
                if _nest_direct_decorator(decorator, source, imported_names, "Body")
            ),
            None,
        )
        if body_decorator is None:
            continue
        call = next(child for child in body_decorator.named_children if child.type == "call_expression")
        arguments = call.child_by_field_name("arguments")
        pattern = parameter.child_by_field_name("pattern")
        annotation = parameter.child_by_field_name("type")
        type_nodes = annotation.named_children if annotation is not None else []
        if (
            arguments is None
            or arguments.named_children
            or pattern is None
            or pattern.type != "identifier"
            or annotation is None
            or len(type_nodes) != 1
            or type_nodes[0].type != "type_identifier"
        ):
            continue
        body_parameters.append({
            "name": _text(pattern, source),
            "type": _text(type_nodes[0], source),
            "required": parameter.type == "required_parameter",
        })
    return body_parameters[0] if len(body_parameters) == 1 else None


def _nest_bound_parameters(
    method: Node, source: bytes, imported_names: dict[str, str],
) -> list[dict[str, str | bool]]:
    """Return direct Nest path, query and header bindings with simple types only."""
    parameters = method.child_by_field_name("parameters")
    if parameters is None:
        return []
    bindings: list[dict[str, str | bool]] = []
    decorator_kinds = {"Param": "path", "Query": "query", "Headers": "header"}
    for parameter in parameters.named_children:
        parts = _nest_simple_parameter_parts(parameter, source)
        if parts is None:
            continue
        variable, type_name, required = parts
        candidates: list[tuple[str, str]] = []
        for decorator in (child for child in parameter.named_children if child.type == "decorator"):
            call = next((child for child in decorator.named_children if child.type == "call_expression"), None)
            if call is None:
                continue
            function = call.child_by_field_name("function")
            arguments = call.child_by_field_name("arguments")
            decorator_name = imported_names.get(_text(function, source)) if function is not None else None
            kind = decorator_kinds.get(decorator_name or "")
            args = arguments.named_children if arguments is not None else []
            name = _string(args[0], source) if len(args) == 1 else None
            if kind is not None and name is not None:
                candidates.append((kind, name))
        if len(candidates) != 1:
            continue
        kind, name = candidates[0]
        bindings.append({
            "kind": kind,
            "name": name,
            "variable": variable,
            "type": type_name,
            "required": True if kind == "path" else required,
        })
    return bindings


def _nest_simple_parameter_parts(parameter: Node, source: bytes) -> tuple[str, str, bool] | None:
    """Return an identifier plus a non-composite TypeScript annotation."""
    pattern = parameter.child_by_field_name("pattern")
    annotation = parameter.child_by_field_name("type")
    type_nodes = annotation.named_children if annotation is not None else []
    if (
        pattern is None
        or pattern.type != "identifier"
        or len(type_nodes) != 1
        or type_nodes[0].type not in {"predefined_type", "type_identifier"}
    ):
        return None
    return _text(pattern, source), _text(type_nodes[0], source), parameter.type == "required_parameter"


def _nest_direct_decorator(
    decorator: Node, source: bytes, imported_names: dict[str, str], expected: str,
) -> bool:
    call = next((child for child in decorator.named_children if child.type == "call_expression"), None)
    if call is None:
        return False
    function = call.child_by_field_name("function")
    return function is not None and imported_names.get(_text(function, source)) == expected


class StaticAnalysisEngine:
    """Facade selecting an AST analyzer for the supported service stack."""

    def __init__(
        self, depth_provider: DepthProvider | None = None,
        frontends: Mapping[str, LanguageFrontend] | None = None,
        flow_classifiers: Mapping[str, FlowClassifier] | None = None,
        framework_adapters: Mapping[str, FrameworkAdapter] | None = None,
    ) -> None:
        # Deferred: jvm_spring_analyzer imports this module for its shared Spring
        # helpers, so importing it back at module load time would be circular.
        from orbitkb.analysis.jvm_spring_analyzer import JvmSpringAnalyzer

        self._depth_provider = depth_provider or NoopDepthProvider()
        analyzers = {
            "go": (_GoAnalyzer(Language(tree_sitter_go.language())), ("*.go",)),
            "jvm-spring": (JvmSpringAnalyzer(), ("*.java", "*.kt")),
            "node-ts": (
                _NodeGraphqlAnalyzer(Language(tree_sitter_typescript.language_typescript())),
                ("*.js", "*.jsx", "*.ts", "*.tsx", "*.graphql", "*.gql", "*.prisma"),
            ),
            "node-js": (_NodeGraphqlAnalyzer(Language(tree_sitter_javascript.language())), ("*.js", "*.jsx", "*.graphql", "*.gql", "*.prisma")),
            "python": (_PythonAnalyzer(), ("*.py",)),
        }
        self._frontends: dict[str, LanguageFrontend] = {
            stack: AnalyzerFrontend(
                patterns, analyzer.analyze,
                frozenset({"messaging"}) if stack in BUILTIN_MESSAGING_STACKS else frozenset(),
            )
            for stack, (analyzer, patterns) in analyzers.items()
        }
        if frontends:
            self._frontends.update(frontends)
        self._flow_classifiers: dict[str, FlowClassifier] = {"jvm-spring": SpringDataClassifier()}
        if flow_classifiers:
            self._flow_classifiers.update(flow_classifiers)
        self._framework_adapters: dict[str, FrameworkAdapter] = {
            "jvm-spring": CombinedFrameworkAdapter((SpringFeignRecognizer(), SpringSecurityAdapter())),
        }
        if framework_adapters:
            self._framework_adapters.update(framework_adapters)

    def list_files(self, root: Path, stack: str) -> list[Path]:
        """The file listing alone -- a plain `rglob`, no parsing (see `analyze_files`)."""
        frontend = self._frontends.get(stack)
        if frontend is None:
            return []
        return self._source_files(root, frontend.file_patterns)

    def supports_stack(self, stack: str) -> bool:
        return stack in self._frontends

    def analyze_files(self, paths: list[Path], root: Path, stack: str) -> AnalysisResult:
        """Just the per-file AST pass, over exactly the given `paths` (a subset of
        `list_files`'s result is fine) -- no cross-file enrichment (see `enrich`).
        """
        frontend = self._frontends.get(stack)
        if frontend is None:
            return AnalysisResult()
        result = AnalysisResult()
        for path in paths:
            logger.debug("analyzing file: %s", path)
            result.extend(frontend.analyze_file(path, root))
        return result

    def enrich(self, result: AnalysisResult, files: list[Path], root: Path, stack: str) -> AnalysisResult:
        """Cross-file analysis that needs every file's result already merged (gRPC
        handler/client linking, Spring Data classification, contract enrichment, the
        bounded flow resolver...). Kept separate from `analyze_files` so a crash here
        -- it still touches tree-sitter for go's gRPC linking -- only costs this
        enrichment pass, not the per-file symbols/edges already collected.
        """
        frontend = self._frontends.get(stack)
        if frontend is not None:
            result.capabilities["messaging"] = "messaging" in frontend.supported_capabilities
        if stack == "python":
            seen_routes = {(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints}
            for route in proven_routes(files, root):
                key = ("http", route.method, route.path, route.symbol)
                if key in seen_routes:
                    continue
                seen_routes.add(key)
                evidence = Evidence(route.file.relative_to(root).as_posix(), route.line, route.line)
                result.entrypoints.append(EntryPoint("http", route.method, route.path, route.symbol, evidence))
        if stack in {"node-ts", "node-js"}:
            schema = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in files if path.suffix in {".graphql", ".gql"})
            result.contracts.update(_GraphqlContractExtractor().contracts(schema))
            resolve_node_router_mounts(result, files, root)
        classifier = self._flow_classifiers.get(stack)
        if classifier is not None:
            classifier.classify(result, files)
        if stack == "jvm-spring":
            result.grpc_handlers.extend(jvm_grpc_handlers(files, root))
            result.grpc_handlers.extend(kotlin_grpc_handlers(files, root))
            result.grpc_client_bindings.extend(jvm_grpc_client_bindings(files, root))
            result.grpc_client_bindings.extend(kotlin_grpc_client_bindings(files, root))
        if stack == "go":
            result.grpc_handlers.extend(_go_grpc_handlers(files, root))
            result.grpc_client_bindings.extend(_go_grpc_client_bindings(files, root))
        if stack == "jvm-spring":
            enrich_kotlin_expression_returns(result, files, root)
        _enrich_contract_fields(result.contracts, files)
        _enrich_rabbitmq_contracts(result.contracts, files)
        _enrich_openapi_contracts(result, root)
        _enrich_protobuf_contracts(result, root)
        _link_grpc_handlers(result)
        _link_grpc_client_calls(result)
        result.configuration_bindings.extend(_literal_configuration_bindings(result.symbols, root))
        result.persistence_facts.extend(_persistence_facts(files, root))
        result.migration_facts.extend(_migration_facts(_migration_files(root), root))
        result.cloud_facts.extend(detect_cloud_facts(files, root))
        if stack in {"node-ts", "node-js"}:
            _node_bind_consumer_argument_types(result, files, root, stack)
        result = BoundedFlowResolver().resolve(result)
        if stack in {"node-ts", "node-js"}:
            result.message_contracts.extend(_node_forwarded_kafka_topic_contracts(result, files, root, stack))
        adapter = self._framework_adapters.get(stack)
        if adapter is not None:
            adapter.enrich(result, files, root)
        result.edges.extend(self._depth_provider.enrich(root, result))
        return result

    def analyze(self, root: Path, stack: str) -> AnalysisResult:
        files = self.list_files(root, stack)
        result = self.analyze_files(files, root, stack)
        return self.enrich(result, files, root, stack)

    def input_digest(self, root: Path, stack: str) -> str | None:
        """Return a versioned digest of every local artifact this analyzer reads."""
        frontend = self._frontends.get(stack)
        if frontend is None:
            return None
        files = {
            *self._source_files(root, frontend.file_patterns),
            *_openapi_files(root),
            *_protobuf_files(root),
            *_migration_files(root),
        }
        digest = hashlib.sha256(f"{STATIC_ANALYSIS_INPUT_VERSION}:{stack}\0".encode("utf-8"))
        for path in sorted(files):
            relative_path = path.relative_to(root).as_posix()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _source_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
        return sorted({
            path
            for pattern in patterns
            for path in root.rglob(pattern)
            if not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
            and not _is_test_file(path)
        })


_REST_TEMPLATE_CALL_PATTERN = re.compile(
    r'\b(?P<receiver>\w+)\.(?P<operation>getForEntity|getForObject|postForEntity|postForObject|put|delete)'
    r'\s*\(\s*"(?P<url>https?://[^"]+)"',
)
_REST_TEMPLATE_EXCHANGE_PATTERN = re.compile(
    r'\b(?P<receiver>\w+)\.exchange\s*\(\s*"(?P<url>https?://[^"]+)"'
    r'\s*,\s*HttpMethod\.(?P<method>[A-Z]+)',
)
_WEB_CLIENT_CALL_PATTERN = re.compile(
    r'\b(?P<receiver>\w+)\.(?P<operation>get|post|put|patch|delete)\s*\(\s*\)'
    r'\s*\.uri\s*\(\s*"(?P<url>https?://[^"]+)"',
)
_WEB_CLIENT_METHOD_PATTERN = re.compile(
    r'\b(?P<receiver>\w+)\.method\s*\(\s*HttpMethod\.(?P<method>[A-Z]+)\s*\)'
    r'\s*\.uri\s*\(\s*"(?P<url>https?://[^"]+)"',
)
_WEB_CLIENT_REACTOR_REQUEST = (
    r'\b(?P<receiver>\w+)\.(?:get|post|put|patch|delete)\s*\(\s*\)'
    r'|\b(?P<method_receiver>\w+)\.method\s*\(\s*HttpMethod\.[A-Z]+\s*\)'
)
_WEB_CLIENT_REACTOR_TIMEOUT_PATTERN = re.compile(
    rf'(?:{_WEB_CLIENT_REACTOR_REQUEST})(?:(?!;).)*?\.timeout\s*\(\s*'
    r'Duration\.of(?P<duration_unit>Millis|Seconds|Minutes)\s*\(\s*(?P<value>\d+)\s*\)\s*\)',
    re.DOTALL,
)
_WEB_CLIENT_REACTOR_RETRY_PATTERN = re.compile(
    rf'(?:{_WEB_CLIENT_REACTOR_REQUEST})(?:(?!;).)*?\.retry\s*\(\s*(?P<value>\d+)\s*\)',
    re.DOTALL,
)
def _literal_internal_http_destination(url: str) -> tuple[str, str] | None:
    """Return a safe service host/path pair from a literal internal HTTP URL."""
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None or not re.fullmatch(r"[a-z][a-z0-9-]*", host, re.IGNORECASE):
        return None
    return host, parsed.path or "/"


def _spring_rest_template_service_calls(
    symbol: str,
    declaration: str,
    receivers: frozenset[str],
    path: Path,
    root: Path,
    node: Node,
) -> list[StaticServiceCall]:
    """Extract literal inter-service ``RestTemplate`` calls on injected members.

    A host must be a single service-like label. This excludes IPs, localhost,
    external domains and dynamic configuration rather than guessing their identity.
    Query text is deliberately not persisted.
    """
    calls = []
    for pattern, method_for in (
        (_REST_TEMPLATE_CALL_PATTERN, lambda match: _REST_TEMPLATE_METHODS[match.group("operation")]),
        (_REST_TEMPLATE_EXCHANGE_PATTERN, lambda match: match.group("method")),
    ):
        for match in pattern.finditer(declaration):
            method = method_for(match)
            if match.group("receiver") not in receivers or method not in _HTTP_METHOD_LITERALS:
                continue
            destination = _literal_internal_http_destination(match.group("url"))
            if destination is None:
                continue
            host, target_path = destination
            calls.append(StaticServiceCall(
                source=symbol,
                target_service=host,
                protocol="http",
                target_method=method,
                target_path=target_path,
                evidence=_declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
            ))
    return calls


def _spring_web_client_service_calls(
    symbol: str,
    declaration: str,
    receivers: frozenset[str],
    path: Path,
    root: Path,
    node: Node,
) -> list[StaticServiceCall]:
    """Extract literal ``WebClient`` verb/URI pairs on an injected client member."""
    calls = []
    for pattern, method_for in (
        (_WEB_CLIENT_CALL_PATTERN, lambda match: match.group("operation").upper()),
        (_WEB_CLIENT_METHOD_PATTERN, lambda match: match.group("method")),
    ):
        for match in pattern.finditer(declaration):
            method = method_for(match)
            if match.group("receiver") not in receivers or method not in _HTTP_METHOD_LITERALS:
                continue
            destination = _literal_internal_http_destination(match.group("url"))
            if destination is None:
                continue
            host, target_path = destination
            calls.append(StaticServiceCall(
                source=symbol,
                target_service=host,
                protocol="http",
                target_method=method,
                target_path=target_path,
                evidence=_declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
            ))
    return calls


def _spring_resilience_policies(
    symbol: str,
    declaration: str,
    modifiers: str,
    web_client_receivers: frozenset[str],
    path: Path,
    root: Path,
    node: Node,
) -> list[ResiliencePolicy]:
    """Extract only literal retry and timeout limits declared on a Spring method.

    The resulting fact describes the source method, not a runtime guarantee for
    each call inside it. Reactor limits must be on a chain started by an injected
    ``WebClient`` member; dynamic values and policy objects are intentionally
    omitted.
    """
    policies: list[ResiliencePolicy] = []
    for match in re.finditer(r'@Retryable\s*\([^)]*\bmaxAttempts\s*=\s*(?P<value>\d+)', modifiers):
        policies.append(ResiliencePolicy(
            source=symbol,
            kind="retry",
            mechanism="spring_annotation",
            value=int(match.group("value")),
            unit="attempts",
            evidence=_declaration_match_evidence(path, root, node, declaration, 0, 0),
        ))
    for pattern, kind, unit, scale in (
        (_WEB_CLIENT_REACTOR_TIMEOUT_PATTERN, "timeout", "milliseconds", {
            "Millis": 1, "Seconds": 1_000, "Minutes": 60_000,
        }),
        (_WEB_CLIENT_REACTOR_RETRY_PATTERN, "retry", "retries", None),
    ):
        for match in pattern.finditer(declaration):
            receiver = match.group("receiver") or match.group("method_receiver")
            if receiver not in web_client_receivers:
                continue
            value = int(match.group("value"))
            if scale is not None:
                value *= scale[match.group("duration_unit")]
            policies.append(ResiliencePolicy(
                source=symbol,
                kind=kind,
                mechanism="reactor",
                value=value,
                unit=unit,
                evidence=_declaration_match_evidence(path, root, node, declaration, match.start(), match.end()),
            ))
    return policies


def _enrich_contract_fields(contracts: dict[str, dict], files: list[Path]) -> None:
    shapes = _dto_shapes(files)
    for contract in contracts.values():
        for key in ("request", "returns"):
            value = contract.get(key)
            if value and (fields := shapes.get(value["type"])):
                value["fields"] = fields


_OPENAPI_FILENAMES = frozenset({
    "openapi.json", "openapi.yaml", "openapi.yml",
    "swagger.json", "swagger.yaml", "swagger.yml",
})
_OPENAPI_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


@dataclass(frozen=True)
class _OpenApiOperation:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    request_body_present: bool | None
    request_body_required: bool | None
    response_statuses: tuple[str, ...]
    security: str
    evidence: Evidence


def _enrich_openapi_contracts(result: AnalysisResult, root: Path) -> None:
    """Attach a declared OpenAPI operation only to an exact AST-proven endpoint."""
    operations_by_endpoint: dict[tuple[str, str], list[_OpenApiOperation]] = {}
    for operation in _openapi_operations(root):
        operations_by_endpoint.setdefault((operation.method, operation.path), []).append(operation)
    for entrypoint in result.entrypoints:
        operations = operations_by_endpoint.get((entrypoint.method, entrypoint.name), [])
        if entrypoint.kind != "http" or len(operations) != 1:
            continue
        operation = operations[0]
        contract = result.contracts.setdefault(entrypoint.symbol, {})
        contract["formal_contract"] = {
            "format": "openapi",
            "operation_id": operation.operation_id,
            "summary": operation.summary,
            "description": operation.description,
            "request_body_present": operation.request_body_present,
            "request_body_required": operation.request_body_required,
            "response_statuses": list(operation.response_statuses),
            "security": operation.security,
            "evidence": {
                "file": operation.evidence.file_path,
                "start_line": operation.evidence.start_line,
                "end_line": operation.evidence.end_line,
            },
        }


def _openapi_operations(root: Path) -> list[_OpenApiOperation]:
    operations: list[_OpenApiOperation] = []
    for path in _openapi_files(root):
        source = path.read_text(encoding="utf-8", errors="ignore")
        document = _load_openapi_document(path, source)
        if document is None:
            continue
        paths = document.get("paths")
        if not isinstance(paths, dict):
            continue
        for raw_path, path_item in paths.items():
            if not isinstance(raw_path, str) or not isinstance(path_item, dict):
                continue
            for raw_method, operation in path_item.items():
                method = raw_method.lower() if isinstance(raw_method, str) else ""
                if method not in _OPENAPI_HTTP_METHODS or not isinstance(operation, dict):
                    continue
                operation_id = operation.get("operationId")
                if not isinstance(operation_id, str):
                    operation_id = None
                operations.append(_OpenApiOperation(
                    method=method.upper(),
                    path=raw_path,
                    operation_id=operation_id,
                    summary=_openapi_operation_text(operation.get("summary")),
                    description=_openapi_operation_text(operation.get("description")),
                    request_body_present=_openapi_request_body_present(operation, path_item),
                    request_body_required=_openapi_request_body_required(operation, path_item),
                    response_statuses=_openapi_response_statuses(operation),
                    security=_openapi_security(operation, document),
                    evidence=_openapi_evidence(path, root, source, operation_id, raw_path),
                ))
    return operations


def _openapi_operation_text(value: object) -> str | None:
    return redact_sensitive_values(value.strip()) if isinstance(value, str) and value.strip() else None


def _openapi_files(root: Path) -> list[Path]:
    return sorted(
        path
        for pattern in ("*.json", "*.yaml", "*.yml")
        for path in root.rglob(pattern)
        if path.name.lower() in _OPENAPI_FILENAMES
        and not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
    )


def _load_openapi_document(path: Path, source: str) -> dict | None:
    try:
        document = json.loads(source) if path.suffix.lower() == ".json" else yaml.safe_load(source)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    if not isinstance(document.get("openapi"), str) and str(document.get("swagger")) != "2.0":
        return None
    return document


def _openapi_request_body_present(operation: dict, path_item: dict) -> bool | None:
    if "requestBody" in operation:
        request_body = operation["requestBody"]
        if not isinstance(request_body, dict) or "$ref" in request_body:
            return None
        return True
    if any("parameters" in container and not isinstance(container["parameters"], list)
           for container in (path_item, operation)):
        return None
    parameters = [*_openapi_parameters(path_item), *_openapi_parameters(operation)]
    if any(not isinstance(parameter, dict) or "$ref" in parameter for parameter in parameters):
        return None
    return any(isinstance(parameter, dict) and parameter.get("in") == "body"
               for parameter in parameters)


def _openapi_request_body_required(operation: dict, path_item: dict) -> bool | None:
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        if "$ref" in request_body:
            return None
        return request_body.get("required") is True
    parameters = [*_openapi_parameters(path_item), *_openapi_parameters(operation)]
    if any(isinstance(parameter, dict) and "$ref" in parameter for parameter in parameters):
        return None
    return any(
        isinstance(parameter, dict)
        and parameter.get("in") == "body"
        and parameter.get("required") is True
        for parameter in parameters
    )


def _openapi_parameters(container: dict) -> list:
    parameters = container.get("parameters")
    return parameters if isinstance(parameters, list) else []


def _openapi_response_statuses(operation: dict) -> tuple[str, ...]:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return ()
    return tuple(str(status) for status in responses)


def _openapi_security(operation: dict, document: dict) -> str:
    declaration = operation.get("security") if "security" in operation else document.get("security")
    if not isinstance(declaration, list):
        return "unspecified"
    return "required" if declaration else "not_required"


def _openapi_evidence(
    path: Path, root: Path, source: str, operation_id: str | None, operation_path: str,
) -> Evidence:
    path_position = source.find(operation_path)
    position = source.find(operation_id, path_position) if operation_id else path_position
    return _line_evidence(path, root, source, max(position, 0))


_PROTO_PACKAGE = re.compile(r"^\s*package\s+(?P<package>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;", re.MULTILINE)
_PROTO_IMPORT = re.compile(r'^\s*import\s+(?:(?:public|weak)\s+)?"(?P<path>[^"\\]+)"\s*;', re.MULTILINE)
_PROTO_SERVICE = re.compile(r"\bservice\s+(?P<service>[A-Za-z_]\w*)\s*\{")
_PROTO_RPC = re.compile(
    r"\brpc\s+(?P<rpc>[A-Za-z_]\w*)\s*\(\s*(?P<request_stream>stream\s+)?"
    r"(?P<request>\.?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\)\s*returns\s*\(\s*"
    r"(?P<response_stream>stream\s+)?(?P<response>\.?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\)\s*;",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ProtobufRpc:
    entrypoint: EntryPoint
    contract: dict


def _enrich_protobuf_contracts(result: AnalysisResult, root: Path) -> None:
    """Expose only uniquely declared, literal RPC signatures from local .proto files."""
    candidates = _protobuf_rpcs(root)
    counts: dict[str, int] = {}
    for candidate in candidates:
        symbol = candidate.entrypoint.symbol
        counts[symbol] = counts.get(symbol, 0) + 1
    for candidate in candidates:
        entrypoint = candidate.entrypoint
        if counts[entrypoint.symbol] != 1:
            continue
        result.entrypoints.append(entrypoint)
        result.contracts[entrypoint.symbol] = {"formal_contract": candidate.contract}


def _link_grpc_handlers(result: AnalysisResult) -> None:
    """Link one unique declared Protobuf RPC to one explicit static handler.

    This establishes source-level intent only. It does not claim a transport
    server, generated stubs or runtime registration is present.
    """
    declared = _declared_protobuf_rpc_entrypoints(result)
    handlers: dict[tuple[str, str], list[GrpcHandler]] = {}
    for handler in result.grpc_handlers:
        handlers.setdefault((handler.service, handler.rpc), []).append(handler)
    for key, rpc_entrypoints in declared.items():
        matching_handlers = [
            handler
            for (service, rpc), candidates in handlers.items()
            if service == key[0] and rpc.casefold() == key[1].casefold()
            for handler in candidates
        ]
        if len(rpc_entrypoints) != 1 or len(matching_handlers) != 1:
            continue
        entrypoint = rpc_entrypoints[0]
        handler = matching_handlers[0]
        confidence = "high" if handler.rpc == key[1] else "medium"
        result.edges.append(FlowEdge(entrypoint.symbol, handler.symbol, "invokes", handler.evidence, confidence))


def _link_grpc_client_calls(result: AnalysisResult) -> None:
    """Replace an unambiguous static gRPC stub call with its declared RPC symbol."""
    bindings: dict[tuple[str, str], list[GrpcClientBinding]] = {}
    for binding in result.grpc_client_bindings:
        bindings.setdefault((binding.owner, binding.member), []).append(binding)
    declared = _declared_protobuf_rpc_entrypoints(result)
    linked_edges: list[FlowEdge] = []
    for edge in result.edges:
        receiver, separator, rpc = edge.target.rpartition(".")
        owner = edge.source.split(".", 1)[0]
        matching_bindings = bindings.get((owner, receiver.removeprefix("this.")), []) if separator else []
        if len(matching_bindings) != 1:
            linked_edges.append(edge)
            continue
        binding = matching_bindings[0]
        resolved_rpc = _resolve_declared_protobuf_rpc(declared, binding.service, rpc)
        if resolved_rpc is None:
            linked_edges.append(edge)
            continue
        entrypoint, confidence = resolved_rpc
        linked_edges.append(replace(edge, target=entrypoint.symbol, confidence=confidence))
    result.edges = linked_edges


def _declared_protobuf_rpc_entrypoints(result: AnalysisResult) -> dict[tuple[str, str], list[EntryPoint]]:
    declared: dict[tuple[str, str], list[EntryPoint]] = {}
    for entrypoint in result.entrypoints:
        if entrypoint.kind != "grpc":
            continue
        contract = result.contracts.get(entrypoint.symbol, {}).get("formal_contract", {})
        if not isinstance(contract, dict):
            continue
        service = contract.get("service")
        rpc = contract.get("rpc")
        if isinstance(service, str) and isinstance(rpc, str):
            declared.setdefault((service, rpc), []).append(entrypoint)
    return declared


def _resolve_declared_protobuf_rpc(
    declared: dict[tuple[str, str], list[EntryPoint]], service: str, rpc: str,
) -> tuple[EntryPoint, str] | None:
    exact = declared.get((service, rpc), [])
    if len(exact) == 1:
        return exact[0], "high"
    camel_case_candidates = [
        entrypoint
        for (candidate_service, candidate_rpc), entrypoints in declared.items()
        if candidate_service == service and candidate_rpc.casefold() == rpc.casefold()
        for entrypoint in entrypoints
    ]
    return (camel_case_candidates[0], "medium") if len(camel_case_candidates) == 1 else None


def _protobuf_rpcs(root: Path) -> list[_ProtobufRpc]:
    rpcs: list[_ProtobufRpc] = []
    for path in _protobuf_files(root):
        source = path.read_text(encoding="utf-8", errors="ignore")
        without_comments = _mask_proto_comments(source)
        code = _mask_proto_strings(without_comments)
        package_match = _PROTO_PACKAGE.search(code)
        package = package_match.group("package") if package_match else None
        imports = [match.group("path") for match in _PROTO_IMPORT.finditer(without_comments)]
        for service_match in _PROTO_SERVICE.finditer(code):
            closing_brace = _matching_brace(code, service_match.end() - 1)
            if closing_brace is None:
                continue
            service = service_match.group("service")
            qualified_service = f"{package}.{service}" if package else service
            body = code[service_match.end() : closing_brace]
            for rpc_match in _PROTO_RPC.finditer(body):
                rpc = rpc_match.group("rpc")
                offset = service_match.end() + rpc_match.start()
                name = f"{qualified_service}.{rpc}"
                symbol = f"proto.{name}"
                evidence = _line_evidence(path, root, source, offset)
                rpcs.append(_ProtobufRpc(
                    entrypoint=EntryPoint("grpc", "RPC", name, symbol, evidence),
                    contract={
                        "format": "protobuf",
                        "package": package,
                        "service": service,
                        "rpc": rpc,
                        "request": {
                            "type": rpc_match.group("request"),
                            "streaming": rpc_match.group("request_stream") is not None,
                        },
                        "response": {
                            "type": rpc_match.group("response"),
                            "streaming": rpc_match.group("response_stream") is not None,
                        },
                        "imports": imports,
                        "evidence": {
                            "file": evidence.file_path,
                            "start_line": evidence.start_line,
                            "end_line": evidence.end_line,
                        },
                    },
                ))
    return rpcs


def _protobuf_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.proto")
        if not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
    )


def _mask_proto_comments(source: str) -> str:
    """Blank comments while preserving strings and line offsets in a .proto file."""
    masked = list(source)

    def blank(index: int) -> None:
        if masked[index] != "\n":
            masked[index] = " "

    index = 0
    while index < len(source):
        if source[index] == '"':
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == '"':
                    index += 1
                    break
                index += 1
        elif source.startswith("//", index):
            while index < len(source) and source[index] != "\n":
                blank(index)
                index += 1
        elif source.startswith("/*", index):
            while index < len(source) and not source.startswith("*/", index):
                blank(index)
                index += 1
            if index < len(source):
                blank(index)
                if index + 1 < len(source):
                    blank(index + 1)
                index += 2
        else:
            index += 1
    return "".join(masked)


def _mask_proto_strings(source: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"', lambda match: re.sub(r"[^\n]", " ", match.group()), source)


def _matching_brace(source: str, opening_brace: int) -> int | None:
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


_ENVIRONMENT_CONFIGURATION_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SPRING_VALUE_PROPERTY = re.compile(
    r'@Value\s*\(\s*(?:value\s*=\s*)?' + _spring_placeholder_literal("key") + r'\s*\)',
)
_SPRING_CONFIGURATION_PROPERTIES = re.compile(
    r'@ConfigurationProperties\s*\(\s*(?:(?:prefix|value)\s*=\s*)?'
    r'"(?P<prefix>[A-Za-z_][A-Za-z0-9_.-]{0,127})"\s*\)',
)


@dataclass(frozen=True)
class _CodeToken:
    kind: str
    text: str
    start: int


def _spring_value_property_binding(
    owner: str, member: str | None, declaration: str, evidence: Evidence,
) -> ConfigurationBinding | None:
    """Return an exact Spring ``@Value`` property binding, never resolved values.

    Spring permits SpEL and composed placeholders. Those forms do not identify a
    single configuration key locally, so only one literal ``${key}`` (optionally
    with a literal default) becomes an index fact.
    """
    match = _SPRING_VALUE_PROPERTY.search(declaration)
    if match is None or member is None:
        return None
    key = match.group("key")
    return ConfigurationBinding(
        source=f"{owner}.{member}",
        key=key,
        kind="property",
        sensitive=_SENSITIVE_CONFIGURATION_KEY.search(key) is not None,
        evidence=evidence,
    )


def _spring_configuration_properties_prefix(annotations: str) -> str | None:
    """Return one explicit Spring configuration-properties prefix, if present."""
    match = _SPRING_CONFIGURATION_PROPERTIES.search(annotations)
    return match.group("prefix") if match else None


def _configuration_properties_binding(
    prefix: str | None, owner: str, member: str | None, evidence: Evidence,
) -> ConfigurationBinding | None:
    """Build the canonical property key for one direct configuration member."""
    if prefix is None or member is None:
        return None
    key = f"{prefix}.{_canonical_spring_property_segment(member)}"
    return ConfigurationBinding(
        source=f"{owner}.{member}",
        key=key,
        kind="property",
        sensitive=_SENSITIVE_CONFIGURATION_KEY.search(key) is not None,
        evidence=evidence,
    )


def _canonical_spring_property_segment(member: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", member).replace("_", "-").lower()


def _literal_configuration_bindings(symbols: list[Symbol], root: Path) -> list[ConfigurationBinding]:
    """Extract literal environment and JVM-property reads inside local symbols only."""
    source_cache: dict[str, str] = {}
    go_os_imports_by_file: dict[str, bool] = {}
    bindings: list[ConfigurationBinding] = []
    seen: set[tuple[str, str]] = set()
    for symbol in symbols:
        file_path = symbol.evidence.file_path
        source = source_cache.setdefault(
            file_path, (root / file_path).read_text(encoding="utf-8", errors="ignore"),
        )
        if file_path not in go_os_imports_by_file:
            go_os_imports_by_file[file_path] = _has_standard_os_import(file_path, source)
        declaration, offset = _source_lines(source, symbol.evidence.start_line, symbol.evidence.end_line)
        for key, kind, position in _configuration_key_reads(
            _c_like_tokens(declaration), allow_go_os=go_os_imports_by_file[file_path],
        ):
            if (symbol.name, key) in seen:
                continue
            seen.add((symbol.name, key))
            bindings.append(ConfigurationBinding(
                source=symbol.name,
                key=key,
                kind=kind,
                sensitive=_SENSITIVE_CONFIGURATION_KEY.search(key) is not None,
                evidence=_line_evidence(root / file_path, root, source, offset + position),
            ))
    return bindings


def _source_lines(source: str, start_line: int, end_line: int) -> tuple[str, int]:
    lines = source.splitlines(keepends=True)
    offset = sum(len(line) for line in lines[: start_line - 1])
    return "".join(lines[start_line - 1 : end_line]), offset


def _has_standard_os_import(file_path: str, source: str) -> bool:
    if not file_path.endswith(".go"):
        return False
    tokens = _c_like_tokens(source)
    for index, token in enumerate(tokens):
        if token.text != "import":
            continue
        following = _token_at(tokens, index + 1)
        if following is not None and following.kind == "string" and following.text == "os":
            return True
        if following is not None and following.kind == "identifier":
            imported = _token_at(tokens, index + 2)
            if imported is not None and imported.kind == "string" and imported.text == "os":
                return True
        if following is not None and following.text == "(":
            cursor = index + 2
            while (candidate := _token_at(tokens, cursor)) is not None and candidate.text != ")":
                if candidate.kind == "string" and candidate.text == "os":
                    return True
                cursor += 1
    return False


def _configuration_key_reads(tokens: list[_CodeToken], allow_go_os: bool) -> list[tuple[str, str, int]]:
    reads: list[tuple[str, str, int]] = []
    for index, token in enumerate(tokens):
        previous_is_member = index > 0 and tokens[index - 1].text == "."
        if token.text == "process" and not previous_is_member:
            key_token = _node_environment_key(tokens, index)
            kind = "environment"
        elif token.text == "System" and not previous_is_member:
            key_token = _call_configuration_key(tokens, index, "getenv")
            kind = "environment"
            if key_token is None:
                key_token = _call_configuration_key(tokens, index, "getProperty")
                kind = "property"
        elif token.text == "os" and allow_go_os and not previous_is_member:
            key_token = _call_configuration_key(tokens, index, "Getenv", "LookupEnv")
            kind = "environment"
        else:
            key_token = None
            kind = None
        pattern = _ENVIRONMENT_CONFIGURATION_KEY if kind == "environment" else _PROPERTY_CONFIGURATION_KEY
        if key_token is not None and kind is not None and pattern.fullmatch(key_token.text):
            reads.append((key_token.text, kind, token.start))
    return reads


def _node_environment_key(tokens: list[_CodeToken], index: int) -> _CodeToken | None:
    if _token_texts(tokens, index, ("process", ".", "env", ".")):
        candidate = _token_at(tokens, index + 4)
        return candidate if candidate and candidate.kind == "identifier" else None
    if _token_texts(tokens, index, ("process", ".", "env", "[")):
        candidate = _token_at(tokens, index + 4)
        closing = _token_at(tokens, index + 5)
        return candidate if candidate and candidate.kind == "string" and closing and closing.text == "]" else None
    return None


def _call_configuration_key(tokens: list[_CodeToken], index: int, *names: str) -> _CodeToken | None:
    if not _token_texts(tokens, index, (tokens[index].text, ".")):
        return None
    operation = _token_at(tokens, index + 2)
    opening = _token_at(tokens, index + 3)
    candidate = _token_at(tokens, index + 4)
    if operation is None or opening is None or candidate is None:
        return None
    if operation.text not in names or opening.text != "(" or candidate.kind != "string":
        return None
    return candidate if _token_at(tokens, index + 5) and _token_at(tokens, index + 5).text == ")" else None


def _token_texts(tokens: list[_CodeToken], start: int, expected: tuple[str, ...]) -> bool:
    return tuple(token.text for token in tokens[start : start + len(expected)]) == expected


def _token_at(tokens: list[_CodeToken], index: int) -> _CodeToken | None:
    return tokens[index] if index < len(tokens) else None


def _c_like_tokens(source: str) -> list[_CodeToken]:
    """Tokenize identifiers and literal strings while discarding C-style comments."""
    tokens: list[_CodeToken] = []
    index = 0
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline + 1
        elif source.startswith("/*", index):
            closing = source.find("*/", index + 2)
            index = len(source) if closing < 0 else closing + 2
        elif source[index] in {"'", '"', "`"}:
            quote = source[index]
            start = index
            index += 1
            value_start = index
            escaped = False
            while index < len(source) and source[index] != quote:
                escaped = escaped or source[index] == "\\"
                index += 2 if source[index] == "\\" else 1
            if index >= len(source):
                continue
            value = source[value_start:index]
            index += 1
            if not escaped:
                tokens.append(_CodeToken("string", value, start))
        elif re.match(r"[A-Za-z_$]", source[index]):
            start = index
            index += 1
            while index < len(source) and re.match(r"[A-Za-z0-9_$]", source[index]):
                index += 1
            tokens.append(_CodeToken("identifier", source[start:index], start))
        else:
            tokens.append(_CodeToken("symbol", source[index], index))
            index += 1
    return tokens


def _dto_shapes(files: list[Path]) -> dict[str, list[dict]]:
    shapes: dict[str, list[dict]] = {}
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        shapes.update(_java_dto_shapes(source))
        if path.suffix == ".kt":
            shapes.update(kotlin_data_class_shapes(source))
        shapes.update(_go_dto_shapes(source))
    return shapes


def _enrich_rabbitmq_contracts(contracts: dict[str, dict], files: list[Path]) -> None:
    queues = _rabbitmq_queue_options(files)
    bindings = _rabbitmq_bindings(files)
    for contract in contracts.values():
        if contract.get("transport") != "rabbitmq" or contract.get("direction") != "consumes":
            continue
        if options := queues.get(contract["queue"]):
            contract.update(options)
        if queue_bindings := bindings.get(contract["queue"]):
            contract["bindings"] = queue_bindings


def _rabbitmq_queue_options(files: list[Path]) -> dict[str, dict]:
    options = {}
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'(?:assertQueue|durable)\s*\(\s*"([^"]+)"', source):
            queue = match.group(1)
            declaration = source[match.start() : match.start() + 600]
            values = _rabbitmq_option_values(declaration)
            if values:
                options[queue] = values
        for queue, values in _go_rabbitmq_queue_options(source).items():
            options.setdefault(queue, {}).update(values)
    return options


def _rabbitmq_option_values(source: str) -> dict:
    dead_letter = re.search(r'(?:deadLetterRoutingKey\s*\(\s*|deadLetterRoutingKey|x-dead-letter-routing-key)\s*["\':=]+\s*"([^"]+)"', source)
    retry = re.search(r'(?:messageTtl\s*\(\s*|messageTtl|x-message-ttl)\s*["\':=]+\s*(\d+)', source)
    values = {}
    if dead_letter:
        values["dead_letter_routing_key"] = dead_letter.group(1)
    if retry:
        values["retry_delay_ms"] = int(retry.group(1))
    return values


def _rabbitmq_bindings(files: list[Path]) -> dict[str, list[dict]]:
    bindings: dict[str, set[tuple[str, str]]] = {}
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        for queue, exchange, routing_key in _node_rabbitmq_bindings(source):
            bindings.setdefault(queue, set()).add((exchange, routing_key))
        for queue, exchange, routing_key in _spring_rabbitmq_bindings(source):
            bindings.setdefault(queue, set()).add((exchange, routing_key))
        for queue, exchange, routing_key in _go_rabbitmq_bindings(source):
            bindings.setdefault(queue, set()).add((exchange, routing_key))
    return {
        queue: [{"exchange": exchange, "routing_key": routing_key} for exchange, routing_key in sorted(values)]
        for queue, values in bindings.items()
    }


def _node_rabbitmq_bindings(source: str) -> list[tuple[str, str, str]]:
    return re.findall(
        r'(?:\w+\.)?bindQueue\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"', source,
    )


def _spring_rabbitmq_bindings(source: str) -> list[tuple[str, str, str]]:
    queues = _spring_rabbitmq_factories(source, "Queue")
    exchanges = _spring_rabbitmq_factories(source, r"(?:Topic|Direct|Fanout|Headers)?Exchange")
    bindings = []
    pattern = (
        r'BindingBuilder\s*\.\s*bind\s*\(\s*(\w+)\s*\(\s*\)\s*\)\s*'
        r'\.to\s*\(\s*(\w+)\s*\(\s*\)\s*\)\s*\.with\s*\(\s*"([^"]+)"'
    )
    for queue_factory, exchange_factory, routing_key in re.findall(pattern, source):
        queue = queues.get(queue_factory)
        exchange = exchanges.get(exchange_factory)
        if queue and exchange:
            bindings.append((queue, exchange, routing_key))
    return bindings


def _go_rabbitmq_bindings(source: str) -> list[tuple[str, str, str]]:
    channels = set(re.findall(r"\b(\w+)\s+\*?amqp\.Channel\b", source))
    pattern = r'(\w+)\.QueueBind\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"'
    return [
        (queue, exchange, routing_key)
        for receiver, queue, routing_key, exchange in re.findall(pattern, source)
        if receiver in channels
    ]


def _go_rabbitmq_queue_options(source: str) -> dict[str, dict]:
    channels = set(re.findall(r"\b(\w+)\s+\*?amqp\.Channel\b", source))
    pattern = r'(\w+)\.QueueDeclare\s*\(\s*"([^"]+)"\s*,.*?amqp\.Table\s*\{(.*?)\}\s*\)'
    return {
        queue: values
        for receiver, queue, table in re.findall(pattern, source, re.DOTALL)
        if receiver in channels and (values := _rabbitmq_option_values(table))
    }


def _spring_rabbitmq_factories(source: str, type_pattern: str) -> dict[str, str]:
    java_pattern = rf'\b(\w+)\s*\([^)]*\)\s*\{{\s*return\s+new\s+(?:\w+\.)?{type_pattern}\s*\(\s*"([^"]+)"'
    kotlin_pattern = rf'\bfun\s+(\w+)\s*\([^)]*\)\s*(?::\s*[\w.<>?]+\s*)?=\s*(?:\w+\.)?{type_pattern}\s*\(\s*"([^"]+)"'
    return {name: value for name, value in re.findall(java_pattern, source) + re.findall(kotlin_pattern, source)}


def _persistence_facts(files: list[Path], root: Path) -> list[PersistenceFact]:
    facts = []
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"@Entity\s+(?:@Table\s*\(\s*name\s*=\s*\"([^\"]+)\"\s*\)\s*)?(?:class|data\s+class)\s+(\w+)", source):
            name, owner = match.group(1) or match.group(2), match.group(2)
            facts.append(PersistenceFact(name, "sql_table", owner, _line_evidence(path, root, source, match.start())))
        for match in re.finditer(r'@Document\s*\(\s*(?:collection\s*=\s*)?["\']([^"\']+)["\']\s*\)\s*(?:data\s+)?class\s+(\w+)', source):
            collection, owner = match.groups()
            facts.append(PersistenceFact(collection, "document", owner, _line_evidence(path, root, source, match.start())))
        for match in re.finditer(r"type\s+(\w+)\s+struct\s*\{(.*?)\}", source, re.DOTALL):
            if 'gorm:"' in match.group(2):
                facts.append(PersistenceFact(match.group(1), "sql_table", match.group(1), _line_evidence(path, root, source, match.start())))
        if path.suffix in {".js", ".ts"}:
            models = list(local_mongoose_model_declarations(path).values())
            if (default := proven_default_mongoose_model_export(path)) is not None:
                models.append(default)
            if path.suffix == ".js" and (exported := proven_commonjs_mongoose_model_export(path)) is not None:
                models.append(exported)
            for name, collection, line in models:
                kind = "document" if collection else "mongoose_model"
                fact_name = collection or name
                if not any(
                    fact.name == fact_name and fact.kind == kind and fact.owner == name
                    and fact.evidence.file_path == path.relative_to(root).as_posix()
                    for fact in facts
                ):
                    facts.append(PersistenceFact(
                        fact_name, kind, name, Evidence(path.relative_to(root).as_posix(), line, line),
                    ))
        facts.extend(_prisma_persistence_facts(source, path, root))
    return facts


_SQL_IDENTIFIER = (
    r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)'
    r'(?:\s*\.\s*(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*))*'
)
_SQL_MIGRATION_OPERATIONS = (
    ("create_table", re.compile(
        rf"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<table>{_SQL_IDENTIFIER})", re.IGNORECASE,
    ), False, None),
    ("add_column", re.compile(
        rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>{_SQL_IDENTIFIER})\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>{_SQL_IDENTIFIER})",
        re.IGNORECASE,
    ), False, "column"),
    ("drop_column", re.compile(
        rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>{_SQL_IDENTIFIER})\s+DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?(?P<column>{_SQL_IDENTIFIER})",
        re.IGNORECASE,
    ), True, "column"),
    ("drop_table", re.compile(
        rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>{_SQL_IDENTIFIER})", re.IGNORECASE,
    ), True, None),
    ("create_index", re.compile(
        rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?{_SQL_IDENTIFIER}\s+ON\s+(?P<table>{_SQL_IDENTIFIER})",
        re.IGNORECASE,
    ), False, None),
)
_MIGRATION_DIRECTORY_NAMES = frozenset({"migration", "migrations", "changelog", "changelogs"})
_FLYWAY_FILENAME = re.compile(r"V\d+(?:_\d+)*__.+\.sql$", re.IGNORECASE)
_LIQUIBASE_DATABASE_CHANGELOG = re.compile(
    r"<(?:[A-Za-z_][A-Za-z0-9_-]*:)?databaseChangeLog\b", re.IGNORECASE,
)
_LIQUIBASE_CHANGESET = re.compile(
    r"<changeSet\b[^>]*>(?P<body>.*?)</changeSet\s*>", re.IGNORECASE | re.DOTALL,
)
_LIQUIBASE_SIMPLE_OPERATION = re.compile(
    r"<(?P<tag>createTable|dropTable|createIndex|dropColumn)\b(?P<attributes>[^>]*)/?>",
    re.IGNORECASE,
)
_LIQUIBASE_ADD_COLUMN = re.compile(
    r"<addColumn\b(?P<attributes>[^>]*)>(?P<body>.*?)</addColumn\s*>", re.IGNORECASE | re.DOTALL,
)
_LIQUIBASE_COLUMN = re.compile(r"<column\b(?P<attributes>[^>]*)/?>", re.IGNORECASE)
_LIQUIBASE_ATTRIBUTE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_LIQUIBASE_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$-]*(?:\.[A-Za-z_][A-Za-z0-9_$-]*)*",
)
_LIQUIBASE_SIMPLE_OPERATIONS = {
    "createtable": ("create_table", False, None),
    "droptable": ("drop_table", True, None),
    "createindex": ("create_index", False, None),
    "dropcolumn": ("drop_column", True, "columnname"),
}


def _migration_files(root: Path) -> list[Path]:
    """Select conventional SQL and Liquibase XML migration locations only."""
    return [
        path
        for pattern in ("*.sql", "*.xml")
        for path in root.rglob(pattern)
        if not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
        and (
            any(part.lower() in _MIGRATION_DIRECTORY_NAMES for part in path.relative_to(root).parts[:-1])
            or (path.suffix.lower() == ".sql" and _FLYWAY_FILENAME.fullmatch(path.name) is not None)
        )
    ]


def _migration_facts(files: list[Path], root: Path) -> list[MigrationFact]:
    facts_with_offsets: list[tuple[int, MigrationFact]] = []
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix.lower() == ".sql":
            facts_with_offsets.extend(_sql_migration_facts(source, path, root))
        elif _LIQUIBASE_DATABASE_CHANGELOG.search(source):
            facts_with_offsets.extend(_liquibase_migration_facts(source, path, root))
    return [fact for _offset, fact in sorted(facts_with_offsets, key=lambda item: (item[1].evidence.file_path, item[0]))]


def _sql_migration_facts(source: str, path: Path, root: Path) -> list[tuple[int, MigrationFact]]:
    facts: list[tuple[int, MigrationFact]] = []
    analyzable = _mask_sql_comments(source)
    for operation, pattern, destructive, column_group in _SQL_MIGRATION_OPERATIONS:
        for match in pattern.finditer(analyzable):
            column = _normalize_sql_identifier(match.group(column_group)) if column_group else None
            facts.append((match.start(), MigrationFact(
                operation=operation,
                table_name=_normalize_sql_identifier(match.group("table")),
                column_name=column,
                destructive=destructive,
                evidence=_line_evidence(path, root, source, match.start()),
            )))
    return facts


def _liquibase_migration_facts(source: str, path: Path, root: Path) -> list[tuple[int, MigrationFact]]:
    """Extract only literal Liquibase XML changes nested in declared change sets."""
    facts: list[tuple[int, MigrationFact]] = []
    analyzable = _mask_xml_comments(source)
    for change_set in _LIQUIBASE_CHANGESET.finditer(analyzable):
        body = change_set.group("body")
        body_offset = change_set.start("body")
        for match in _LIQUIBASE_SIMPLE_OPERATION.finditer(body):
            operation, destructive, column_attribute = _LIQUIBASE_SIMPLE_OPERATIONS[
                match.group("tag").lower()
            ]
            attributes = _liquibase_attributes(match.group("attributes"))
            table_name = _literal_liquibase_identifier(attributes.get("tablename"))
            column_name = (
                _literal_liquibase_identifier(attributes.get(column_attribute))
                if column_attribute
                else None
            )
            if table_name is None or (column_attribute and column_name is None):
                continue
            offset = body_offset + match.start()
            facts.append((offset, MigrationFact(
                operation=operation,
                table_name=table_name,
                column_name=column_name,
                destructive=destructive,
                evidence=_line_evidence(path, root, source, offset),
            )))
        for add_column in _LIQUIBASE_ADD_COLUMN.finditer(body):
            attributes = _liquibase_attributes(add_column.group("attributes"))
            table_name = _literal_liquibase_identifier(attributes.get("tablename"))
            if table_name is None:
                continue
            for column in _LIQUIBASE_COLUMN.finditer(add_column.group("body")):
                column_name = _literal_liquibase_identifier(
                    _liquibase_attributes(column.group("attributes")).get("name"),
                )
                if column_name is None:
                    continue
                offset = body_offset + add_column.start("body") + column.start()
                facts.append((offset, MigrationFact(
                    operation="add_column",
                    table_name=table_name,
                    column_name=column_name,
                    destructive=False,
                    evidence=_line_evidence(path, root, source, offset),
                )))
    return facts


def _liquibase_attributes(source: str) -> dict[str, str]:
    return {
        match.group("name").lower(): match.group("value").strip()
        for match in _LIQUIBASE_ATTRIBUTE.finditer(source)
    }


def _literal_liquibase_identifier(value: str | None) -> str | None:
    if value is None or _LIQUIBASE_IDENTIFIER.fullmatch(value) is None:
        return None
    return value


def _mask_xml_comments(source: str) -> str:
    """Blank XML comments while preserving line offsets for source evidence."""
    return re.sub(r"<!--.*?-->", lambda match: re.sub(r"[^\n]", " ", match.group()), source, flags=re.DOTALL)


def _mask_sql_comments(source: str) -> str:
    """Blank comments and single-quoted literals while preserving every line offset."""
    masked = list(source)

    def _blank(index: int) -> None:
        if masked[index] != "\n":
            masked[index] = " "

    index = 0
    while index < len(source):
        if source.startswith("--", index):
            while index < len(source) and source[index] != "\n":
                _blank(index)
                index += 1
        elif source.startswith("/*", index):
            _blank(index)
            _blank(index + 1)
            index += 2
            while index < len(source) and not source.startswith("*/", index):
                _blank(index)
                index += 1
            if index < len(source):
                _blank(index)
                if index + 1 < len(source):
                    _blank(index + 1)
                index += 2
        elif source[index] == "'":
            _blank(index)
            index += 1
            while index < len(source):
                _blank(index)
                if source[index] == "'":
                    if index + 1 < len(source) and source[index + 1] == "'":
                        _blank(index + 1)
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
        else:
            index += 1
    return "".join(masked)


def _normalize_sql_identifier(identifier: str) -> str:
    return ".".join(part.strip().strip('"`[]') for part in identifier.split("."))


def _prisma_persistence_facts(source: str, path: Path, root: Path) -> list[PersistenceFact]:
    providers = set(re.findall(r'datasource\s+\w+\s*\{[^}]*\bprovider\s*=\s*"([^"]+)"', source, re.DOTALL))
    kinds = {
        "postgresql": "sql_table", "mysql": "sql_table", "sqlite": "sql_table",
        "sqlserver": "sql_table", "cockroachdb": "sql_table", "mongodb": "document",
    }
    if len(providers) != 1 or (kind := kinds.get(next(iter(providers)))) is None:
        return []
    facts = []
    for match in re.finditer(r'model\s+(\w+)\s*\{(.*?)\}', source, re.DOTALL):
        owner, body = match.groups()
        mapping = re.search(r'@@map\s*\(\s*"([^"]+)"\s*\)', body)
        if mapping:
            facts.append(PersistenceFact(mapping.group(1), kind, owner, _line_evidence(path, root, source, match.start())))
    return facts


def _line_evidence(path: Path, root: Path, source: str, offset: int) -> Evidence:
    line = source.count("\n", 0, offset) + 1
    return Evidence(path.relative_to(root).as_posix(), line, line)


def _java_dto_shapes(source: str) -> dict[str, list[dict]]:
    shapes = {}
    field_pattern = (
        r"((?:\s*@\w+(?:\([^)]*\))?\s*)*)"
        r"([A-Z]\w*(?:<[^>]+>)?|boolean|byte|short|int|long|float|double|char)"
        r"\s+(\w+)\s*;"
    )
    for name, body in re.findall(r"\bclass\s+(\w+)[^{]*\{(.*?)\}", source, re.DOTALL):
        fields = []
        for field_annotations, type_name, field_name in re.findall(
            field_pattern, body,
        ):
            validations = re.findall(r"@(NotNull|NotBlank|NotEmpty|Positive|Negative|Size|Pattern)\b", field_annotations)
            fields.append({"name": field_name, "type": type_name, "required": bool(validations), "validations": validations})
        if fields:
            shapes[name] = fields
    return shapes


def _go_dto_shapes(source: str) -> dict[str, list[dict]]:
    shapes = {}
    for name, body in re.findall(r"\btype\s+(\w+)\s+struct\s*\{(.*?)\}", source, re.DOTALL):
        fields = []
        for field_name, type_name, tags in re.findall(r"(?m)^\s*(\w+)\s+([\w*\[\]]+)(?:\s+`([^`]*)`)?", body):
            json_name = re.search(r'json:"([^,"]+)', tags)
            validations = ["required"] if re.search(r'validate:"[^"]*\brequired\b', tags) else []
            fields.append({"name": json_name.group(1) if json_name else field_name, "type": type_name.lstrip("*"), "required": bool(validations), "validations": validations})
        if fields:
            shapes[name] = fields
    return shapes
