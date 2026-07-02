---
name: rust_best_practices
description: Rust 代码审查规则。当审查 .rs 文件时套用。检查 unsafe 使用、错误处理（unwrap/panic）、所有权与借用、unsafe 块 SAFETY 注释、安全漏洞。
category: style
reviewer_type: style
languages: [rust]
---

# Rust Best Practices Review

## When to Apply
- 审查 `.rs` 文件（非测试文件）
- 审查 Rust 项目的安全性、惯用性、所有权正确性

## When NOT to Apply
- **测试代码**（`#[cfg(test)]` 模块、`tests/` 目录）→ `unwrap()` 在测试中是惯用法；测试 assist functions 可放宽复杂度
- **示例代码**（`examples/`）→ 为了教学目的可能故意简化
- **build.rs / 构建脚本** → 不同规则体系
- **FFI 代码**（`extern "C"` 块）→ `unsafe` 是必需的；遵循 C 的惯例
- **生成的代码**（`target/`, `out/`）→ 不审查
- **宏实现**（`macro_rules!`）→ 宏内部的复杂性是内在的

## Security（必查，最高优先级）

### Command Injection
- `std::process::Command::new(user_input)` — 用户控制命令名 → **error**
- `.arg("-c").arg(userInput)` 配合 shell → **error**
- 任何 Command 的 executable/args 来自用户输入且未白名单校验 → **error**

### Hardcoded Secrets
- API key、token、password 以字面量写入源码 → **error**
- 不要在字符串中放 JWT secret、数据库密码、云服务凭证

### Unsafe Code
- `unsafe { ... }` 没有 `// SAFETY:` 注释 → **warning**
- `transmute` 在非 FFI 代码中 → **warning**（优先用 safe cast）
- 裸指针解引用前未检查 null/alignment → **error**

## Key Areas

### Error Handling
- 库代码（`lib.rs`）: 禁止 `unwrap()` / `expect()` / `panic!` → 用 `?` 或 `match`
- 应用代码: `unwrap()` 在初始化阶段可接受，请求处理路径不可接受
- `Result` 不可用 `let _ =` 吞掉
- 实现 `std::error::Error` 用于自定义错误

### Ownership & Borrowing
- 不必要的 `.clone()` → 优先传引用 `&T`
- 返回 `Vec<T>` 而调用方只需遍历 → 返回 `&[T]` 或 `impl Iterator`
- `Rc<RefCell<T>>` 不是设计问题的解决方案

### Idiomatic Rust
- 优先用 iterator combinators 替代手动 `for` + `push`
- `if let` / `while let` 替代单臂 `match`
- 自动 derive: `#[derive(Debug, Clone, PartialEq, Eq, Hash)]`

## Validation Criteria

**True Positive**: 代码在生产路径上会导致 panic、unsafety 或违反 Rust 安全保证。Confidence > 0.7。

**False Positive**:
- `unwrap()` 在 `#[test]` 函数中（这是惯用法）
- `unsafe` 在 FFI 边界（这是必需的）
- `unwrap()` 在 `main()` 或初始化代码中（程序无法恢复时 panic 是合理的）
- `clone()` 是为了满足 borrow checker 且性能可接受
- `panic!` 在 `build.rs` 或编译时代码中
- 生成的 protobuf/FlatBuffers 代码
