import pytest

from orbitkb.generation.policy import GenerationPolicy


@pytest.mark.parametrize(
    ("force", "is_new", "changed", "removed", "expected"),
    [
        (False, False, set(), set(), False),
        (True, False, set(), set(), True),
        (False, True, set(), set(), True),
        (False, False, {"entity.py"}, set(), True),
        (False, False, set(), {"entity.py"}, True),
        (False, False, {"unrelated.py"}, {"other.py"}, False),
    ],
)
def test_aggregate_generation_policy_preserves_file_change_rules(force, is_new, changed, removed, expected):
    policy = GenerationPolicy(force=force, is_new=is_new, changed=changed, removed=removed)

    assert policy.should_regenerate_aggregate({"entity.py"}) is expected
