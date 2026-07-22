---
name: python_correctness
description: Python-specific correctness review — observable semantic bugs in None/falsy, mutable defaults, async lifecycle, generators, exceptions, ORM evaluation, serialization boundaries, and concurrency
category: correctness
reviewer_type: correctness
languages: [python]
---

# Python Correctness Review

Review observable semantic bugs in changed Python code. Every finding MUST cite a concrete changed line and repository evidence (caller, callee, definition, or test) that demonstrates the wrong result.

## Method

1. Read the changed lines and identify the contract (return type, caller expectations, error paths, state transitions).
2. For each contract, check only the categories below that apply to the changed code.
3. Search repository declarations, signatures, and call sites to confirm or refute each suspicion.
4. Report only when you can trace one concrete execution path to an observable wrong result.

Do NOT report: naming, formatting, type-hint style, missing tests/docstrings, speculative robustness, or refactoring suggestions. If no concrete wrong result can be demonstrated, return no finding.

## Categories

### None vs falsy
Changed code uses a truthiness check (`if x:`, `x or default`) where `None` is a valid value distinct from `""`, `0`, `[]`, `{}`. Trace whether the guarded value can legitimately be None or another falsy sentinel.

### Mutable / dataclass defaults
Changed code assigns a mutable default (`list`, `dict`, `set`) as a function default argument or a `dataclass`/`attrs` field default. Confirm the class or function is actually instantiated more than once in the call graph.

### Async / await / task lifecycle
Changed code creates a `Task` or `Future` but does not `await`, `cancel`, or store it — the result or exception is silently dropped. Or: changed code awaits in a `finally` block where cancellation can suppress cleanup. Check the surrounding coroutine's lifecycle.

### Generator / iterator exhaustion
Changed code iterates a generator or iterator more than once, passes a generator to a function that exhausts it, or returns a generator where the caller expects a reusable sequence. Confirm the caller's iteration pattern.

### Exception / context-manager behavior
Changed code catches a broad exception (`except Exception`, bare `except`) that swallows a meaningful error from an inner call. Or: changed code enters a context manager whose `__exit__` silently suppresses an exception the caller needs to see.

### Django / ORM evaluation / transaction semantics
Changed code evaluates a queryset at the wrong point (e.g., `.exists()` before a bulk create, slicing a queryset that needs `.iterator()`). Or: changed code runs mixed read/write queries without an explicit `transaction.atomic()`, or uses `select_related`/`prefetch_related` on a queryset that is later sliced or unioned.

### Serialization / type-state boundaries
Changed code passes a `datetime`, `Decimal`, `Enum`, `bytes`, or other non-JSON-safe type across a serialization boundary (JSON response, `json.dumps`, Redis cache, Celery task arg) without conversion. Or: changed code reads a deserialized value and assumes a type (e.g., `int` vs `str` from form data) that does not match the source.

### Wrong caller / callee / argument / return contract
Changed code calls the wrong function, passes arguments in the wrong order, or returns the wrong object (e.g., returning `None` where a value is expected, returning a generator where a list is expected, swapping two similar-looking arguments). Confirm by checking the callee's signature and the caller's expectations.

### Concurrency / shared state
Changed code mutates a shared dict, list, or class attribute from multiple threads or async tasks without a lock. Or: changed code reads a shared resource that another task may be writing concurrently. Check whether the mutation is actually reachable from concurrent entry points.

## Suppressed patterns

- Style, naming, formatting, import order
- Type-hint completeness or annotation style
- Missing tests, docstrings, or comments
- Speculative robustness ("could fail if X changes")
- Micro-optimizations or readability preferences
