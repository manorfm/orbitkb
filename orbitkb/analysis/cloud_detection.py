"""Deterministic cloud SDK operation resolution shared across languages.

For Go, JVM and Node, a `CloudFact` is now produced *inside* each analyzer's
own per-function walk in `engine.py` — the same place GORM/Mongoose/Prisma
calls are already reclassified — so it also gets a real `FlowEdge` whose
`source` is the containing function's symbol, visible in `trace_flow`/
`describe_entrypoint`. This module holds the shared, language-agnostic pieces
that makes possible: `ClientKind` (what a locally-declared client resolves
to), the per-language "declared client" resolvers reused by `engine.py`, and
`cloud_edge_kind_and_fact`, which turns an already-resolved call edge
(`receiver.method`) into a `FlowEdge.kind` override plus its matching
`CloudFact` in one step.

Python is the one exception: its analyzer (`_PythonCliAnalyzer`) has no
entrypoint/function-boundary machinery to attach a `FlowEdge` source to, so
`detect_cloud_facts` remains a flat, whole-file pass for it alone — a
`CloudFact` without a `FlowEdge`, same posture the v1 trade-off described for
every language before this integration.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from orbitkb.analysis.cloud_taxonomy import (
    AWS_SDK_GO_V2_METHODS,
    AWS_SDK_JAVA_V1_FQN,
    AWS_SDK_JAVA_V1_TYPES,
    AWS_SDK_JAVA_V2_FQN,
    AWS_SDK_JAVA_V2_TYPES,
    AWS_SDK_JS_V3_COMMANDS,
    AWS_SDK_JS_V3_MODULE_SERVICE,
    AWS_SDK_METHOD_TABLE,
    AWS_SERVICE_RESOURCE_TYPE,
    AZURE_BLOB_CLIENT_TYPES,
    AZURE_BLOB_JAVA_FQN,
    AZURE_BLOB_METHOD_TABLE,
    AZURE_EVENTHUB_JAVA_FQN,
    AZURE_EVENTHUB_JAVA_TYPES,
    AZURE_EVENTHUB_METHOD_TABLE,
    AZURE_SERVICEBUS_JAVA_FQN,
    AZURE_SERVICEBUS_JAVA_TYPES,
    AZURE_SERVICEBUS_METHOD_TABLE,
    BOTO3_SERVICE_LITERALS,
    CLOUD_FACTORY_METHOD_NAMES,
    GCP_PUBSUB_JAVA_FQN,
    GCP_PUBSUB_JAVA_TYPES,
    GCP_PUBSUB_METHOD_TABLE,
    GO_CLOUD_IMPORT_PATHS,
    NODE_STATEFUL_CLIENT_MODULES,
    NON_AWS_SERVICE_RESOURCE_TYPE,
)
from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.models import CloudFact, Evidence
from orbitkb.discovery.go_imports import parse_go_import_paths
from orbitkb.discovery.node_imports import parse_node_named_imports

# (provider, service_name, resource_type, sdk, operation lookup table) — what a
# locally-declared client variable/parameter/field resolves to. Consumed by
# engine.py's per-function walk via cloud_edge_kind_and_fact below.
ClientKind = tuple[str, str, str, str, "dict[str, tuple[str, str]]"]

_NODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}

# flow_edges.kind has no 'admin' value; an admin-kind cloud operation (e.g.
# SNS Subscribe) becomes a generic 'invokes' edge rather than a guessed one.
CLOUD_OPERATION_KIND_TO_FLOW_EDGE_KIND: dict[str, str] = {
    "publish": "publishes", "consume": "consumes", "read": "reads",
    "write": "writes", "admin": "invokes",
}


def cloud_edge_kind_and_fact(
    target: str, evidence: Evidence, declarations: dict[str, ClientKind],
) -> tuple[str | None, CloudFact | None]:
    """Given an already-resolved call edge's target (`receiver.method`, as
    every tree-sitter `_FileAnalyzer` already produces) and the declared-client
    table for its file, resolves both the `FlowEdge.kind` override and the
    matching `CloudFact` in one step — `(None, None)` when the receiver isn't
    a verified cloud client or the method isn't one of its known operations.
    Shared by every per-function "declared client -> method call" integration
    (JVM, Go, Node's Azure Blob) so a cloud call gets a real `FlowEdge` whose
    `source` is the containing function, the same way GORM/Mongoose calls do.
    """
    receiver, separator, method = target.rpartition(".")
    if not separator:
        return None, None
    if receiver not in declarations:
        # A receiver-qualified access (Go's `h.sqsClient` through a method
        # receiver, or `this.sqsClient` in JVM/Node) -- the declared client's
        # own bare identifier is still the call's last dotted segment; the
        # qualifying receiver in front of it isn't itself part of the proof.
        receiver = receiver.rsplit(".", 1)[-1]
        if receiver not in declarations:
            return None, None
    provider, service_name, resource_type, sdk, method_table = declarations[receiver]
    operation = method_table.get(method)
    if operation is None:
        return None, None
    operation_kind, canonical_operation = operation
    fact = CloudFact(
        provider=provider, resource_type=resource_type, service_name=service_name,
        operation=canonical_operation, operation_kind=operation_kind, sdk=sdk, target_name=None,
        evidence=evidence,
    )
    return CLOUD_OPERATION_KIND_TO_FLOW_EDGE_KIND[operation_kind], fact


def node_command_imports(source: str) -> dict[str, tuple[str, str]]:
    """Local identifier -> (module basename, original Command class name), for
    every named import from a recognized `@aws-sdk/client-*` package. AWS SDK
    v3's Command construction (`new SendMessageCommand(...)`) has no declared
    client to reclassify a call on — engine.py walks `new_expression` nodes
    directly and resolves them against this table instead."""
    return {
        local_name: (module_name, original_name)
        for local_name, module_name, original_name in parse_node_named_imports(source)
        if module_name in AWS_SDK_JS_V3_MODULE_SERVICE and original_name in AWS_SDK_JS_V3_COMMANDS
    }


# Local identifier -> the exact exported symbol name its own import resolved
# to, restricted to the module each type is really shipped from
# (NODE_STATEFUL_CLIENT_MODULES) — same "right name, verified import" gate
# node_command_imports already applies for AWS Commands, closing the same gap
# for every stateful client type this taxonomy recognizes.
def _node_stateful_client_import_names(source: str) -> dict[str, str]:
    return {
        local_name: original_name
        for local_name, module_name, original_name in parse_node_named_imports(source)
        if NODE_STATEFUL_CLIENT_MODULES.get(original_name) == module_name
    }


# type_name (the exported symbol, not a local alias) -> ClientKind, for every
# stateful client Node exposes directly. Azure Blob/Event Hub are called on
# directly once constructed; GCP Pub/Sub's `PubSub`/Azure `ServiceBusClient`
# are base clients whose *child* topic/sender reference (see
# _node_factory_chain_declarations below) is where the operation is actually
# called — both shapes start from the same "declared client" proof here.
_NODE_STATEFUL_CLIENT_KIND: dict[str, ClientKind] = {
    "BlobServiceClient": ("azure", "blob_storage", "object_storage", "azure-storage-blob", AZURE_BLOB_METHOD_TABLE),
    "BlobContainerClient": ("azure", "blob_storage", "object_storage", "azure-storage-blob", AZURE_BLOB_METHOD_TABLE),
    "BlobClient": ("azure", "blob_storage", "object_storage", "azure-storage-blob", AZURE_BLOB_METHOD_TABLE),
    "EventHubProducerClient": (
        "azure", "event_hub", NON_AWS_SERVICE_RESOURCE_TYPE["event_hub"], "azure-eventhub", AZURE_EVENTHUB_METHOD_TABLE,
    ),
    "PubSub": ("gcp", "pubsub", NON_AWS_SERVICE_RESOURCE_TYPE["pubsub"], "gcp-pubsub", GCP_PUBSUB_METHOD_TABLE),
    "ServiceBusClient": (
        "azure", "service_bus", NON_AWS_SERVICE_RESOURCE_TYPE["service_bus"], "azure-servicebus", AZURE_SERVICEBUS_METHOD_TABLE,
    ),
}

_NODE_STATEFUL_CLIENT_DECLARATION_RE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*new\s+(\w+)\s*\(")


def _node_direct_stateful_client_declarations(source: str) -> dict[str, ClientKind]:
    verified = _node_stateful_client_import_names(source)
    declarations: dict[str, ClientKind] = {}
    for identifier, local_type_name in _NODE_STATEFUL_CLIENT_DECLARATION_RE.findall(source):
        original_name = verified.get(local_type_name)
        kind = _NODE_STATEFUL_CLIENT_KIND.get(original_name) if original_name else None
        if kind is not None:
            declarations[identifier] = kind
    return declarations


# `const topic = pubsub.topic('orders')` / `const sender =
# serviceBusClient.createSender('q')` — a base client already resolved by
# _node_direct_stateful_client_declarations producing a *child* reference.
# Same propagation rule as Go's _go_factory_chain_declarations: only when the
# base identifier is already proven and the method is a known factory method
# for its (provider, service_name).
_NODE_FACTORY_CHAIN_RE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(\w+)\.(\w+)\(")


def _node_factory_chain_declarations(source: str, base_declarations: dict[str, ClientKind]) -> dict[str, ClientKind]:
    chained: dict[str, ClientKind] = {}
    for new_identifier, base_identifier, method in _NODE_FACTORY_CHAIN_RE.findall(source):
        kind = base_declarations.get(base_identifier)
        if kind is None:
            continue
        provider, service_name = kind[0], kind[1]
        if method in CLOUD_FACTORY_METHOD_NAMES.get((provider, service_name), frozenset()):
            chained[new_identifier] = kind
    return chained


def node_stateful_client_declarations(source: str) -> dict[str, ClientKind]:
    """Every locally resolvable "declared client -> method call" cloud
    binding in a Node/TS file: direct stateful clients (Azure Blob/Event Hub)
    plus, for GCP Pub/Sub and Azure Service Bus, the base client merged with
    its one-level factory-chained child references — the same "declared
    type -> method call" resolution JVM/Go use, just with an extra hop for
    the SDKs that need it."""
    declarations = dict(_node_direct_stateful_client_declarations(source))
    declarations.update(_node_factory_chain_declarations(source, declarations))
    return declarations


def _jvm_client_kind(type_name: str, imports: dict[str, str]) -> ClientKind | None:
    """Only trusts `type_name` once its own import resolves to the exact FQN
    the real SDK ships — a project's own unrelated `SqsClient` with no such
    import (or a different one) resolves to None here, not a false positive."""
    resolved_fqn = imports.get(type_name)
    if resolved_fqn is None:
        return None
    if resolved_fqn == AWS_SDK_JAVA_V2_FQN.get(type_name):
        service_name = AWS_SDK_JAVA_V2_TYPES[type_name]
        resource_type = AWS_SERVICE_RESOURCE_TYPE.get(service_name)
        return None if resource_type is None else ("aws", service_name, resource_type, "aws-sdk-java-v2", AWS_SDK_METHOD_TABLE)
    if resolved_fqn == AWS_SDK_JAVA_V1_FQN.get(type_name):
        service_name = AWS_SDK_JAVA_V1_TYPES[type_name]
        resource_type = AWS_SERVICE_RESOURCE_TYPE.get(service_name)
        return None if resource_type is None else ("aws", service_name, resource_type, "aws-sdk-java-v1", AWS_SDK_METHOD_TABLE)
    if resolved_fqn == AZURE_BLOB_JAVA_FQN.get(type_name):
        return ("azure", "blob_storage", "object_storage", "azure-storage-blob", AZURE_BLOB_METHOD_TABLE)
    if resolved_fqn == GCP_PUBSUB_JAVA_FQN.get(type_name):
        return ("gcp", "pubsub", NON_AWS_SERVICE_RESOURCE_TYPE["pubsub"], "gcp-pubsub", GCP_PUBSUB_METHOD_TABLE)
    if resolved_fqn == AZURE_SERVICEBUS_JAVA_FQN.get(type_name):
        return ("azure", "service_bus", NON_AWS_SERVICE_RESOURCE_TYPE["service_bus"], "azure-servicebus", AZURE_SERVICEBUS_METHOD_TABLE)
    if resolved_fqn == AZURE_EVENTHUB_JAVA_FQN.get(type_name):
        return ("azure", "event_hub", NON_AWS_SERVICE_RESOURCE_TYPE["event_hub"], "azure-eventhub", AZURE_EVENTHUB_METHOD_TABLE)
    return None


_JVM_CLIENT_TYPE_ALTERNATION = "|".join(
    re.escape(t) for t in sorted(
        {
            *AWS_SDK_JAVA_V1_TYPES, *AWS_SDK_JAVA_V2_TYPES, *AZURE_BLOB_CLIENT_TYPES,
            *GCP_PUBSUB_JAVA_TYPES, *AZURE_SERVICEBUS_JAVA_TYPES, *AZURE_EVENTHUB_JAVA_TYPES,
        },
        key=len, reverse=True,
    )
)
# Java: `TYPE name;` / Kotlin: `val name: TYPE` — declaration order is reversed
# between the two languages, so each gets its own pattern rather than one
# trying to cover both orders ambiguously.
_JAVA_FIELD_RE = re.compile(
    r"\b(?:private|protected|public)?\s*(?:final\s+)?(" + _JVM_CLIENT_TYPE_ALTERNATION + r")\s+(\w+)\s*[=;]"
)
_KOTLIN_FIELD_RE = re.compile(
    r"\b(?:private\s+|protected\s+|public\s+)?(?:val|var)\s+(\w+)\s*:\s*(" + _JVM_CLIENT_TYPE_ALTERNATION + r")\b"
)


def jvm_client_declarations(source: str) -> dict[str, ClientKind]:
    imports = parse_jvm_imports(source)
    declarations: dict[str, ClientKind] = {}
    for match in _JAVA_FIELD_RE.finditer(source):
        kind = _jvm_client_kind(match.group(1), imports)
        if kind is not None:
            declarations[match.group(2)] = kind
    for match in _KOTLIN_FIELD_RE.finditer(source):
        kind = _jvm_client_kind(match.group(2), imports)
        if kind is not None:
            declarations[match.group(1)] = kind
    return declarations


# Any package alias, not a fixed set — safety comes from verifying the
# alias's own import path against GO_CLOUD_IMPORT_PATHS below, not from
# constraining which alias spellings this regex will even consider.
_GO_CLIENT_PARAMETER_RE = re.compile(r"\b(\w+)\s+\*(\w+)\.Client\b")

# `topic := client.Topic("orders")` — a base client already resolved by
# _GO_CLIENT_PARAMETER_RE producing a *child* reference. Only propagates a
# ClientKind when `client` is already a proven declaration and `method` is a
# known factory method for its (provider, service_name) — an unrelated
# `x := y.Foo()` never matches since `y` won't be in the base declarations.
_GO_FACTORY_CHAIN_RE = re.compile(r"\b(\w+)\s*:?=\s*(\w+)\.(\w+)\(")


def _go_factory_chain_declarations(source: str, base_declarations: dict[str, ClientKind]) -> dict[str, ClientKind]:
    chained: dict[str, ClientKind] = {}
    for new_identifier, base_identifier, method in _GO_FACTORY_CHAIN_RE.findall(source):
        kind = base_declarations.get(base_identifier)
        if kind is None:
            continue
        provider, service_name = kind[0], kind[1]
        if method in CLOUD_FACTORY_METHOD_NAMES.get((provider, service_name), frozenset()):
            chained[new_identifier] = kind
    return chained


def go_client_declarations(source: str) -> dict[str, ClientKind]:
    import_paths = parse_go_import_paths(source)
    declarations: dict[str, ClientKind] = {}
    for identifier, package_alias in _GO_CLIENT_PARAMETER_RE.findall(source):
        resolved = GO_CLOUD_IMPORT_PATHS.get(import_paths.get(package_alias, ""))
        if resolved is None:
            continue
        provider, service_name = resolved
        if provider == "azure":
            declarations[identifier] = ("azure", "blob_storage", "object_storage", "azure-storage-blob", AZURE_BLOB_METHOD_TABLE)
        elif provider == "gcp" and service_name == "pubsub":
            declarations[identifier] = (
                "gcp", "pubsub", NON_AWS_SERVICE_RESOURCE_TYPE["pubsub"], "gcp-pubsub-go", GCP_PUBSUB_METHOD_TABLE,
            )
        else:
            resource_type = AWS_SERVICE_RESOURCE_TYPE.get(service_name)
            if resource_type is not None:
                declarations[identifier] = ("aws", service_name, resource_type, "aws-sdk-go-v2", AWS_SDK_GO_V2_METHODS)
    declarations.update(_go_factory_chain_declarations(source, declarations))
    return declarations


def _boto3_bound_variables(tree: ast.Module) -> dict[str, str]:
    """Local variable name -> service_name, for every `boto3.client(<literal>)`
    / `boto3.resource(<literal>)` assignment. The literal service string
    passed to boto3 is itself the proof of which AWS service it talks to."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in {"client", "resource"}
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "boto3"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            continue
        service_name = BOTO3_SERVICE_LITERALS.get(call.args[0].value)
        if service_name is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = service_name
    return bound


def _python_cloud_facts(source: str, rel_path: str) -> list[CloudFact]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    bound_services = _boto3_bound_variables(tree)
    facts: list[CloudFact] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in bound_services
            and node.func.attr in AWS_SDK_METHOD_TABLE
        ):
            continue
        service_name = bound_services[node.func.value.id]
        resource_type = AWS_SERVICE_RESOURCE_TYPE.get(service_name)
        if resource_type is None:
            continue
        operation_kind, operation = AWS_SDK_METHOD_TABLE[node.func.attr]
        end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
        facts.append(CloudFact(
            provider="aws", resource_type=resource_type, service_name=service_name,
            operation=operation, operation_kind=operation_kind, sdk="boto3",
            target_name=None,
            evidence=Evidence(file_path=rel_path, start_line=node.lineno, end_line=end_line),
        ))
    return facts


def detect_cloud_facts(files: list[Path], root: Path) -> list[CloudFact]:
    """Python only — Go/JVM/Node facts are now produced inside engine.py's own
    per-function walk (see module docstring), so they no longer flow through
    this flat, whole-file entry point."""
    facts: list[CloudFact] = []
    for path in files:
        if path.suffix != ".py":
            continue
        rel_path = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        facts.extend(_python_cloud_facts(text, rel_path))
    return facts
