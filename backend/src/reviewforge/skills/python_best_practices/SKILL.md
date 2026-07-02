---
name: python_best_practices
description: Python code style and best practices review rules
category: style
reviewer_type: style
languages: [python]
---

# Python Best Practices Review

## Key Areas

### Type Hints
- Public functions should have type hints
- Return types should be explicit (not just `-> None` for everything)
- Use `from __future__ import annotations` for forward refs

### Error Handling
- Bare `except:` or `except Exception: pass` is always a finding
- Errors should be logged or re-raised, never silently swallowed
- Use specific exception types

### Naming
- Functions: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private: leading underscore `_name`

### Function Complexity
- Functions > 30 lines should be split
- Nesting > 3 levels is a warning
- Too many parameters (> 5) is a design smell

### Imports
- Unused imports
- Circular imports
- Missing `__init__.py`

## Validation Criteria

**True Positive**: The issue makes the code harder to maintain or understand.

**False Positive**: The pattern is intentional (e.g., a broad except in a top-level handler), or it's in a generated file.
