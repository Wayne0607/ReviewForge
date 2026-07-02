---
name: ruby_best_practices
description: Ruby code style and best practices review rules
category: style
reviewer_type: style
languages: [ruby]
---

# Ruby Best Practices Review

## Key Areas

### Error Handling
- Rescue specific exceptions (`StandardError` subclasses), never `Exception` (catches SignalException, SystemExit)
- Avoid bare `rescue => e` without a specific class — it catches `StandardError` but is still too broad
- Don't rescue just to log and re-raise; either handle or let it propagate
- In `ensure` blocks, don't return or raise — it masks the original exception

### Metaprogramming
- Avoid `method_missing` — override `respond_to_missing?` as well, and prefer `define_method` when possible
- `instance_eval` and `class_eval` break encapsulation; use sparingly and document why
- `send` and `__send__` bypass access controls; prefer `public_send` when calling public methods
- Monkey-patching: prefer `Module#prepend` over reopening classes for safer composition

### Blocks & Enumerables
- Prefer block form over `Proc.new` or `lambda` for simple callbacks
- Use `&:method` shorthand when the block only calls one method on the yielded object
- Know the difference: `each` returns the receiver; `map` returns a new array
- Use `find` (not `select.first`), `any?` (not `select.any?`), `all?` for clarity and early exit
- Prefer `yield` over `&block.call` when you control the method definition

### Naming & Conventions
- Methods: `snake_case`; predicate methods end with `?`; dangerous/bang methods end with `!`
- Classes/Modules: PascalCase
- Constants: `UPPER_SNAKE_CASE`
- Files: snake_case, match the primary class name
- Use `attr_reader`/`attr_writer`/`attr_accessor` instead of manual getter/setter methods

### Resource & Memory
- File/IO operations: use block form `File.open(path) { |f| ... }` for automatic closing
- Avoid loading large files entirely into memory; stream line-by-line with `File.foreach`
- Be aware: frozen string literals reduce allocations; use `# frozen_string_literal: true`
- Thread safety: MRI has a GIL, but shared mutable state between threads is still dangerous

## Validation Criteria

**True Positive**: The pattern hides bugs, violates Ruby conventions, or creates maintenance risks.

**False Positive**: The pattern is a deliberate DSL or framework convention (e.g., Rails metaprogramming).
