# Rust-Specific Security Patterns

## Unsafe Code
- `unsafe { ... }` blocks that contain more than the minimal necessary operation
- Raw pointer dereference without null/alignment/lifetime validation
- `transmute` between unrelated types — can violate Rust's safety guarantees
- `MaybeUninit::assume_init()` without proper initialization check
- Check: every `unsafe` block has a `// SAFETY:` comment explaining invariants

## Command Injection
- `std::process::Command::new(userInput)` — user controls the command name
- `Command::arg(userInput)` — safe for arguments (no shell interpretation)
- Shell bypass: `.arg("-c").arg(userInput)` with shell invocation — user controls shell script
- `std::process::Command` does NOT use a shell by default (unlike C `system()`) — safer but still risky with user-controlled command names

## Memory Safety Bypasses
- `std::mem::forget()` used to leak resources with pending cleanup (drop guards)
- `ManuallyDrop` without explicit drop when needed
- Custom `Drop` implementations that panic (double-panic is an abort)
- `Rc`/`Arc` cycles that prevent deallocation of large graphs

## Unsafe Deserialization
- `serde` deserializing untrusted data with `deserialize_any` (can construct arbitrary types)
- `bincode`, `postcard` — binary formats without schema validation
- JSON/YAML with `#[serde(untagged)]` enums — attacker can match unexpected variants
- `serde_json::from_str::<serde_json::Value>(userInput)` is safe; arbitrary typed deserialization needs validation

## FFI Safety
- C function signatures: does the Rust signature match the C ABI exactly?
- Lifetimes: does Rust guarantee the pointer is valid for the duration?
- Panics across FFI boundary: `catch_unwind` required when calling Rust from C
- Ownership: who frees the memory? Document in function contract
