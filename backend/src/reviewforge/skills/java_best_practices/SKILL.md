---
name: java_best_practices
description: Java code style and best practices review rules
category: style
reviewer_type: style
languages: [java]
---

# Java Best Practices Review

## Key Areas

### Exception Handling
- Never catch `Exception` and swallow silently — at minimum, log with context
- Never throw from a `finally` block; it masks the original exception
- Use checked exceptions for recoverable conditions, unchecked for programming errors
- Don't use exceptions for control flow; they are expensive and obscure intent

### Resource Management
- Always close resources with try-with-resources (`AutoCloseable`)
- Resources at risk: `InputStream`, `OutputStream`, `Reader`, `Writer`, `Connection`, `Statement`, `ResultSet`
- Nesting try-with-resources: declare resources in reverse order of dependency

### Collections & Streams
- Prefer `List.of()` / `Set.of()` / `Map.of()` over `Collections.unmodifiable*` for small immutable collections
- Avoid `null` returns from collections methods; return empty collections instead
- Stream: don't mutate external state inside lambdas; use `collect()` instead
- Avoid `peek()` for side effects outside debugging — it's not guaranteed to execute
- Parallel streams: only for CPU-bound, stateless, independent operations on large datasets

### Optional Usage
- Only use `Optional` as a return type, never as a field or method parameter
- Prefer `orElseThrow()` over `get()` to make failure explicit
- Don't use `Optional` for conditional logic; use `ifPresentOrElse` or map/flatMap chains
- `Optional.of()` throws NPE on null; use `Optional.ofNullable()` for uncertain inputs

### Naming & Style
- Classes: PascalCase, interfaces don't need `I` prefix
- Methods: camelCase, should read as verb phrases
- Constants: `UPPER_SNAKE_CASE`
- Package names: lowercase, reverse domain, no underscores
- Always override `hashCode()` when overriding `equals()`

### Defensive Programming
- Never expose internal mutable state; return defensive copies or unmodifiable views
- Constructor parameters: validate and copy mutable objects on entry
- Use `Objects.requireNonNull()` at API boundaries for explicit null rejection
- Be aware of `SimpleDateFormat` thread-safety; use `DateTimeFormatter` instead

## Validation Criteria

**True Positive**: The pattern introduces bugs, resource leaks, or makes the code harder to reason about.

**False Positive**: The pattern is intentional (e.g., null return as a domain concept), or constrained by a framework contract.
