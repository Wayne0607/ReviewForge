---
name: vue_patterns
description: Vue.js code review rules (Composition API, SFC, reactivity)
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [vue, nuxt]
---

# Vue Patterns Review

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
