---
name: java_best_practices
description: Java 代码审查规则。当审查 .java 文件时套用。检查异常处理、资源管理（try-with-resources）、Optional 使用、Stream API、安全漏洞。
category: style
reviewer_type: style
languages: [java]
---

# Java Best Practices Review

## When to Apply
- 审查 `.java` 文件（非测试文件）
- 审查 Java 项目的安全性、惯用性、资源管理

## When NOT to Apply
- **测试文件**（`*Test.java`, `*Tests.java`, `src/test/`）→ 测试中的异常处理、资源管理规则放宽
- **生成的代码**（`target/generated-sources/`, Lombok `@Data` 生成的 getter/setter）→ 不审查
- **JNI/FFI 代码** → 遵循 C 的惯例
- **框架样板**（Spring `@Configuration`、JPA Entity）→ 框架约定优先
- **遗留兼容性代码** → 标注 `@Deprecated` 的代码不强制现代化

## Security（必查，最高优先级）

### Command Injection
- `Runtime.getRuntime().exec(userInput)` → **error**
- `ProcessBuilder` 参数来自用户输入且未白名单 → **error**

### SQL Injection
- `Statement.executeQuery("SELECT ... WHERE name = '" + userInput + "'")` → **error**
- JPA native query / Hibernate HQL 用字符串拼接 → **error**
- 动态表名/列名必须白名单（`?` 占位符不支持标识符）

### Insecure Deserialization
- `ObjectInputStream` 处理不可信数据 → **error**
- `XStream`, `Kryo` 反序列化用户数据 → **error**

### Hardcoded Secrets
- 密码、API key、token 在源码中作为字符串常量 → **error**

### Path Traversal
- `new File(base, userInput)` 未规范化检查 → **error**

## Key Areas

### Exception Handling
- 禁止 `catch (Exception e) {}` 空吞异常 — 至少 log warning
- 禁止 `finally` 块中抛异常 — 掩盖原始异常
- 不用异常做控制流

### Resource Management
- 所有 `AutoCloseable` 必须用 try-with-resources
- 风险资源: `InputStream/OutputStream`, `Reader/Writer`, `Connection`, `Statement`, `ResultSet`

### Optional
- 只用 `Optional` 做返回值，不用作字段或参数
- 用 `orElseThrow()` 而非裸 `get()`
- `Optional.of()` 在 null 上抛 NPE → 用 `ofNullable()`

### Collections & Streams
- 返回空集合（`Collections.emptyList()`）而非 `null`
- Stream: 不在 lambda 中修改外部状态
- 用 `collect()` 而非 `forEach` + mutable accumulator

### Naming
- 方法: camelCase；类: PascalCase；常量: UPPER_SNAKE
- 永远成对覆写 `equals()` 和 `hashCode()`

## Validation Criteria

**True Positive**: 会引入 bug、资源泄漏或安全隐患。Confidence > 0.7。

**False Positive**:
- `catch (Exception e) { throw new RuntimeException(e); }` — 有意义的包装
- `null` 返回是域概念（如"未找到"）
- `Optional` 用作字段是 ORM/JPA 的限制
- 方法名 snake_case 是因为映射到数据库列名
- 生成的 EqualsAndHashCode（Lombok）不算遗漏
- 测试代码中的资源不强制 try-with-resources
