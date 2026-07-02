---
name: svelte_patterns
description: Svelte code review rules (runes, reactivity, stores)
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [svelte]
---

# Svelte Patterns Review

## Key Areas

### Reactivity (Runes Mode)
- Use `$state()` for reactive variables; `$state.snapshot()` to get a plain object copy
- `$derived()` for computed values; must be pure (no side effects, no mutations)
- `$effect()` for side effects; Svelte auto-tracks dependencies — don't over-specify
- Avoid mutating `$state` during `$derived` computation; this causes infinite loops

### Component Design
- Components > 250 lines should be split into smaller components or reusable helpers
- Use `$props()` with destructuring for clean prop declarations: `let { name, count = 0 } = $props()`
- Slot props for flexible composition; prefer named slots over deeply nested prop drilling
- Avoid exporting mutable state from components directly; use callback props or stores

### Stores
- Use `$state()` runes in `.svelte.js` files for shared reactive state (Svelte 5)
- For Svelte 4: use `writable()` / `derived()` stores; subscribe with `$storeName` auto-subscription
- Never mutate a store directly from multiple unrelated components without clear ownership
- Clean up store subscriptions in `onDestroy` if using manual `.subscribe()`

### Template
- `{#each}` blocks always need a unique `key` expression
- `{@html}` is an XSS risk — only use with sanitized content
- `{#if}`/`{#each}` blocks are statements, not expressions; can't be used inline
- Use `{@const}` for local constants in templates to avoid recomputation

### Performance
- Svelte compiles away the framework — don't over-optimize reactivity
- Large loops in `$derived`/`$effect` on frequently-updated state can be costly
- Use `$effect.pre` for DOM reads before paint to avoid layout thrashing

## Validation Criteria

**True Positive**: The pattern creates reactivity bugs, memory leaks, or XSS vulnerabilities.

**False Positive**: The pattern is a deliberate SvelteKit convention or compiler-optimized code.
