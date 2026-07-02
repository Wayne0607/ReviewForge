---
name: ruby_best_practices
description: Ruby 代码审查规则。当审查 .rb 文件时套用。检查安全性（eval/命令注入/反序列化）、异常处理、元编程规范、代码块使用惯例。
category: style
reviewer_type: style
languages: [ruby]
---

# Ruby Best Practices Review

## When to Apply
- 审查 `.rb` 文件（非测试文件）
- 审查 Ruby/Rails 项目的安全性、惯用性

## When NOT to Apply
- **测试文件**（`test/`, `spec/`, `*_test.rb`, `*_spec.rb`）→ 测试 DSL 的模式不同；测试中可以 rescue 任意异常
- **Rails 框架文件**（ActiveRecord model, ActionController）→ 框架约定优先（如 `before_action`、metaprogramming）
- **Gemfile / .gemspec** → 依赖文件，不同规则
- **生成的 schema.rb** → 不审查
- **rake 任务** → CLI 入口，放宽复杂度
- **DSL 实现代码**（内部 gem/library）→ `method_missing` 和 `instance_eval` 是 DSL 的正常模式

## Security（必查，最高优先级）

### Code Injection
- `eval(user_input)` — 用户输入作为 Ruby 代码执行 → **error**
- `instance_eval(user_input)` / `class_eval(user_input)` → **error**
- `send(user_input)` 方法名来自用户输入且未白名单 → **error**

### Command Injection
- `` `command #{user_input}` `` 反引号拼接用户输入 → **error**
- `system("cmd #{user_input}")` → **error**
- `exec("cmd #{user_input}")` → **error**
- `%x(cmd #{user_input})` — 等同反引号 → **error**
- `Open3.capture` / `popen` 参数来自用户 → **error**

### Insecure Deserialization
- `YAML.load(user_data)` → **error**（应用 `safe_load` 或指定 `permitted_classes`）
- `Marshal.load(user_data)` → **error**

### Hardcoded Secrets
- API key、token、password 硬编码 → **error**

## Key Areas

### Error Handling
- **禁止** `rescue Exception` — 必须 rescue `StandardError` 子类
- 空 rescue 块不指定异常类型 → **warning**
- `ensure` 块中禁止 `return` 或 `raise`

### Metaprogramming
- `method_missing` 必须同步覆写 `respond_to_missing?`
- 优先用 `define_method` 替代 `method_missing`
- `instance_eval` / `class_eval` 需注释说明原因
- Monkey-patching 用 `Module#prepend` 优于 reopen class

### Blocks & Enumerables
- 优先用 block 形式而非 `Proc.new` / `lambda`
- 区分：`each` 返回 receiver、`map` 返回新数组
- 优先用 `find`（非 `select.first`）、`any?`（非 `select.any?`）

### Naming
- 方法: `snake_case`；谓词 `?` 结尾；危险方法 `!` 结尾
- 类: PascalCase；常量: UPPER_SNAKE

## Validation Criteria

**True Positive**: 代码会引入安全漏洞或运行时异常。Confidence > 0.7。

**False Positive**:
- Rails 的 `constantize`、`try`、`send` 是框架正常用法
- `method_missing` 在 DSL gem 中是有意设计
- `rescue Exception` 在顶层错误处理器或测试中可能是有意的
- `eval` 在 IRB/控制台代码或 build 脚本中
- `YAML.load` 加载可信配置文件（但应建议换成 `safe_load`）
- Meta-programming 是 Rails/ActiveSupport 的标准模式
