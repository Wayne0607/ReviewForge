---
name: svelte_patterns
description: Svelte/SvelteKit 代码审查规则。当审查 .svelte 文件或 Svelte 项目的 JS/TS 文件时套用。检查 Runes 响应式、stores、模板安全（{@html} XSS）。
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [svelte]
---

# Svelte/SvelteKit Patterns Review

## When to Apply
- 审查 `.svelte` 文件或 `.svelte.js`/`.svelte.ts` 文件
- 审查 SvelteKit 项目的组件和页面

## When NOT to Apply
- **测试文件**（`*.test.ts`, `*.spec.ts`）→ 测试规则放宽
- **配置文件**（`svelte.config.*`, `vite.config.*`）→ 不同规则
- **生成的代码** → 不审查
- **Svelte 4 旧语法**（非 runes 模式）→ 不强制用 runes
- **纯服务端代码**（`+server.ts`, `hooks.server.ts`）→ 组件规则不适用

## Security（必查，最高优先级）

### XSS
- `{@html userContent}` — 用户内容渲染为原始 HTML → **error**
- `element.innerHTML = userContent` 在 `onMount` 中 → **error**

### Client-Side Secrets
- API key、token 在组件/store 中硬编码 → **error**
- SvelteKit 的 public env vars 含敏感凭证 → **error**

## Key Areas

### Reactivity (Runes Mode — Svelte 5)
- `$state()` for reactive variables; `$state.snapshot()` for plain copies
- `$derived()` must be pure — no side effects, no mutations
- `$effect()` auto-tracks dependencies; don't over-specify
- Never mutate `$state` during `$derived` computation

### Component Design
- > 250 lines → split into components or `.svelte.js` helpers
- `$props()` with destructuring for type-safe props
- Slot props for flexible composition

### Stores
- Svelte 5: use `$state()` runes in `.svelte.js` files for shared state
- Svelte 4: `writable()`/`derived()` stores; `$storeName` auto-subscription
- Clean up manual `.subscribe()` in `onDestroy`

### Template
- `{#each}` always needs unique `key`
- `{#if}` blocks are statements, not expressions
- Use `{@const}` for template-local constants

## Validation Criteria

**True Positive**: Creates reactivity bugs, memory leaks, or XSS. Confidence > 0.7.

**False Positive**:
- `{@html}` 的内容来自可信 CMS 且已 sanitize
- Svelte 4 语法在尚未迁移的项目中
- `$effect` 中的操作是必要的初始化副作用
