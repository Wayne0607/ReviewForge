---
name: vue_patterns
description: Vue/Nuxt 代码审查规则。当审查 .vue 文件或 Vue 项目的 TS/JS 文件时套用。检查组件设计、响应式、Composition API、模板安全（v-html XSS）。
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [vue, nuxt]
---

# Vue/Nuxt Patterns Review

## When to Apply
- 审查 `.vue` 单文件组件
- 审查 Vue/Nuxt 项目的 composable 和组件逻辑

## When NOT to Apply
- **测试文件**（`*.test.ts`, `*.spec.ts`）→ 测试规则放宽
- **配置文件**（`nuxt.config.*`, `vite.config.*`）→ 不同规则
- **生成的类型**（`*.d.ts`）→ 不审查
- **纯工具函数**（`utils/`, `helpers/` 中的非 Vue 代码）→ Vue 特定规则不适用
- **SSR-only 代码**（`server/` 目录下的 Nitro handlers）→ 组件规则不适用

## Security（必查，最高优先级）

### XSS
- `v-html="userContent"` — 用户内容渲染为原始 HTML → **error**
- `:innerHTML="userContent"` — 等效于 v-html → **error**
- 动态组件 `<component :is="userInput">` 组件名来自用户 → **warning**

### Client-Side Secrets
- API key、token 在 `<script setup>` 或 composable 中硬编码 → **error**
- `NUXT_PUBLIC_*` / `VITE_*` 前缀变量包含敏感凭证 → **error**

### Open Redirect
- `router.push(userInput)` 跳转目标未白名单 → **warning**

## Key Areas

### Component Design
- Use `<script setup>` for new components
- Props typed with `defineProps<T>()`
- Never mutate props directly; use emit or `defineModel`
- > 300 lines → split into composables or sub-components

### Reactivity
- `ref()` vs `reactive()`: prefer `ref()` for primitives
- Don't destructure reactive objects — use `toRefs()`
- `computed()` must have no side effects
- Clean up watchers/timers/listeners in `onUnmounted`

### Template
- `v-if` vs `v-show`: `v-if` for heavy/rare, `v-show` for frequent toggles
- Always `:key` in `v-for`; use unique IDs, not array indices
- Don't use `v-if` and `v-for` on the same element

### State Management (Pinia)
- Don't store derived state
- Use `storeToRefs()` for reactive destructuring

## Validation Criteria

**True Positive**: Causes reactivity bugs, memory leaks, or security issues. Confidence > 0.7.

**False Positive**:
- `v-html` 渲染的内容来自 CMS/可信源且经过 sanitize
- `computed` 的"副作用"是读取外部状态（不是写入）
- 不清理 `setInterval` 但该 watch 仅触发一次且组件生命周期等同于应用
- Props 修改发生在 `defineModel` 或 `v-model` 模式中
