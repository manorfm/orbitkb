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


def test_evidence_source_packs_consecutive_lines_without_losing_text_or_pointers():
    first = CodeExcerpt("api.kt", 10, 10, "fun first() = 1")
    second = CodeExcerpt("api.kt", 11, 12, "fun second() = 2\nfun third() = 3")
    other = CodeExcerpt("other.kt", 1, 1, "fun other() = 4")

    source = EvidenceSource.from_excerpts([first, second, other], max_chars=500)

    assert source.prompt_text.count("--- api.kt") == 1
    assert "--- api.kt (lines 10-12) ---\nfun first() = 1\nfun second() = 2\nfun third() = 3" in source.prompt_text
    assert "--- other.kt (lines 1-1) ---" in source.prompt_text
    assert source.pointers == [
        {"file": "api.kt", "start_line": 10, "end_line": 10},
        {"file": "api.kt", "start_line": 11, "end_line": 12},
        {"file": "other.kt", "start_line": 1, "end_line": 1},
    ]


def test_evidence_source_packs_only_when_both_excerpts_fit_the_budget():
    first = CodeExcerpt("api.kt", 10, 10, "fun first() = 1")
    second = CodeExcerpt("api.kt", 11, 11, "fun second() = 2")
    budget = len("--- api.kt (lines 10-10) ---\nfun first() = 1\n")

    source = EvidenceSource.from_excerpts([first, second], max_chars=budget)

    assert "fun first() = 1" in source.prompt_text
    assert "fun second() = 2" not in source.prompt_text
    assert "... (truncated, excerpt budget reached)" in source.prompt_text
    assert source.pointers == [{"file": "api.kt", "start_line": 10, "end_line": 10}]


def test_aggregate_evidence_uses_separate_budgets_and_preserves_pointer_order():
    primary = CodeExcerpt("entity.py", 1, 2, "entity")
    config = CodeExcerpt("config.yml", 3, 4, "broker")

    source = AggregateEvidenceSource.from_groups([primary], [config], max_chars=50)

    assert "entity" in source.primary.prompt_text
    assert "broker" in source.config_text
    assert [pointer["file"] for pointer in source.pointers] == ["entity.py", "config.yml"]
    assert AggregateEvidenceSource.from_groups([primary], [], max_chars=50).config_text == "(none found)"
