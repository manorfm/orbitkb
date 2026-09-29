from orbitkb.discovery.base import CodeExcerpt
from orbitkb.generation.evidence import AggregateEvidenceSource, EvidenceSource


def test_evidence_source_tracks_only_excerpts_sent_to_the_model_and_redacts_secrets():
    first = CodeExcerpt("menu.py", 1, 3, "API_TOKEN = production-secret-value")
    second = CodeExcerpt("details.py", 5, 8, "load_menu()")
    source = EvidenceSource.from_excerpts([first, second], max_chars=70)

    assert "production-secret-value" not in source.prompt_text
    assert "[REDACTED]" in source.prompt_text
    assert "... (truncated, excerpt budget reached)" in source.prompt_text
    assert source.pointers == [{"file": "menu.py", "start_line": 1, "end_line": 3}]


def test_evidence_source_keeps_empty_fallback_and_no_pointers():
    source = EvidenceSource.from_excerpts([], max_chars=110)

    assert source.prompt_text == "(no excerpts found)"
    assert source.pointers == []


def test_aggregate_evidence_uses_separate_budgets_and_preserves_pointer_order():
    primary = CodeExcerpt("entity.py", 1, 2, "entity")
    config = CodeExcerpt("config.yml", 3, 4, "broker")

    source = AggregateEvidenceSource.from_groups([primary], [config], max_chars=50)

    assert "entity" in source.primary.prompt_text
    assert "broker" in source.config_text
    assert [pointer["file"] for pointer in source.pointers] == ["entity.py", "config.yml"]
    assert AggregateEvidenceSource.from_groups([primary], [], max_chars=50).config_text == "(none found)"
