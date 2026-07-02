---
name: go_best_practices
description: Go code style and best practices review rules
category: style
reviewer_type: style
languages: [go]
---

# Go Best Practices Review

## Key Areas

### Error Handling
- Never ignore errors with `_ = err` — always check and handle
- Don't just log and continue; decide: return, retry, or wrap with context
- Use `fmt.Errorf("...: %w", err)` to wrap errors, preserving the chain
- Avoid `panic` in library code; use it only for truly unrecoverable states

### Goroutine Lifecycle
- Every goroutine must have a clear exit path (context cancellation, channel close, done signal)
- Use `context.Context` for cancellation propagation across goroutines
- Avoid starting goroutines in `init()` or global scope — no way to stop them
- Always `defer wg.Done()` immediately after `wg.Add(1)` before starting goroutine

### Interface Design
- Interfaces should be small (1-3 methods); prefer single-method interfaces
- Define interfaces where they are consumed, not where the implementation lives
- Accept interfaces, return concrete types — this gives callers flexibility
- Don't export interfaces for mocks; let consumers define what they need

### Naming Conventions
- Package names: lowercase, single word, no underscores (e.g., `http`, `json`)
- Exported names: PascalCase with initialisms all-caps (e.g., `HTTPServer`, `UserID`)
- Unexported names: camelCase
- Getters: prefer `Owner()` over `GetOwner()`
- Avoid stutter: `user.UserName` → `user.Name`; don't repeat package name in symbols

### Memory & Performance
- Avoid allocations in hot paths: prefer value types over pointers when possible
- Use `sync.Pool` for frequently-allocated short-lived objects
- Pre-allocate slice capacity when size is known: `make([]T, 0, expectedSize)`
- Avoid boxing values into interfaces in tight loops
- `defer` in a loop delays execution until function exit — wrap in a closure to scope it

### Testing Patterns
- Use table-driven tests: define a slice of test cases and iterate
- Name test functions `Test<Function>_<Scenario>` for clarity
- Use `t.Run()` for subtests to enable parallel execution and fine-grained output
- Avoid `time.Sleep` in tests; use channels or `time.After` with explicit waits

## Validation Criteria

**True Positive**: The pattern violates Go conventions and causes bugs, leaks, or maintainability issues.

**False Positive**: The pattern is justified in context (e.g., `panic` in an `init` function for must-parse templates), or it's in generated code.
