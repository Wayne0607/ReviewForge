---
name: ruby_best_practices
description: Ruby code style and best practices review rules
category: style
reviewer_type: style
languages: [ruby]
---

# Ruby Best Practices Review

## Security（必查，最高优先级）

### Code Injection
- `eval(user_input)` — 用户输入直接作为 Ruby 代码执行 → **error**
- `instance_eval(user_input)` / `class_eval(user_input)` — 同上 → **error**
- `send(user_input)` / `public_send(user_input)` 方法名来自用户输入且未白名单 → **error**
- `Kernel.const_get(user_input)` 动态常量查找来自用户输入 → **warning**

### Command Injection
- `` `command #{user_input}` `` 反引号中拼接用户输入 → **error**
- `system("command #{user_input}")` 用户输入在 shell 命令中 → **error**
- `exec("command #{user_input}")` 同上 → **error**
- `Open3.capture` / `popen` 参数来自用户输入且未白名单 → **error**
- `%x(command #{user_input})` — `%x` 语法等同反引号 → **error**

### Insecure Deserialization
- `YAML.load(user_data)` — 使用 `safe_load` 或显式指定 `permitted_classes` → **error**
- `Marshal.load(user_data)` — 可构造恶意对象 → **error**
- `JSON.load` vs `JSON.parse` — `load` 可包含 Ruby 对象反序列化 → **warning**

### Hardcoded Secrets
- API key、password、token 以字符串字面量写在源码中 → **error**
- 常量 (`API_SECRET = "sk_..."`) 包含凭证 → **error**

### Path Traversal
- `File.open(user_path)` / `File.read(user_path)` 用户控制的路径未做 `..` 过滤 → **error**
- `Dir.glob(user_pattern)` 用户控制的 glob 模式 → **warning**

## Key Areas

### Error Handling
- **禁止** `rescue Exception` — 必须 rescue `StandardError` 子类；`Exception` 会捕获 `SignalException`、`SystemExit`
- 禁止空 `rescue` 块（bare rescue 不指定异常类型）
- 禁止 rescue 后只 log 不处理就直接 re-raise — 要么处理，要么让它传播
- `ensure` 块中禁止 `return` 或 `raise` — 会掩盖原始异常

### Metaprogramming
- `method_missing` 必须同步覆写 `respond_to_missing?` — 否则 `respond_to?` 返回错误结果
- 优先用 `define_method` 替代 `method_missing` 做动态方法
- `instance_eval` / `class_eval` 打破封装，需有注释说明原因
- Monkey-patching 优先用 `Module#prepend` 而非 reopen class

### Blocks & Enumerables
- 优先用 block 形式而非 `Proc.new` / `lambda`
- `&:method` 简写只在 block 仅调用一个方法时使用
- 区分：`each` 返回 receiver、`map` 返回新数组
- 优先用 `find`（非 `select.first`）、`any?`（非 `select.any?`）

### Naming & Conventions
- 方法: `snake_case`；谓词方法以 `?` 结尾；危险方法以 `!` 结尾
- 类/模块: PascalCase
- 常量: `UPPER_SNAKE_CASE`
- 文件名: snake_case，匹配主类名

### Resource Management
- File/IO: 用块形式 `File.open(path) { |f| ... }` 自动关闭
- 避免一次性加载大文件；用 `File.foreach` 逐行流式读取
- Gemfile 中锁定版本、审核新增依赖的必要性

## Validation Criteria

**True Positive**: 代码会引入安全漏洞、运行时异常或违反 Ruby 约定。

**False Positive**: 是 Rails/Domain-Specific DSL 的惯用法（如 Rails 的 `constantize`），或有明确注释说明原因。
