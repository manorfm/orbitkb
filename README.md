# OrbitKB

<!-- BADGES:START -->
[![PyPI](https://img.shields.io/pypi/v/orbitkb.svg)](https://pypi.org/project/orbitkb/)
[![Python versions](https://img.shields.io/pypi/pyversions/orbitkb.svg)](https://pypi.org/project/orbitkb/)
[![CI](https://github.com/manorfm/orbitkb/actions/workflows/ci.yml/badge.svg)](https://github.com/manorfm/orbitkb/actions/workflows/ci.yml)
[![Security](https://github.com/manorfm/orbitkb/actions/workflows/security.yml/badge.svg)](https://github.com/manorfm/orbitkb/actions/workflows/security.yml)
[![License](https://img.shields.io/pypi/l/orbitkb.svg)](https://pypi.org/project/orbitkb/)
<!-- BADGES:END -->

OrbitKB is an MCP knowledge layer that gives coding agents the smallest useful,
evidence-backed view of a software change before they edit code.

It indexes one or many repositories into a cumulative SQLite knowledge base, then
answers questions such as: “What needs to change to add feature X?” Agents receive
bounded impact, flow, contract, dependency and risk context instead of searching a
whole repository or loading unrelated documentation.

> [!IMPORTANT]
> OrbitKB is not an autonomous coding agent and does not modify source code. It is
> also not a generic code graph or repository-wide RAG system. Its purpose is to
> give an agent precise context so the agent can plan and act safely.

## Why use it?

Large systems make seemingly small changes expensive to understand. A new feature can
cross an HTTP endpoint, application flow, database, broker, another service and an
external partner. The relevant knowledge is normally fragmented across source files,
repositories and people.

OrbitKB turns that into progressive, queryable context:

1. It extracts deterministic facts locally from source.
2. It stores concise service and contract knowledge with provenance.
3. An agent asks for a compact change briefing, then drills into only the endpoints,
   flows or dependencies that matter.
4. After delivery, Git-based verification and feedback can measure whether the
   predicted change surface was useful.

The result is not “more context”; it is less irrelevant context and a clearer record
of what is known, inferred, stale or still unknown.

## Quick start

Install the CLI:

```bash
pip install orbitkb
# or, for an isolated executable:
pipx install orbitkb
```

Add the `tokens` extra (`pip install "orbitkb[tokens]"`) for a reproducible
`tiktoken:o200k_base` token-budget measurement instead of the approximate default.

Index a repository, ask for a bounded implementation briefing, and inspect it with an
MCP client:

```bash
orbitkb index /path/to/shop --repository-name shop --backend codex

orbitkb context "Add Pix as a checkout payment method" \
  --repository shop \
  --max-services 3

orbitkb serve --backend codex
```

Every command has runnable help:

```bash
orbitkb --help
orbitkb index --help
```

The core workflow is **index → ask → verify**.

```bash
# After the implementation has landed, compare the predicted surface with Git.
orbitkb verify <run-id> --repository shop --since <commit>
```

## Connect an MCP client

```bash
orbitkb setup --repository shop --backend codex
```

Registers `orbitkb serve` as a stdio MCP server for Claude Code, Cursor and Codex
CLI in one step, by writing directly to each client's own config file
(`.mcp.json`, `.cursor/mcp.json`, `~/.codex/config.toml`) — never by shelling out to
a client's own `mcp add` CLI, since Cursor doesn't have one. The write is always
idempotent: an existing, matching entry is left alone, a conflicting one is reported
(with the exact snippet to paste manually) rather than overwritten, unless `--force`
is passed. `--client claude|cursor|codex` limits it to one client; without it,
`orbitkb setup` detects which clients are plausibly installed (an executable on
PATH, or that client's own config directory) and registers only those — falling
back to all three when nothing is detected. In a real terminal (never in a
script or when an agent shells out to this command) it offers a menu to confirm
the selection first; `--yes` skips that menu and proceeds with what was
detected. `--scope project|user` controls where Claude Code/Cursor register
(default: `project`, i.e. `.mcp.json` at the repository root, so anyone who
clones it inherits the config); `--dry-run` prints what would be written
without touching disk.

With `--repository`, `orbitkb setup` also:

- Installs a non-blocking `post-commit`/`post-merge` git hook that runs `orbitkb
  update --repository <name>` in the background after every commit or merge, so
  the index stays close to current without anyone remembering to run it by hand.
  It never overwrites a hook you already have (husky, pre-commit framework, a
  hand-written script) — it reports the conflict and gives you the exact block to
  paste in yourself instead.
- Writes an instruction block to both `AGENTS.md` and `CLAUDE.md` at the
  repository root, teaching any MCP-connected agent to check the `freshness`
  field before trusting `find_change_surface`/`plan_change`/`get_change_context`,
  and to run `orbitkb update --repository <name>` when it comes back stale. Both
  files get it (not just one) because Claude Code only falls back to `AGENTS.md`
  when a repository has no `CLAUDE.md` of its own.

Without `--repository`, `orbitkb setup` only registers the MCP clients
(`--scope` then defaults to `user`, since there's no repository root to anchor a
project-scoped file to) — useful for a first-time, machine-wide setup before
anything has been indexed yet.

Undo it with `orbitkb setup --remove --repository shop`. It always prints a
preview first; nothing is actually removed until you add `--yes`. Safety runs
both ways: an MCP entry is only removed when it still looks like some
`orbitkb serve` invocation (an entry you repurposed to run something else is
left alone and reported), and a hook or instruction block is only removed when
it still carries the marker `orbitkb setup` writes — a hook or `AGENTS.md`/
`CLAUDE.md` you already had of your own is never touched.

Prefer a manual, one-off configuration instead? A generic client configuration
looks like this:

```json
{
  "mcpServers": {
    "orbitkb": {
      "type": "stdio",
      "command": "orbitkb",
      "args": ["serve", "--backend", "codex"]
    }
  }
}
```

Use the absolute executable path if `orbitkb` is not on the PATH inherited by the
MCP client. Indexing and change-surface synthesis require an authenticated supported
headless backend. Read-only inspection of an existing database does not.

## What an agent can ask

Start broad, then narrow the request.

| Need | Start with | Follow with |
| --- | --- | --- |
| Start a change plan | `plan_change` | `refine_change_plan`, `describe_change_unit`, `assess_working_change` |
| Check supported static coverage | `describe_indexing_capabilities` | index the selected service |
| Plan an epic | `get_change_context` | `describe_service`, `describe_entrypoint` |
| Find likely impact | `find_change_surface` | `get_relationships`, `describe_api` |
| Understand a request path or its static error mapping | `list_entrypoints` | `describe_entrypoint` |
| Inspect data or events | `describe_persistence`, `describe_messages` | `get_relationships` |
| Inspect runtime configuration use | `describe_configuration` | `describe_entrypoint`, `list_security_findings` |

| Inspect Kubernetes configuration sources | `describe_runtime_configuration` | `describe_configuration`, `describe_entrypoint` |
| Inspect feature-flag reads | `describe_feature_flags` | `describe_entrypoint`, `list_security_findings` |
| Inspect cloud/infra dependencies | `describe_cloud_dependencies` | `find_architecture_smells` |
| Inspect indexed CI validation commands | `describe_ci_commands` | `plan_change`, `assess_working_change` |
| Report a persisted unit check | `describe_change_unit` | `record_change_unit_validation_result` |
| Find manual checks needing attention | `describe_change_plan_validation_status` | `describe_change_unit` |
| Review reported plan validation | `describe_change_validation_status` | `record_ci_validation_result`, `record_change_unit_validation_result` |
| Review advisory plan closure | `review_change_closure` | `describe_change_unit`, `assess_working_change` |
| Find architectural risks | `find_architecture_smells` | evidence and remediation in the finding |
| Compare runtime and static paths | `describe_runtime_divergence` | `describe_entrypoint` |

`describe_indexing_capabilities` lists Go, Java/Kotlin Spring, JavaScript, TypeScript,
and Python stacks. Each entry states supported entrypoint and error contract
types, known limits, and whether message contracts are analyzed. Python reports
messaging analysis as `unsupported` and has no static error contract protocol.
Go, Node and Spring expose source-proven message consumers; Spring also exposes
`@Scheduled(cron = "...")` jobs with literal cron values attached to methods. Property placeholders
and expressions are omitted; job concurrency and idempotency remain `unknown`.
Spring listener channels come from literal `queues` or `topics` on method annotations;
an `id` alone does not establish a channel. Dynamic message channels remain unknown.
Spring HTTP routes and Feign paths require literal method and class mappings;
unresolved path expressions are omitted.
Feign declarations and mappings inside comments or strings are ignored.
Feign calls need a literal service name and an unambiguous method mapping; a
dynamic name or conflicting overloaded mappings leave the destination unknown.
When Feign interfaces share a simple name, explicit imports or the consumer's
package determine which interface a call reaches; unresolved cases stay unknown.
Feign URL configuration bindings identify packaged interfaces by qualified name.
Named `@FeignClient` arguments can appear in either order.
An injected Feign client with a literal public HTTP URL records an external call
using that host and the combined URL and mapping path, including literal paths
containing parentheses. Feign method mappings also retain literal routes when
another mapping argument contains parentheses. Literal URLs that cannot
be classified do not produce an internal service call; property based URLs keep
their configuration binding and declared service name. URLs composed with dynamic
host or path expressions, constants, or concatenations do not establish a destination.
An empty URL uses the declared service name.

All MCP responses are structured JSON and use progressive disclosure: list a bounded
set, select one item, then ask for detail. The full per-tool contract — exact response
fields, pagination and filtering rules, ambiguity handling, and every `plan_change`
review-unit and `find_architecture_smells` finding this table's tools can return — is
documented in [INTERFACE.md](INTERFACE.md).

## What OrbitKB knows

OrbitKB keeps three kinds of information separate:

| Kind | Source | How to interpret it |
| --- | --- | --- |
| Static fact | Local source analysis | Deterministic structure with file and line evidence. |
| Semantic description | Bounded generation from redacted evidence | Useful interpretation, not a source fact. |
| Change inference | Task-specific analysis | A hypothesis with reason, confidence, evidence and unknowns. |

Unknown or ambiguous data stays explicit. OrbitKB does not choose an implementation
candidate silently, turn missing configuration into proof, or present an architecture
smell as a production defect.

### Source coverage

The supported deterministic subset is intentionally focused:

| Area | Current coverage | Boundary |
| --- | --- | --- |
| Go | HTTP handlers with explicit router methods, plus literal `net/http.HandleFunc` registrations with named handlers declared in the same file and a verified `net/http` import (`ANY` method); calls, GORM and `database/sql`, RabbitMQ, Kafka (`segmentio/kafka-go`) | Generic `router.Handle` is not assigned a method; dynamic routing and types remain unknown. |
| Java and Kotlin with Spring | HTTP, injection, repositories, JDBC, Mongo, RabbitMQ, Kafka (`KafkaTemplate`/`@KafkaListener`), scheduled jobs; Feign (keyword or positional `@FeignClient`, including its `url` as a configuration binding) and injected `RestTemplate`/`WebClient` calls with literal internal service/route mappings; explicit method-level retry/timeout limits; authorization requirements from `@PreAuthorize`/`@Secured` and a Kotlin `SecurityFilterChain`'s `authorizeHttpRequests` DSL, including one hop into a local zero-argument policy function for a custom `AuthorizationManager`, correlated to the specific route it governs (`describe_api`'s `api_shape.security`); literal `@RequestHeader`/`ResponseEntity` header names per endpoint (`api_shape.request/response.headers`) | Only unambiguous local wiring and literal limits are resolved; dynamic URLs, policies and values, IPs, localhost and external domains are not inferred. Java's `.requestMatchers(...).hasRole(...)` chain-style security DSL, method-level `@PreAuthorize`/`@Secured` route correlation, and other stacks' authorization/header patterns are not yet covered. |
| Python | CLI `main` functions; direct `FastAPI()` handlers and local or nested `APIRouter` handlers mounted with `include_router`, including package relative imports, literal prefixes and paths, synchronous or asynchronous handler calls, and calls to explicitly imported local functions or modules, including `from api import service` and `from . import service`. A router on a proven imported submodule can be mounted as `include_router(routes.router)`, including `import api.routes as routes` and `import api.routes`. Handler symbols include their source module path, so equal file names in different packages retain separate flows. | Routes enter a parent only when registered before its local mount. Dynamic paths and prefixes, indirect router exports, and Flask/Django static flows remain unresolved. Qualified calls without a proven local import remain unresolved boundaries. Package imports with an ambiguous initializer attribute remain unresolved. |
| Node and TypeScript | Express literal routes (including successive `app.route(path).all(middleware).get(handler).post(handler)` calls and a default import combined with named type imports), direct route middleware registration, and routers exported through CommonJS and mounted once through a literal relative import, including chained `router.route(path)` methods; Fastify direct routes and literal `route({ method, url, handler })` objects, exported or local NestJS controllers with imported decorators and literal `@Controller` prefixes, typed constructor dependency flows, direct `@UseGuards`/`@UsePipes`, cache and throttling decorators, direct `@Body()` DTO types and literal path/query/header bindings, GraphQL, Mongoose, Prisma, RabbitMQ, Kafka (`kafkajs`) | Express and Fastify route hints require a proven receiver and literal path. Handlers may be local named, assigned or inline functions, imports from one relative JS/TS file with a direct named function export, `export { handler }`, or a default export of a named, local or anonymous function, or a relative CommonJS `require` of a function exported through `module.exports`. Destructured `require` resolves local function properties of one literal `module.exports` object or direct top-level `exports.name`/`module.exports.name` assignments; mutations, calls receiving the exported object or an alias, literal containers passed to calls, and returning the exported object invalidate that proof. Using it only as a source of `Object.assign` is allowed. A relative CommonJS `require` of a single local class instance or a literal object of local functions links calls to the exported methods; a constant destructured `require` links proven exported functions. `this.method()` links only to a method of that same class. Kafka topics passed as string literals through unambiguous local parameter forwarding are associated with their call sites; dynamic topics remain unknown. Promise continuations chained from a call do not create flow edges; the original call and calls inside callbacks remain. Local or cross-file mount prefixes are included. Unmounted or ambiguously mounted routers produce no route hints. Dynamic imports and runtime composition remain unknown. |
| GraphQL | Operations, local schema contracts, input/output shapes | Remote composition, directives and federation behavior are not inferred. |
| Persistence and messaging | Postgres/Mongo evidence, RabbitMQ bindings and contracts, Kafka producer/consumer contracts | Only literal, source-proven configuration is exposed; Kafka consumer detection is Go/JVM/Node only, no Python. |
| Cloud/infra | AWS (SQS, SNS, S3, EventBridge, Kinesis), Azure (Blob Storage, Service Bus, Event Hub) and GCP (Pub/Sub) call sites (Go, Java, Kotlin, Node/TS; Python is AWS-only via `boto3`), plus Terraform/CloudFormation/plain Kubernetes declarations, parsed with real grammars (`python-hcl2`, `cfn-flip`) — never keyword matching. A cloud call also produces a `FlowEdge`, visible in `trace_flow`/`describe_entrypoint`, for every language except Python. | GCS, Dockerfile, and unrendered Helm templates are not resolved; Azure Service Bus code facts can't distinguish queue from topic (defaults to queue — see `describe_cloud_dependencies`). |

For Node/TypeScript, a Kafka consumer with one literal `subscribe` topic and
one `run` handler on the same `kafkajs` consumer in the same scope produces
a source-backed consume contract. The topology shows the topic as a channel;
ambiguous subscriptions or handlers are omitted.
Direct Kafka publish contracts require a producer constructed from an imported
`kafkajs` client, a literal topic, and an unshadowed producer receiver.
When bootstrap code passes one proven local CommonJS class instance to a
consumer function, the bounded message flow can follow calls on that parameter.
Conflicting or unknown arguments keep the receiver unresolved.
Calls on a local CommonJS import of a direct `mongoose.model(...)` export are
classified as persistence reads or writes when the export and import bindings
are proven. An unrelated module with the same method names remains an ordinary call.
An immutable local document returned by a proven `findOne` or `findById` call,
optionally followed by `sort`, also makes its direct `save()` call a write.
`lean()` results, unknown receivers and reassigned locals remain unresolved.
The proven query's `sort()` is treated as query composition, so it does not
appear as a separate call in the flow; the underlying read remains visible.
A direct CommonJS `mongoose.model("Name", schema)` export also appears in
`describe_persistence.static_facts` as a `mongoose_model` when no literal
collection is declared. `Name` identifies the logical model; the physical
collection remains unknown. Logical model names alone do not trigger a
shared-resource architecture finding.
Local JS/TS `const` and `export const` models use the same identity when their
factory comes from an explicit `mongoose` package import or `require`. A literal third argument
records the physical collection as a `document` fact; an unproven factory does
not establish a persistence fact or operation.
For operations on such a proven model, `describe_entrypoint.persistence_operations`
includes its declared `model` name. `collection` contains the literal physical
name only when that model has one matching declaration; otherwise it is `null`.
Instance `save()` inherits the model only when its local document origin is proven.
Named and default JS/TS ESM imports of a directly exported local Mongoose model
carry that identity into the importing module. A default export may contain the
model call or a stable local model constant. The import path, export and
unchanged alias must be proven. Local `export { Model }` and
`export { Model as default }` clauses are supported. A single named re-export
through another local file is resolved when its source is proven. Longer chains,
ambiguous names and external modules remain unresolved. `export *` carries a
model only when it is the barrel's sole export and its source has one direct,
proven model export; default and type-only exports do not qualify.

For Node/TypeScript, direct Axios calls in named functions and class methods
can expose an external host and path when the URL is literal or combines a
top-level constant URL with a literal path. Dynamic URLs, userinfo, query
strings and local IPs remain unresolved. These facts appear in
`describe_entrypoint.external_http_calls`, separate from internal `service_calls`.
| Runtime evidence | Normalized OTel or broker edges | Experimental; payloads, trace IDs and attributes are rejected. |

Kotlin/Spring flow tracing follows a uniquely resolved interface implementation through constructor injection, including controller → use case → gateway → Feign paths.
Spring route extraction includes multiline class `@RequestMapping` prefixes and handler mappings without a path argument, such as `@GetMapping` and `@GetMapping()`.
Java/Spring request and response DTO shapes include declared primitive fields as well as object fields.
Kotlin/Spring request and response DTO shapes include `val`/`var` properties
from data-class primary constructors, with nullable and defaulted fields
marked optional. A Kotlin expression body ending in a uniquely imported
extension can expose a candidate response DTO when that extension directly
constructs it. Its receiver is confirmed only when a unique injected
interface method declares the same non-null return type; otherwise the
candidate remains inferred and ambiguous for zero-call eligibility.
Feign interface methods remain outbound client declarations; their mappings are not generated as service entrypoints.

An optional external depth provider can enrich a selected flow when native resolution
is insufficient. It is bounded by timeout, edge budget, validation, cache and a local
circuit breaker; it never injects an opaque whole-repository graph.

## Cumulative knowledge across repositories

One database can accumulate independent repositories or a monorepo:

```bash
orbitkb index /work/checkout --repository-name checkout --backend codex
orbitkb index /work/payments --repository-name payments --backend codex
orbitkb list
```

A service identity is `(repository, name)`. When names collide, agents must pass the
repository to service-scoped tools. This avoids silently mixing two services named
`orders` from different repositories.

Re-index a single service with `orbitkb update <name>`, or every service in a
repository at once with `orbitkb update --repository <name>` — a service whose
root path no longer exists is reported and skipped, without aborting the rest.
To cap model attempts, pass `--max-llm-invocations N` to `index` or `update`.
The nonnegative limit applies separately to each service and counts retries;
`0` runs only deterministic work. When reached, indexing reports `partial`
and exits with an error code. A later run retries pending units while reusing
completed ones. This bounds calls, not tokens or the cost of an individual call.
`--max-reported-cost-usd N` also stops later attempts once cumulative reported
cost reaches the nonnegative per-service limit. If a call fails or omits cost,
later attempts are deferred because the remaining budget is unknown. A single
call can exceed the limit; use the invocation limit as a separate safeguard.
`--max-reported-tokens N` similarly uses the backend's reported input plus
output tokens per service. It does not add cached input tokens again. Missing
either count defers later attempts. Like the cost limit, it is checked between
calls and cannot cap tokens consumed by a call already running.

Re-indexing is authoritative for detected service boundaries. Removed services are
removed from the knowledge base; moved services keep their identity by name, while a
renamed service at the same root keeps its identity by root path. Repository removal
is explicit:

```bash
orbitkb remove --repository retired-service
```

## Generate documentation

Turn the indexed knowledge base into static docs or diagrams — no LLM call, no
extra cost beyond what `index`/`update` already paid for, useful for humans
reading outside an MCP client (a wiki page, a PR description, an architecture
review):

```bash
orbitkb export md --out docs/
orbitkb export mermaid --out docs/
```

`export md` writes, per indexed service, `docs/<service>/index.md` (description,
stack, dependencies with the reason and data each call needs, APIs, persistence,
messaging, cloud integrations) and one `docs/<service>/apis/<method-path>.md` per
detected API (response shape, calls it makes, validations/constraints).
API paths that normalize to the same filename receive distinct pages and links.
On reexport, a hidden manifest in each `apis/` directory removes obsolete pages
only when they still match OrbitKB's generated content. Edited and unrelated
files remain; a conflicting user page gets a different generated filename.
When service names repeat across repositories, both Markdown and Mermaid use
`docs/<repository>--<service>/` for those services. Unsafe or colliding directory
names receive a safe, unique suffix. `--service` selects every matching service.
API pages show the first source-proven route security rule that matches their
method and path. Method-level annotations are not correlated to API pages yet,
so this is a declared route rule, not a complete effective policy.
They also list source-proven request and response header names for that exact
method and path; header values are not captured or exported.
When a route's bounded static flow reaches an HTTP client, its API page lists
the declared operation even without a model-generated call. The target remains
marked unresolved. When an indexed HTTP call has the same target, its reason
and the source-proven operations appear together on one line.
If traversal reaches a limit or an unresolved flow boundary, the page says
that other calls may exist.
`describe_api` exposes source-proven operations in `source_calls`, separate
from generated `calls`, including when they share a target.
`source_calls_status` distinguishes `unassessed` (no matching source flow),
`assessed` (complete traversal), and `limited` (a traversal cap or unresolved
flow boundary). Markdown shows unknown calls when the source flow is unavailable.
A declared target is not a confirmed runtime address.
The service dependency list also includes source-proven HTTP client targets
from canonical or legacy static facts when no indexed call represents them,
labeled `unresolved` rather than treated as a confirmed destination.
An indexed call represents an HTTP target only when its call kind is `http`;
a queue or gRPC call to the same name does not hide the declared HTTP target.
`describe_service` exposes those targets separately in paginated `source_targets`;
its generated `calls` retain their existing meaning.
`source_targets_status` is `unassessed` when no canonical HTTP route is available,
`limited` when a route reaches an unresolved or bounded flow or an indexed API
has no matching canonical route, and `assessed` when all known HTTP routes are
traversed without limits. An empty target list therefore
does not by itself mean that the service has no outbound calls.
When a service has no canonical snapshot, its page reports that dependency
analysis is unavailable alongside any known targets.
When a snapshot has no canonical HTTP route, the Markdown dependency section
marks route flow as unassessed alongside any known targets.
If a route in the snapshot has unresolved or bounded flow, the dependency list
warns that other dependencies may exist alongside any known targets.
Indexed calls with an unresolved target carry the same label in service and API
Markdown pages and in the topology diagram.
The Messaging section also reports a source-confirmed Redis Pub/Sub publisher;
its channel remains unresolved when static analysis did not identify it.
The Persistence section likewise reports confirmed MongoDB template access
when no MongoDB entity is indexed; its collection remains unresolved.

`export mermaid` writes `docs/topology.mmd` (system-wide service topology —
dependencies, external/cloud nodes, architecture cycles highlighted) and one
`docs/<service>/er.mmd` entity-relationship diagram per service, from detected
persistence facts. Mermaid renders natively in GitHub/GitLab/most editors, and
is plain text — versionable and diffable in a PR, unlike a generated image.
Node identifiers remain distinct when different names normalize to the same
Mermaid slug, including entity names in ER diagrams; relationships still point
to the intended entity.
Fields that normalize to the same ER name also remain distinct, and their
relationship labels match the displayed field names.
ER attribute names and types that start with digits receive a letter prefix
required by Mermaid syntax; collisions after prefixing remain distinct.
ER relationship labels use the same normalized field names shown in entity
blocks, so indexed field text cannot add diagram statements.
Entrypoint sequence diagrams encode indexed participant and message labels,
including line breaks and semicolons, before writing Mermaid statements.
Services with the same name in different repositories are labeled with their
repository; calls and scoped topology queries retain their separate identities.
Cycle highlighting follows the same service identities.
Topology labels escape reserved characters from indexed names and channels so
they remain text within the intended node or edge.
When source analysis confirms a Spring Redis Pub/Sub publication, topology shows
a Redis node for that producer. It does not assign an unknown channel, consumer
or shared Redis instance.
Source-proven HTTP calls from canonical or legacy static facts also appear
with their declared target marked `unresolved` when no existing service call
has established the destination.
Confirmed outbound HTTP requests to literal external URLs appear as vendor host
nodes, with method and path on their edges. Repeated requests share the host
node, and these requests do not imply a link to another indexed service.
Confirmed publish and consume contracts appear as service-scoped channel nodes
with directed edges. A channel name alone does not establish a shared broker or
a link between services; channels already represented by indexed messages are
shown only once.
Confirmed calls on an injected Spring Mongo template add a per-service MongoDB
node labeled `accesses`; the diagram does not infer the collection or operation
direction from `MongoTemplate.execute`.

Both accept `--service <name>` to export just one service instead of every
indexed one.

## Safety and data handling

- Source and configuration evidence is redacted before generation, storage, logs and
  embeddings.
- Security findings retain locations and remediation, never secret values.
- Context-budget telemetry stores only metadata such as service IDs, counts, response
  size and a token estimate; it never stores task text, prompts, source or cards.
- Telemetry failures are non-blocking.
- Runtime ingestion accepts only normalized edge counters and rejects payloads and
  tracing attributes.

Feedback closes the loop without retaining sensitive text:

```bash
orbitkb context-feedback <run-id> insufficient --missing-services fraud-service
orbitkb context-metrics
orbitkb context-verify <run-id> --repository shop --since <commit>
```

The compact-context cap remains 1–5 service cards until historical evidence supports
a different policy.

## Operations

The default database is `~/.orbitkb/orbitkb.db`. Pass `--db <path>` to use another
knowledge base.

Use the local terminal monitor to inspect completed indexing totals, context-briefing
and change-plan counts, and any index run or bounded MCP operation currently active.
It opens the existing SQLite database in read-only mode and never starts a service,
indexes code, or retains new data:

```bash
orbitkb metrics
orbitkb metrics --watch --interval 1
orbitkb metrics --watch --alerts-only
```

`--watch` refreshes the terminal until `Ctrl+C`; it redraws only when persisted state
changes, except while an active operation needs its duration updated. When attached to
a terminal it uses color in addition to explicit status text. Use `--no-color` for
logs or plain output; `--interval` must be greater than zero in watch mode.
`ORBITKB_METRICS_INTERVAL` can supply the watch interval when `--interval` is
omitted; an explicit CLI value takes precedence, and either value must be finite and
greater than zero.
Optionally set `ORBITKB_METRICS_MIN_INTERVAL` as a finite positive safety floor. A
requested interval below it fails before the watch starts, including an explicit
`--interval`; it is never silently clamped.
`orbitkb metrics --help` also shows runnable examples for both environment variables.
In full `--watch` mode, the monitor prints `refresh: every … (CLI|environment|default)`
using the effective interval after those rules; `--alerts-only` omits it to keep the
filtered view focused. When `ORBITKB_METRICS_MIN_INTERVAL` is configured, the same
line adds `safety floor active` without repeating the floor value.
Within validation and plan quality, color is reserved for nonzero actionable states:
pending or stale items use yellow, while failures and confirmed breaks use red.
When plan quality has no actionable risk, the monitor collapses its detailed coverage,
contract and surface lines into `quality risks: none`; those details reappear as soon
as an actionable count is present.
When any follow-up is needed, an `Action needed` line appears at the top with compact
counts for validation, plan-review, closure and quality follow-ups. It identifies no
plan, repository, path, command or source content.
Use `--alerts-only` to show that summary and only the validation or plan-quality
sections with actionable counts; it prints `No alerts` when nothing needs follow-up.
With `--watch --alerts-only`, the terminal redraws only when that filtered alert view
changes, ignoring healthy totals and background activity that the view omits.
That filtered view ends with a UTC `updated:` timestamp for the last actual render;
the timestamp itself does not trigger periodic redraws.
During `orbitkb index` or `orbitkb update`, it also shows the current normalized
generation stage and completed/total unit count. It never displays source paths,
endpoint names, prompts or generated content.

The monitor also summarizes manual checks on ready plans as passed, pending or failed.
CI shows only agent-reported passed/failed results: it intentionally does not label a
workflow command as pending globally because a current indexed command cannot be
proven to belong to every plan.

After an explicit `review_change_closure`, the monitor also presents the latest
aggregate plan-quality result for that plan/repository: coverage counts, units omitted
or unassessable, files outside the planned surface, and public error-contract risks or
breaks. It retains only those counters and the advisory status, never a Git revision,
file path, diff, or change-unit ID.
Its `review coverage` line aggregates ready plans into reviewed and awaiting their
first closure review, so a zero-risk aggregate is not confused with a plan that has
never been reviewed. It also counts ready plans with a closure review potentially
stale because the repository was indexed afterwards; this is a prompt to reassess,
not a finding that a change is wrong.
An in-flight MCP operation records only its tool name, process ID and start time, then
removes that ephemeral row when it finishes; task text, arguments, code and responses
are never recorded for monitoring. If a local MCP process exits abruptly, the monitor
ignores its inactive PID and the next tracked operation prunes the abandoned row.

Back up before destructive maintenance:

```bash
orbitkb backup --out /safe/orbitkb-backup.db --db /path/orbitkb.db
orbitkb restore /safe/orbitkb-backup.db --db /path/orbitkb.db
```

SQLite backup and restore use its consistent backup API. Indexing is serialized per
`(repository, service)`; a concurrent attempt fails fast and a lock from a terminated
local process is recovered on the next attempt.

> [!WARNING]
> Shared multi-host SQLite is unsupported. Run one host/process domain per database,
> or use storage and coordination that provide distributed locking.

### Production readiness

OrbitKB is suitable for controlled production use when its support boundaries are
accepted and the target environment is validated. It is not an unconditional
production approval out of the box.

```bash
make readiness-audit
```

The audit verifies deterministic static and change-surface candidate corpora, then
returns `"status": "conditional"`. The remaining conditions are intentional:

- Run the opt-in container E2E in the target Docker or CI environment.
- Validate change-surface predictions against actual Git changes in representative
  repositories.
- Use the supported SQLite deployment model.
- Profile representative repositories before adding an AST cache or parallel index
  traversal.

The audit output contains only results and conditions; it does not include source,
prompts or secrets.

## Development and verification

From a source checkout:

```bash
pip install -e ".[dev]"
make verify
```

Useful focused checks:

```bash
make evaluate-static
make evaluate-change-surface
make validate-real-corpus CORPUS=/secure/path/real-change-corpus.json
make evaluate-real-corpus CORPUS=/secure/path/real-change-corpus.json
make gate-real-corpus CORPUS=/secure/path/real-change-corpus.json REQUIRED_STACKS="go node-ts" MIN_CASES_PER_STACK=10 MIN_PRECISION=0.80 MIN_RECALL=0.90
make readiness-audit-corpus CORPUS=/secure/path/real-change-corpus.json REQUIRED_STACKS="go node-ts" MIN_CASES_PER_STACK=10 MIN_PRECISION=0.80 MIN_RECALL=0.90
make benchmark-scale
make integration-containers
make readiness-audit
```

The frozen indexing baseline checks public MCP query fields and Markdown/Mermaid
exports using synthetic Python, JavaScript, TypeScript, Java/Spring,
Kotlin/Spring and Go fixtures.
The separate `verify/flow_corpus/sample-order-kotlin-service` sample exercises a
complex Kotlin/Spring route through interface injection, Feign, MongoDB,
Redis publication and route security without adding a service to the frozen
ShopFlow baseline. Its test records where source-backed navigation stops.
It also checks a seeded HTTP, vendor and messaging graph. It uses the mock backend
and makes no paid LLM calls:

```bash
python -m scripts.check_indexing_baseline
```

The snapshot in `tests/golden/indexing_baseline.json` contains public fixture
facts and export hashes, with no prompts, source excerpts or machine paths.
Real-provider cost, false positives and omissions are recorded as unknown.

`make integration-containers` starts ephemeral RabbitMQ, Postgres, MongoDB and
LocalStack containers, runs native operations against each (a real SQS
create-queue/send-message/receive-message round-trip for LocalStack), and checks
representative static contracts. It is opt-in locally and runs in CI. It is an
infrastructure smoke E2E, not a benchmark of a user's application or driver
compatibility.

`make benchmark-scale` reports median analysis time and peak traced Python memory for
generated Go handler corpora. For a comparable local baseline, change its inputs:

```bash
make benchmark-scale SCALE_FILES="5000" SCALE_REPEAT=5
```

### Opt-in real-change corpus

`make validate-real-corpus` validates an external JSON manifest before it is used as
quality evidence. The repository ships no production corpus, and this command neither
writes to SQLite nor calls an LLM. Keep the manifest in an access-controlled local or
CI location; do not commit it with source code.

Each case has schema version `1` and only the following structured metadata:

| Field | Purpose |
| --- | --- |
| `id`, `changed_unit_refs`, `predicted_unit_refs`, `contract_refs`, `migration_refs`, `test_refs` | Opaque lowercase references; maintain any mapping to real paths or symbols outside the manifest. |
| `task_digest`, `kb_snapshot_digest` | SHA-256 digests that correlate a redacted request and KB snapshot without retaining either. |
| `stack`, `size`, `criticality`, `decision_kinds` | Controlled categories for coverage and quality analysis. |

The validator rejects task text, prompts, descriptions, diffs, source, file paths,
symbols and decision text. This keeps the corpus suitable for measuring coverage by
stack while preserving the privacy boundary required for real changes.

When a reviewed case includes `predicted_unit_refs`, `make evaluate-real-corpus`
reports precision and recall per stack against its `changed_unit_refs`. Cases without
that field remain explicitly pending; they are never counted as empty predictions or
quietly treated as successful evidence.

To enforce a local/CI gate, use `make gate-real-corpus` and provide every target
explicitly: `REQUIRED_STACKS`, `MIN_CASES_PER_STACK`, `MIN_PRECISION` and
`MIN_RECALL`. The command fails on missing stack coverage, pending cases, or metrics
below the supplied limits. There are intentionally no default limits: teams must set
them from their own reviewed corpus rather than inheriting an arbitrary threshold.
`make readiness-audit-corpus` includes that same explicit gate in the readiness
report: a failed gate makes the audit `blocked`; no supplied corpus leaves the normal
audit `conditional` and records real-world validation as an outstanding condition.

Incremental indexing skips unchanged LLM-derived units by file hash. Static analysis
also stores a versioned digest of every local artifact it reads (stack source files,
OpenAPI, Protobuf and supported migrations) and skips AST parsing, flow replacement
and reconstruction when that digest is unchanged. It still reads those inputs to
calculate the digest. `--force`, external depth enrichment, or a source change during
analysis bypasses or withholds snapshot reuse, preserving correctness over speed.
An analyzer version change also rebuilds static facts on the next index or update;
unchanged documentation units do not need another LLM call.
Static source parsing now uses a per-language frontend contract for file selection
and per-file analysis. Existing parsers use the same contract, and the shared
enrichment and canonical projection continue after their results are combined.
Callers extending indexing can pass a configured `analysis_engine` to
`index_service`. Such runs reanalyze static source on each invocation because
the default cache digest does not identify custom parser implementations.
`index_path` also accepts `detectors` and `analysis_engine` for repository
discovery or an explicit stack override. An injected detector list replaces the
built-in list for that call; every detected stack must have a frontend before
indexing writes service data.
Cross-file Spring Security filter-chain rules run through a framework adapter
in that shared enrichment pass.
Feign calls and client URL property bindings now come from one Spring Feign
recognizer in the same pass; route joining remains shared across languages.
Spring Data derived methods and local `@Query` declarations are classified
through a per-language flow classifier before the shared flow resolver runs.
Other language frontends can register the same contract for source-proven flows.
Frontends declare whether they recognize messaging patterns. The canonical
snapshot keeps that capability, and `describe_messages` reports
`static_analysis_status` as `supported`, `unsupported`, or `unknown` when no
capability snapshot exists. An empty contract list only means no recognized
contracts; it does not prove that the service has no messaging integration.
The Python frontend reports messaging analysis as `unsupported`: it can record
a generic publish call, but does not extract source-proven message contracts.
Markdown service pages show the same status and use `(not assessed)` for empty
publish/consume sections when support is unavailable or unknown.
The canonical projection converts entrypoints, symbol declarations, injections,
flow boundary markers, flow edges, service calls,
configuration keys, security requirements, error contracts, message contracts,
persistence resources, migration declarations, gRPC bindings, resilience policies,
feature flags, cloud operations and HTTP header names from `AnalysisResult` into
language-neutral facts with stable IDs and source locations.
It keeps the known origin and confidence, and marks custom authorization logic
as unknown rather than interpreting its behavior.
Indexing stores this snapshot in SQLite together with static flow analysis, and
reuses it when inputs are unchanged. A missing snapshot triggers static
reanalysis without additional LLM calls. `describe_entrypoint` reads its
bounded flow, contract, reachable service calls, external HTTP calls, message
operations and contracts, error contracts, resilience policies and boundaries
from the canonical snapshot. A message channel is confirmed for a reached call
only when its contract shares that call's source location; unresolved publish
and consume calls report an unknown channel. A message consumption entrypoint
also exposes its matching consume contract with source evidence, without adding
a synthetic flow operation.
Other exports retain their current read paths.
`KnowledgeNavigator` can traverse a canonical snapshot from one indexed entrypoint
with explicit depth, node, edge and relation limits. It returns reached facts,
source-backed paths, unresolved or known boundaries, and truncation reasons.
The navigator is used by `describe_entrypoint` and `describe_error_flow`.
The latter follows caller and target error evidence through their canonical
snapshots and reports unresolved or truncated navigation in `unknowns`.
For Kotlin/Spring, explicitly imported extension calls on typed parameters
resolve to local declarations when the receiver has a unique matching symbol;
ambiguous declarations remain unresolved.
Kotlin and Java calls on explicitly typed method parameters also resolve to a
unique local method as possible paths; local variables without a proven type
remain unresolved.
For Kotlin, an immutable local assigned directly from a uniquely resolved method
with an explicit return type can also add an inferred flow path. Chained calls
and methods without a declared return type do not supply that link.
When a source location has exactly one call edge and one proven outbound service
call, navigation reports that edge as an external boundary. A different source
or competing edge remains unresolved.
Spring Data derived queries also retain their read/write classification when a
custom repository interface has one local Spring Data implementation. Another
local implementation leaves that classification unproven.
Confirmed Spring Data reads and writes appear as persistence boundaries in
canonical navigation; this does not assign a collection name to the operation.
Calls on injected Spring JDBC, Mongo and JPA templates use the same boundary
evidence. `MongoTemplate.execute` confirms a Mongo boundary while its read/write
direction stays unknown. Overloaded methods in one class are traversed as
possible paths with inferred confidence.
Calls to `convertAndSend` on an injected Spring `RedisTemplate` or
`StringRedisTemplate` appear as Redis Pub/Sub publish boundaries. A dynamic
channel or payload remains unknown unless separately proven by source evidence.
Kotlin construction of a class declared in the same file does not create a
call-flow boundary. If a function shares the class name, the call stays
unresolved rather than assuming construction.
An internal evidence composer can select route facts by profile for later prompt
reduction. It preserves each fact's source, route path, status and content digest,
and carries navigation boundaries and truncation forward without calling a model.
An evidence reducer combines multiple routes, sends a shared fact once, keeps each
route's path to that fact, and reports estimated characters and every fact omitted
by its budget. A limited source flow or budget omission marks the capsule truncated.
For HTTP routes, the capsule includes the first matching route security rule in
declaration order, with its source. Authorization remains ambiguous when security
evidence is omitted, navigation is limited, or a call is unresolved; omitting
only unrelated flow facts does not hide a confirmed authorization rule.
Endpoint generation now adds compact, route-reachable HTTP call evidence from the
canonical snapshot to its existing source excerpts and outbound hints. It labels
limited navigation or omitted calls instead of treating missing evidence as no call.
The endpoint indexer assesses canonical evidence by documentation dimension.
`IndexResult.sufficiency_shadow` counts assessed statuses for regenerated endpoints.
The assessment distinguishes a source-proven integration target from its business
purpose and exchanged data. A Feign destination alone leaves
`integration_purpose` ambiguous, so it cannot authorize zero-call generation.
For a route with no outbound calls, the integration dimensions become sufficient
only when the selected evidence covers flow and service calls, the entrypoint is
confirmed, messaging analysis is explicitly supported, and navigation has no
limits or boundaries. A partial evidence profile or missing capability fact
cannot prove an empty integration list.
The route capsule also carries frontend capability facts. If messaging analysis
is unsupported, an empty route flow leaves integrations `unsupported`; changing
that capability invalidates the prior route assessment during reindexing.
Source-proven publish or consume edges also prevent a "no outbound integration"
assessment; without a proven destination, the integration stays ambiguous and
keeps the edge as evidence.
An explicit OpenAPI operation description can satisfy the business behavior
dimension for its exact matching HTTP route. Missing or blank descriptions
remain missing; other incomplete dimensions still require generation.
The same assessment treats a bodyless OpenAPI operation as having a known empty
request payload. An optional or unresolved body still needs payload evidence.
If source code declares a body while OpenAPI declares none, the request shape is
ambiguous. A deterministic renderer produces schema-valid API details for a direct
public GET with an explicit OpenAPI summary and description, a typed response,
an exact public HTTP route rule, and a complete route flow without calls.
Computed security matchers are retained as unknown rules in declaration order;
a public wildcard rule alone does not qualify a route for deterministic output.
For regenerated routes, the indexer uses this document without an endpoint LLM
call when all conditions hold. Other routes keep the existing LLM path. Route
details report `render_status` as `used` or `ineligible`. Two source-reviewed
synthetic goldens verify all six API detail fields for Kotlin and Java examples.
Incremental indexing tracks OpenAPI and HTTP security files referenced by a
route. A contract or authorization change refreshes that endpoint and its
component and overview, even when the controller is untouched. An unrelated
operation or comment in a shared OpenAPI file does not regenerate the route.
Removing the contract or public rule sends the route back through LLM generation.
Removing a controller prunes its endpoint and refreshes the service overview;
files that merely leave the indexing scope are not counted as deleted files.
Failed endpoint generation is retried on the next run without another file edit.
When an endpoint's detailed document changes but the ordered route summaries
used by its component do not, indexing reuses the stored component summary and
refreshes its evidence without a component or overview model call. A failed
component generation is retried on the next run even without another edit.
Component reuse is keyed by a SHA-256 digest of the rendered prompt, response
schema and configured backend/model identity; only the digest is stored. Changing
any of these inputs refreshes the component. Backends without an explicit model
identity skip this reuse, and `--force` still regenerates it.
Indexing also fingerprints the source span of each method reached by a route.
One method fingerprint is shared across all routes that call it. Changing that
method refreshes those routes and includes its current bounded, redacted excerpt
in their prompts; changing an unrelated method in the same file does not refresh
them. The fingerprints contain no source text.
Run `PYTHONPATH=. .venv/bin/python scripts/report_shadow_eligibility.py` to
measure eligibility on the repository-owned synthetic corpus with the free mock
backend. The report contains counts by stack and renderer status. In the
current corpus, 2 of 21 regenerated routes use deterministic output:
one Kotlin route and one Java route, both with a confirmed public HTTP filter
rule. A restricted Java filter rule and a standalone method-level
`@PreAuthorize("permitAll()")` remain ineligible. The 47 reported `llm_calls`
are mock backend generations, not paid requests; two endpoint calls were avoided.
`quality_evaluated: false` means this synthetic report does not measure quality
on a real project.
Use `orbitkb index <path> --sufficiency-details` or
`orbitkb update <service> --sufficiency-details` to print one JSON line per
regenerated route with each dimension's status, reason and evidence IDs. An
`unassessed` route indicates that no matching canonical endpoint was available.
`describe_entrypoint.boundaries` also reports unresolved local symbols, cycles
and traversal limits alongside known static boundaries, with source locations
when the bounded edge is included.
The unhandled endpoint error finding also uses canonical reachability and
records traversal truncation in its `unknowns`.
Downstream error and retry findings scope target contracts through the same
snapshot when the target endpoint is indexed; otherwise they use service-wide
contracts with lower confidence. Truncated target flows are reported in `unknowns`.
Other public queries and exports retain their current read paths.
For Spring indexing, class-level `@RequestMapping` declarations are not separate
endpoint generation units; handler mappings create those units with the class path
prefix included.
Repeated hints for the same method and full path produce one API generation, with
evidence from every matching source retained. Different methods on the same path
remain separate APIs.
Index results report `llm_calls` as successfully generated units and
`llm_invocations` as actual backend attempts, including retries and failed units.
Older runs show `null` for invocations because that count was not recorded.
Reported usage totals remain unknown when any attempted call omitted that
measure; a partial sum is never displayed as a complete total. Older runs
cannot recover usage that their backends never reported.
`orbitkb status <service>` also shows generated units, backend invocations, reported
tokens and cost by generation kind. `backend_duration_ms` sums wall time spent in
backend calls for that kind, including failed attempts and retries; it excludes
discovery, prompt rendering, validation and database work. Older per-kind rows
show `None` for duration because it was not measured. These aggregates do not
store endpoint names, paths, prompts or source code; older runs have no per-kind
breakdown.
Use `orbitkb status <service> --units` to inspect each unit's `skipped`,
`success` or `failed` state, backend attempts, reported usage and duration.
Reported cache-read input tokens are shown per unit when available; historical
rows retain an unknown value.
The same view shows `prompt_chars`, the total characters in redacted prompts
submitted to the backend for that unit, including retries. Deterministic and skipped units
show zero; historical rows show `None`. This is a size measure, not a token or
cost estimate, and the prompt text is not stored in telemetry.
With `--units`, each run also shows the total measured characters and the count
of historical units without a measurement. Units appear from largest measured
prompt to smallest, with unknown sizes last, to help locate context-heavy units.
Unit keys are database-local HMAC values, stable across runs in that database;
the telemetry table contains no route, file, symbol, prompt or source text.
Runs recorded before per-unit telemetry have no unit rows.
Each planned generation slot uses an `IndexUnit` to track its identity, status,
attempts, token usage and backend time. The orchestrator aggregates these units
into service totals; skipped slots remain visible with zero attempts.
Component and overview prompts read previously generated API and component
summaries through a small `KnowledgeReader` contract; the default
`LegacyKnowledgeAdapter` reads the existing SQLite rows. Indexing keeps the
endpoint → component → overview order and accepts an injected reader for
alternate views or isolated tests.
Endpoint generation writes the API, its validations and outbound calls through
`KnowledgeWriter`; the default adapter uses the existing SQLite repositories.
Those three writes are atomic for each endpoint, including when indexing runs
inside an existing database transaction.
The reader obtains existing endpoint keys in one query per service before
deciding which routes need regeneration.
Generated component summaries and their evidence are also saved and pruned
through `KnowledgeWriter`. Component evidence follows the stored API summaries
used in its prompt, including summaries reused during incremental indexing.
If a summary has no source evidence, the component does not infer it from raw code.
The service overview descriptions are saved through the same writer after
component generation; optional embeddings still run only when the overview
is regenerated.
Persistence entities and evidence are replaced through `KnowledgeWriter`,
including an empty replacement when discovery finds no persistence hints.
Messaging contracts use the same writer and are cleared when no messaging
hints remain.
Persistence and messaging share one incremental `GenerationPolicy` for force,
new service and file change checks.
Endpoint, persistence and messaging evidence pointers include only excerpts
that fit in their rendered prompt sections. Main code and configuration have
separate excerpt budgets; text is redacted before it reaches the backend.
Consecutive excerpts from the same file share one prompt block while each
included excerpt keeps its own evidence pointer.

Architecture rules have fact-mutation tests for cycles and fan-out (both at
service level and their intra-service component analog), shared storage,
read-entrypoint side effects, RabbitMQ recovery-policy hypotheses, cloud
dependencies undeclared in IaC (or declared but unreferenced in code), and cloud
security/misconfiguration smells (missing dead-letter queue, public object
storage, missing encryption or bucket versioning). A component-level cycle or
fan-in/fan-out finding is scoped to one service's traced entrypoint-to-boundary
`flow_edges`, never a claim about that service's whole code graph. Static and
change-surface evaluations are deterministic regression checks; they do not
claim to measure an LLM's judgment on arbitrary codebases. `make evaluate-static`
also reports precision and recall per supported stack and fails when any stack
regresses, so a passing aggregate cannot mask a regression in another stack.

## Further reading

- [MCP interface contract](INTERFACE.md)
