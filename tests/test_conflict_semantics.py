"""CONFLICT-001: classify_conflict's four-dimension semantics, pinned.

C08 requires the four context dimensions (time / scope / environment /
definition) to be compared explicitly and honestly: a difference in context
means NEITHER value is automatically false, incomplete context defers to
review, and only identical explicit context with different values is a direct
contradiction. These tests pin that contract.
"""

from facai.trust import classify_conflict

CONTEXT_KEYS = ('time', 'scope', 'environment', 'definition')


def claim(value, **context):
    """A claim whose value and explicit context dimensions are set."""
    return {'subject': 'deploy-target', 'predicate': 'runs-version',
            'value': value, 'context': {key: context.get(key) for key in CONTEXT_KEYS}}


def full_context(**overrides):
    context = {'time': '2026-10-16T00:00Z', 'scope': 'prod',
               'environment': 'linux', 'definition': 'v1.0 release build'}
    context.update(overrides)
    return context


# --- not comparable ------------------------------------------------------------


def test_different_subjects_are_not_comparable():
    left = claim('1.0')
    right = {'subject': 'other-target', 'predicate': 'runs-version',
             'value': '2.0', 'context': full_context()}
    assert classify_conflict(left, right) is None


def test_different_predicates_are_not_comparable():
    left = claim('1.0')
    right = {'subject': 'deploy-target', 'predicate': 'bundles-size',
             'value': '2.0', 'context': full_context()}
    assert classify_conflict(left, right) is None


def test_identical_values_are_not_a_conflict():
    left = claim('1.0', **full_context())
    right = claim('1.0', **full_context())
    assert classify_conflict(left, right) is None


# --- contextual differences: neither side is false -----------------------------


def test_time_difference_is_temporal():
    left = claim('1.0', **full_context(time='2026-10-15T00:00Z'))
    right = claim('2.0', **full_context(time='2026-10-16T00:00Z'))
    result = classify_conflict(left, right)
    assert result['classification'] == 'TEMPORAL'
    assert 'time' in result['different_dimensions']
    assert 'neither value is automatically false' in result['reason']


def test_scope_difference_is_scope():
    left = claim('1.0', **full_context(scope='prod'))
    right = claim('2.0', **full_context(scope='staging'))
    assert classify_conflict(left, right)['classification'] == 'SCOPE'


def test_environment_difference_also_classifies_as_scope():
    """Pinned mapping: classify_conflict treats environment under the SCOPE
    kind (the claim's applicability domain), keeping TEMPORAL and SEMANTIC
    for time and definition. If this mapping ever changes deliberately, the
    registry note must change with it."""
    left = claim('1.0', **full_context(environment='linux'))
    right = claim('2.0', **full_context(environment='windows'))
    assert classify_conflict(left, right)['classification'] == 'SCOPE'


def test_definition_difference_is_semantic():
    left = claim('1.0', **full_context(definition='release build'))
    right = claim('2.0', **full_context(definition='debug build'))
    assert classify_conflict(left, right)['classification'] == 'SEMANTIC'


def test_multiple_differences_report_all_and_classify_by_first_dimension():
    left = claim('1.0', **full_context(time='t1', scope='s1'))
    right = claim('2.0', **full_context(time='t2', scope='s2', definition='d2'))
    result = classify_conflict(left, right)
    assert result['classification'] == 'TEMPORAL'  # CONTEXT_KEYS order wins
    assert set(result['different_dimensions']) == {'time', 'scope', 'definition'}


# --- incomplete context defers to review ---------------------------------------


def test_a_present_dimension_difference_decides_even_if_the_other_side_has_gaps():
    """A difference in a dimension present on BOTH sides is decisive even when
    the other claim lacks a different dimension: the temporal difference alone
    explains the two values, so TEMPORAL outranks the missing-context review."""
    left = claim('1.0', time='t1', scope='s1', environment='e1')  # definition missing
    right = claim('2.0', **full_context())
    assert classify_conflict(left, right)['classification'] == 'TEMPORAL'


def test_disjoint_context_dimensions_defer_to_review():
    """No dimension is present on both sides, so no explicit difference exists
    and completeness is not established: REVIEW, never a direct conflict."""
    left = claim('1.0', time='t1')
    right = claim('2.0', definition='d2')
    result = classify_conflict(left, right)
    assert result['classification'] == 'REVIEW'
    assert result['different_dimensions'] == []


def test_empty_string_context_counts_as_missing():
    left = claim('1.0', **full_context(definition=''))
    right = claim('2.0', **full_context())
    assert classify_conflict(left, right)['classification'] == 'REVIEW'


def test_conflict_without_context_object_defers_to_review():
    left = {'subject': 's', 'predicate': 'p', 'value': '1', 'context': {}}
    right = {'subject': 's', 'predicate': 'p', 'value': '2', 'context': {}}
    assert classify_conflict(left, right)['classification'] == 'REVIEW'


# --- the only direct contradiction ---------------------------------------------


def test_identical_explicit_context_with_different_values_is_direct():
    left = claim('1.0', **full_context())
    right = claim('2.0', **full_context())
    result = classify_conflict(left, right)
    assert result['classification'] == 'DIRECT_CONFLICT'
    assert result['different_dimensions'] == []
    assert 'Different values with the same explicitly supplied context' in result['reason']


def test_symmetry_of_the_classification():
    """Swapping the claims must not change the verdict (the pair conflicts,
    not one side of it)."""
    left = claim('1.0', **full_context(time='t1'))
    right = claim('2.0', **full_context(time='t2'))
    assert classify_conflict(left, right) == classify_conflict(right, left)
