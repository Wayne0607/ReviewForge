---
name: testing_rules
description: 跨语言测试质量审查规则。当 diff 包含测试文件或新增公共 API 时需要对应测试时套用。检查覆盖率、边界条件、mock 使用、测试命名。
category: methodology
reviewer_type: testing
languages: []
---

# Testing Rules Review (Universal)

## When to Apply
- Diff 中包含测试文件（`test_*.py`, `*_test.go`, `*Test.java`, `*.spec.ts`, `*.test.ts`）
- 新增了公共函数/类/方法但没有对应测试
- 修改了业务逻辑但未更新测试

## When NOT to Apply
- **纯重构 PR**（行为不变的重命名/提取方法）→ 不需要新增测试
- **文档变更** → 不需要测试
- **配置文件修改** → 除非配置项改变行为
- **依赖版本升级** → 不需要自己写测试（除非有兼容性问题）
- **已弃用代码的删除** → 不需要补充测试
- **样板/脚手架代码**（简单的 getter/setter/CRUD 代理）→ 可选，不强制
- **紧急热修复** → 回归测试足够，不强求新测试

## Key Areas

### Test Coverage
- New public functions/methods should have corresponding tests
- Critical paths (auth, payment, data mutation) must have tests covering happy path + edge cases
- Bug fixes must include a regression test that fails before the fix and passes after
- **Don't flag**: private/internal helpers, simple delegation methods, getters/setters

### Test Boundaries
- Test boundary conditions: empty input, null/None/nil, max values, zero values, negative numbers
- Test error paths: what happens when an external dependency fails, times out, or returns unexpected data
- Test concurrency edge cases when applicable: race conditions, deadlocks, ordering assumptions

### Mocking
- Don't mock what you don't own — wrap third-party libraries in your own interface
- Over-mocking leads to tests that pass while the real integration is broken
- Prefer real in-memory implementations (fakes) over mocks for databases, caches, queues
- Mock at the architectural boundary, not every internal dependency

### Test Quality
- Test names must describe the scenario and expected outcome
- Each test should test one behavior; avoid multi-step E2E scenarios in a single test
- Avoid logic in tests: no if/else, no loops with dynamic assertions — tests should be linear
- Flaky tests are worse than no tests — they erode trust in the suite
- Assertions must be specific: `assertEquals(expected, actual)` > `assertTrue(actual.contains(x))`

### Test Organization
- Structure: Arrange → Act → Assert (given → when → then)
- Shared setup goes in fixtures/setUp/beforeEach, not duplicated in every test
- Test files should mirror source structure

## Validation Criteria

**True Positive**: The test gap means a bug could reach production undetected. Confidence > 0.6.

**False Positive**:
- The function being called is a private helper tested indirectly via public API
- The class is a simple DTO/value object with only getters/setters
- The method is a thin wrapper around a well-tested library function
- The code is in a prototype/experimental module
- The test coverage gap is for trivial glue code (controller → service → repository wiring)
- It's an integration test covering the happy path only (this is often intentional)
