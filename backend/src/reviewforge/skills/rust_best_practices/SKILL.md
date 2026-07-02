---
name: rust_best_practices
description: Rust code style and best practices review rules
category: style
reviewer_type: style
languages: [rust]
---

# Rust Best Practices Review

## Key Areas

### Ownership & Borrowing
- Avoid unnecessary `.clone()` — pass references or use `Cow` when ownership is ambiguous
- Know the difference: `clone()` duplicates heap data; `Copy` is a bitwise copy for small types
- `Rc<RefCell<T>>` is not a substitute for proper ownership design; prefer restructured types
- Watch for accidental moves in closures: use `move` only when needed, or capture by reference
- In hot paths, prefer `&str` over `String`, `&[T]` over `Vec<T>` for read-only parameters

### Error Handling
- Use `Result<T, E>` for recoverable errors, not `panic!` or `unwrap()`
- `unwrap()` / `expect()` are only acceptable in tests, examples, or truly unrecoverable states
- Implement `std::error::Error` for custom error types to integrate with the ecosystem
- Use `thiserror` crate for deriving Error; use `anyhow` in applications, not libraries
- Chain errors with `?` operator; don't manually `.map_err()` for simple propagation

### Unsafe Code
- Every `unsafe` block must have a `// SAFETY:` comment explaining invariants being upheld
- Minimize `unsafe` scope: wrap only the minimal operation, not surrounding safe code
- Never use `transmute` when safe casts or `From`/`Into` exist
- Raw pointer dereference must be guarded by null/alignment/lifetime checks
- Prefer safe abstractions from `std` over custom unsafe implementations

### Traits & Generics
- Follow the orphan rule: implement your trait for your type, or their trait for your type
- Avoid trait objects (`dyn Trait`) in performance-sensitive paths; prefer generics with static dispatch
- Use `impl Trait` in return position for simple cases, named generics when constraints grow
- Don't over-trait; if there's only one implementation, no need for abstraction yet

### Idiomatic Rust
- Use `match` exhaustively; consider `#[non_exhaustive]` for library enums
- Prefer iterator combinators over manual `for` + mutable accumulator
- Use `if let` and `while let` instead of single-arm `match`
- Derive common traits: `#[derive(Debug, Clone, PartialEq, Eq, Hash)]`
- `const` over `static` when possible; prefer `lazy_static` / `once_cell` over mutable statics

## Validation Criteria

**True Positive**: The pattern causes unsafety, panics in production, or violates Rust's ownership guarantees.

**False Positive**: The pattern is intentional (e.g., `unwrap()` in test code), or forced by FFI boundaries.
