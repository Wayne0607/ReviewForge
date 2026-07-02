---
name: testing_rules
description: Cross-language test quality review rules
category: methodology
reviewer_type: testing
languages: []
---

# Testing Rules Review (Universal)

## Key Areas

### Test Coverage
- New public functions/methods must have corresponding tests
- Critical paths (auth, payment, data mutation) must have tests covering happy path + edge cases
- Bug fixes must include a regression test that fails before the fix and passes after
- Test the behavior, not the implementation — refactoring shouldn't break your tests

### Test Boundaries
- Test boundary conditions: empty input, null/None/nil, max values, zero values, negative numbers
- Test error paths: what happens when an external dependency fails, times out, or returns unexpected data
- Test concurrency edge cases when applicable: race conditions, deadlocks, ordering assumptions

### Mocking
- Don't mock what you don't own (third-party libraries) — wrap them in your own interface
- Over-mocking leads to tests that pass while the real integration is broken (false confidence)
- Prefer real in-memory implementations (fakes) over mocks for databases, caches, queues
- Mock at the architectural boundary, not every internal dependency

### Test Quality
- Test names must describe the scenario and expected outcome: `test_retryOnTimeout_returnsAfter3Attempts`
- Each test should test one behavior; avoid multi-step end-to-end scenarios in a single test
- Avoid logic in tests: no if/else, no loops with dynamic assertions — tests should be linear
- Flaky tests (intermittent failures) are worse than no tests — they erode trust in the suite
- Assertions must be specific: `assertEquals(expected, actual)` > `assertTrue(actual.contains(x))`

### Test Organization
- Structure: Arrange → Act → Assert (given → when → then)
- Shared setup goes in fixtures/setUp/beforeEach, not duplicated in every test
- Test files should mirror source structure: `src/user/service.py` → `tests/user/test_service.py`

## Validation Criteria

**True Positive**: The test gap means a bug could reach production undetected.

**False Positive**: The coverage gap is for trivial code (getter/setter, simple delegation), or the test approach is constrained by framework limitations.
