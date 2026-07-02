---
name: code_quality
description: Cross-language code quality rules (complexity, naming, structure)
category: style
reviewer_type: style
languages: []
---

# Code Quality Review (Universal)

## Key Areas

### Function Complexity
- Functions/methods should generally fit on screen (~40 lines); longer functions are a candidate for extraction
- Nesting depth > 3 levels is a warning — consider guard clauses, early returns, or extracted helpers
- Cyclomatic complexity > 10 needs justification; consider strategy/command patterns for branching

### Naming
- Names should reveal intent: `getUserById()` > `get()`, `isExpired()` > `check()`
- Avoid single-letter variables except in very local scope (loop index `i`, closure param)
- No misleading names: a function called `validate()` shouldn't also mutate state
- Boolean variables should read as questions: `isEmpty`, `hasPermission`, `canEdit`

### Magic Values
- Hardcoded numbers and strings in logic should be extracted as named constants
- Exception: well-known values like `0`, `1`, `-1`, `""` don't need names
- Configuration values (timeouts, limits, URLs) must never be inlined

### Dead Code
- Commented-out code blocks are dead code — delete them (git history preserves them)
- Unused variables, imports, and functions should be removed
- Empty catch/except blocks that silently swallow errors are a red flag

### Single Responsibility
- A function should do one thing and do it well; if the docstring needs "and", it does too much
- A class/module should have one reason to change
- Avoid "god objects" that know about everything — split by concern

### Error Handling
- Errors must not be silently swallowed; at minimum, log with context
- Error messages should describe what went wrong and, if known, how to fix it
- Don't expose internal details (stack traces, SQL queries, file paths) in user-facing error messages

## Validation Criteria

**True Positive**: The issue genuinely impacts readability, maintainability, or error resilience.

**False Positive**: The complexity is inherent to the domain (e.g., a state machine), or conventions are intentionally different for valid reasons (e.g., embedded systems optimization).
