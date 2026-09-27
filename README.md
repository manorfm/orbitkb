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
| Go | HTTP handlers, calls, GORM and `database/sql`, RabbitMQ, Kafka (`segmentio/kafka-go`) | Dynamic routing and types remain unknown. |
| Java and Kotlin with Spring | HTTP, injection, repositories, JDBC, Mongo, RabbitMQ, Kafka (`KafkaTemplate`/`@KafkaListener`), scheduled jobs; Feign and injected `RestTemplate`/`WebClient` calls with literal internal service/route mappings; explicit method-level retry/timeout limits | Only unambiguous local wiring and literal limits are resolved; dynamic URLs, policies and values, IPs, localhost and external domains are not inferred. |
| Node and TypeScript | Express literal routes and direct route middleware registration, Fastify routes, NestJS controllers, typed constructor dependency flows, direct `@UseGuards`/`@UsePipes`, cache and throttling decorators, direct `@Body()` DTO types and literal path/query/header bindings, GraphQL, Mongoose, Prisma, RabbitMQ, Kafka (`kafkajs`) | Dynamic imports and runtime composition remain unknown. |
| GraphQL | Operations, local schema contracts, input/output shapes | Remote composition, directives and federation behavior are not inferred. |
| Persistence and messaging | Postgres/Mongo evidence, RabbitMQ bindings and contracts, Kafka producer/consumer contracts | Only literal, source-proven configuration is exposed; Kafka consumer detection is Go/JVM/Node only, no Python. |
| Cloud/infra | AWS (SQS, SNS, S3, EventBridge, Kinesis), Azure (Blob Storage, Service Bus, Event Hub) and GCP (Pub/Sub) call sites (Go, Java, Kotlin, Node/TS; Python is AWS-only via `boto3`), plus Terraform/CloudFormation/plain Kubernetes declarations, parsed with real grammars (`python-hcl2`, `cfn-flip`) — never keyword matching. A cloud call also produces a `FlowEdge`, visible in `trace_flow`/`describe_entrypoint`, for every language except Python. | GCS, Dockerfile, and unrendered Helm templates are not resolved; Azure Service Bus code facts can't distinguish queue from topic (defaults to queue — see `describe_cloud_dependencies`). |
| Runtime evidence | Normalized OTel or broker edges | Experimental; payloads, trace IDs and attributes are rejected. |

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

`export mermaid` writes `docs/topology.mmd` (system-wide service topology —
dependencies, external/cloud nodes, architecture cycles highlighted) and one
`docs/<service>.er.mmd` entity-relationship diagram per service, from detected
persistence facts. Mermaid renders natively in GitHub/GitLab/most editors, and
is plain text — versionable and diffable in a PR, unlike a generated image.

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
make benchmark-scale
make integration-containers
make readiness-audit
```

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

Incremental indexing skips unchanged LLM-derived units by file hash. Static analysis
also stores a versioned digest of every local artifact it reads (stack source files,
OpenAPI, Protobuf and supported migrations) and skips AST parsing, flow replacement
and reconstruction when that digest is unchanged. It still reads those inputs to
calculate the digest. `--force`, external depth enrichment, or a source change during
analysis bypasses or withholds snapshot reuse, preserving correctness over speed.

Architecture rules have fact-mutation tests for cycles and fan-out (both at
service level and their intra-service component analog), shared storage,
read-entrypoint side effects, RabbitMQ recovery-policy hypotheses, cloud
dependencies undeclared in IaC (or declared but unreferenced in code), and cloud
security/misconfiguration smells (missing dead-letter queue, public object
storage, missing encryption or bucket versioning). A component-level cycle or
fan-in/fan-out finding is scoped to one service's traced entrypoint-to-boundary
`flow_edges`, never a claim about that service's whole code graph. Static and
change-surface evaluations are deterministic regression checks; they do not
claim to measure an LLM's judgment on arbitrary codebases.

## Further reading

- [MCP interface contract](INTERFACE.md)
