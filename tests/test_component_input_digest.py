from orbitkb.generation.input_digest import component_input_digest


def test_digest_changes_with_prompt_schema_and_backend_identity():
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    baseline = component_input_digest("model:v1", "Summarize A", schema)

    assert baseline is not None and len(baseline) == 64
    assert baseline == component_input_digest("model:v1", "Summarize A", schema)
    assert baseline != component_input_digest("model:v1", "Summarize B", schema)
    assert baseline != component_input_digest("model:v2", "Summarize A", schema)
    assert baseline != component_input_digest("model:v1", "Summarize A", {"type": "object"})
    assert component_input_digest(None, "Summarize A", schema) is None
