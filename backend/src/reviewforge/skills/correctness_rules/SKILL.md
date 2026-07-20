---
name: correctness_rules
description: Evidence-first review of observable behavior, contracts, state transitions, and error paths
category: correctness
reviewer_type: correctness
---

# Correctness review method

Review the changed behavior, not code aesthetics.

1. Identify the contract of each changed function from its callers, sibling methods, return type, guards, and error paths.
2. Compare parallel branches and operations. Look for a wrong variable, callee, identifier kind, unit, recorder, provider, return object, or condition.
3. For a wrong argument, callee, identifier, unit, or recorder claim, search for its declaration/signature. If that is unavailable, require at least two independent sibling calls that agree on the contract. One contrasting call is ambiguous and is not evidence of which side is wrong.
4. Trace one concrete input or execution path to an observable wrong result. Report only when that path is supported by changed code or retrieved repository evidence.
5. Check boundaries: empty/null values, first/last item, error returns, cancellation, ordering, concurrent access, partial success, and rollback.
6. Prefer a small exact fix that restores the existing contract.

Do not report naming, formatting, static/final preferences, readability, refactoring, missing comments/tests, micro-optimizations, or speculative robustness. If no concrete wrong result can be demonstrated, return no finding.

## Good finding

The storage failure branch calls the legacy metric recorder even though the legacy operation has not run. This attributes a storage failure to the wrong backend; call the storage recorder used by the sibling success/error branches.

## Bad finding

This field should be static for readability, or this asynchronous code may be harder to maintain.
