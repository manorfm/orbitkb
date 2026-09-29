You are documenting one API endpoint of a microservice so other AI coding agents can
use it as lean architectural context instead of reading the whole codebase.

Service name: $service_name
Detected stack: $stack
Endpoint: $method $path

Use ONLY the evidence below. If something is not visible in the evidence, leave it out
rather than guessing.

Source excerpts for business behavior and payload details that static facts cannot establish
(handler and directly-called helpers, bounded and redacted):
$code_excerpts

Outbound-call hints (route-reachable calls are marked; other hints may or may not
relate to this endpoint — use judgment based on the handler code above):
$outbound_call_hints

Return:
- summary: one line describing what this endpoint does
- description: what it does and what it returns, in business terms
- response_shape: fields of the response payload, if visible in the evidence
- request_shape: fields of the request payload this endpoint accepts, if visible in
  the evidence, each with whether the handler treats it as required (e.g. accessed
  directly without a default/None check) or optional
- calls: any other service, queue or topic this endpoint calls or publishes to while
  handling a request, WHY it does so (business reason, e.g. "to charge the customer's
  card" or "to check current stock before confirming the order"), exactly what
  data it needs from (or sends to) that target, your own confidence (0-1) that
  this call and its reason are correctly attributed from the evidence above — lower it
  when the target name or business reason is only loosely implied rather than explicit
  — and target_kind: "internal" if this looks like a call to another service of this
  same system (bare service name, internal hostname/env var, RPC/service-discovery
  client), "external" if it's a recognizable third-party vendor/SaaS (a vendor SDK
  import, a public vendor API domain, vendor-specific auth), or "unknown" if the
  evidence doesn't make it clear either way
- validations: input validation and authorization rules this endpoint enforces
  (e.g. required auth header/role, field constraints)
