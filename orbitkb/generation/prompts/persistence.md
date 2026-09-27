You are documenting what a microservice persists, so other AI coding agents can use it
as lean architectural context instead of reading the whole codebase.

Service name: $service_name
Detected stack: $stack

Use ONLY the evidence below.

Model/entity/migration excerpts found in this service:
$persistence_excerpts

Best-effort database engine guess, usually from the service's dependency manifest
rather than the model/entity definition itself (a starting point only — verify against
the actual evidence, it can be wrong):
$engine_hints

Configuration/manifest files found in this service, which may reveal the concrete
engine an ORM model doesn't name on its own:
$config_evidence

Return the list of entities/tables this service persists, with their fields (field name
plus a short type/meaning description) as visible in the evidence, and the concrete
database engine (postgres, mysql, mongodb, dynamodb, redis, elasticsearch, sqlite, or
"unknown" if nothing above resolves it — never guess). Skip anything not clearly shown
rather than guessing.

When a field is a foreign key to another entity you're also returning in this same
response (e.g. a `@ManyToOne`/`@JoinColumn`, a `ForeignKey(...)`, a `ref`/populated
relation), set its `references` to that entity's exact name plus whether the field is
also under a uniqueness constraint (one-to-one) or a regular foreign key
(many-to-one). Leave `references` out entirely when the target entity isn't one of
the ones returned here, or when the evidence doesn't clearly show a relationship.
