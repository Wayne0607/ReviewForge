---
name: rust_best_practices
description: Rust code style and best practices review rules
category: style
reviewer_type: style
languages: [rust]
---

# Rust Best Practices Review

## Security（必查，最高优先级）

### Command Injection
- `std::process::Command::new(user_input)` — 用户控制命令名，直接可执行任意程序
- `Command::arg(user_input)` 拼接 `-c` 再传入 shell — 绕过参数分离，可注入任意命令
- 任何 `Command` 的 executable/args 来自用户输入且未白名单校验 → **error**

### Hardcoded Secrets
- API key、token、password 以字符串字面量或 `const`/`static` 写在源码中 → **error**
- JWT secret、数据库密码、云服务凭证的硬编码 → **error**

### Unsafe Code
- `unsafe { ... }` 块如果没有 `// SAFETY:` 注释说明不变量 → **warning**
- `transmute` 在非 FFI 代码中 → **warning**（优先用 safe cast 或 `From`/`Into`）
- 对原始指针解引用前没有 null/alignment 检查 → **error**
- `MaybeUninit::assume_init()` 没有确保初始化 → **error**

### Panic in Production
- 库代码 (`lib.rs`) 中使用 `panic!`、`unreachable!`、`todo!` → **error**
- `unwrap()` / `expect()` 在非测试代码中 → **warning**（应用代码酌情，库代码严格）

## Key Areas

### Error Handling
- 库代码：`unwrap()` / `expect()` 只允许在测试和示例中；其他地方用 `?` 传播或用 `match` 处理
- `Result` 不能用 `let _ = ...` 吞掉；必须显式处理或用 `#[must_use]` lint
- 应用代码：`unwrap()` 在初始化阶段可接受（如配置加载），在请求处理路径中不可接受
- 实现 `std::error::Error` 用于自定义错误类型；库中用 `thiserror`，应用中用 `anyhow`

### Ownership & Borrowing
- 不必要的 `.clone()` — 优先传引用 `&T`，或用 `Cow` 处理可选所有权
- 返回 `Vec<T>` 而调用方只需要遍历 → 考虑返回 `impl Iterator<Item=T>` 或 `&[T]`
- `Rc<RefCell<T>>` 不是所有权设计问题的解决方案；优先重构类型层次

### Idiomatic Rust
- 优先用 iterator combinators（`.map`, `.filter`, `.collect`）替代手动 `for` + `push`
- `if let` / `while let` 替代单臂 `match`
- 自动 derive 常见 trait：`#[derive(Debug, Clone, PartialEq, Eq, Hash)]`
- `const` 优于 `static`（除非需要固定内存地址）

### Testing Patterns
- `unwrap()` 在测试中是惯用法，不报告
- 测试函数标注 `#[test]` 或 `#[tokio::test]`
- 不应有 `#[ignore]` 测试长期未修复

## Validation Criteria

**True Positive**: 代码在生产路径上会导致 panic、unsafety 或违反 Rust 安全保证。

**False Positive**: 代码在 `#[cfg(test)]` 块中、FFI 边界必需的 unsafe、或初始化阶段的 unwrap。
