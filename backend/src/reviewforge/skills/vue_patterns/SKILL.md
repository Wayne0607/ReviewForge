---
name: vue_patterns
description: Vue.js code review rules (Composition API, SFC, reactivity)
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [vue, nuxt]
---

# Vue Patterns Review

## Security（必查，最高优先级）

### XSS
- `v-html="userContent"` — 用户内容渲染为原始 HTML，XSS 风险 → **error**
- `v-bind:innerHTML` / `:innerHTML` — 等效于 `v-html` → **error**
- 动态组件 `<component :is="userInput">` 组件名来自用户输入 → **warning**
- URL 来自用户输入且用于 `window.location.href` / `router.push` → **error**（开放重定向）

### Client-Side Secrets
- 硬编码的 API key、token、密钥放在 `<script setup>` 或 composable 中 → **error**
- `NEXT_PUBLIC_` / `VITE_` 前缀的环境变量包含敏感凭证 → **error**（这些会被打包进客户端 JS）
- `localStorage.setItem('token', ...)` 将 JWT 存在本地存储 → **warning**（XSS 可窃取）

### Template Security
- `v-html` 的任何用户控制内容必须先用 DOMPurify/sanitize-html 清洗
- SSR 渲染的用户内容未转义 → **error**
- 第三方 script 动态加载 `<script :src="userUrl">` → **error**

## Key Areas

### Component Design
- Use `<script setup>` syntax for new components — cleaner, better TypeScript support
- Props must be typed: use `defineProps<{...}>()` with TypeScript or `validator` in Options API
- Avoid mutating props directly; use `v-model` / `defineModel` or emit events upward
- Components > 300 lines should be split; extract composables for reusable logic

### Reactivity
- `ref()` vs `reactive()`: prefer `ref()` for primitives, `reactive()` only for complex objects with nested refs
- Avoid destructuring reactive objects — breaks reactivity; use `toRefs()` or `.value` access
- `computed()` must not have side effects (no mutations, no API calls)
- `watch()` vs `watchEffect()`: use `watchEffect` for automatic dependency tracking, `watch` for explicit deps
- Memory leaks: clean up watchers, event listeners, and timers in `onUnmounted`

### Template
- `v-if` vs `v-show`: `v-if` for rarely-toggled or heavy content; `v-show` for frequent toggles
- Always provide `:key` in `v-for`; use unique IDs, not array indices
- Avoid `v-if` and `v-for` on the same element; use `<template>` wrapper
- `v-html` is an XSS risk; use `v-text` or template interpolation unless content is sanitized

### State Management
- Use Pinia for global state; prefer composables for component-local or shared state
- Don't store derived state; use `storeToRefs()` + computed for reactive destructuring
- Avoid deep watchers on large objects; watch specific paths instead

### TypeScript
- Use typed props, emits, and slots: `defineProps<T>()`, `defineEmits<T>()`, `defineSlots<T>()`
- Avoid `any` in composable return types; use generics for reusable patterns
- Template ref typing: `const el = ref<HTMLDivElement>()` and use `el.value?.focus()`

## Validation Criteria

**True Positive**: The pattern causes reactivity bugs, memory leaks, or security issues.

**False Positive**: The pattern is a deliberate optimization or required by a specific edge case.
