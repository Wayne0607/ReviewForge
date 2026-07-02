---
name: angular_patterns
description: Angular code review rules (components, DI, RxJS)
category: style
reviewer_type: style
languages: [typescript]
frameworks: [angular]
---

# Angular Patterns Review

## Key Areas

### Component Design
- Use `OnPush` change detection strategy by default for performance
- Avoid complex logic in templates; move to component class or pipes
- Inputs: prefer `input()` signal-based API (Angular 17+); use `@Input()` for older versions
- Avoid calling functions in template bindings — they execute on every change detection cycle

### Dependency Injection
- Prefer `inject()` function over constructor injection for cleaner code (Angular 14+)
- Services should be provided at the appropriate level: `root` for singletons, component-level for scoped state
- Avoid injecting components into services — creates circular dependencies
- Use injection tokens for non-class dependencies to maintain type safety

### RxJS & Observables
- Always unsubscribe from long-lived observables: use `takeUntilDestroyed()`, `async` pipe, or `Subscription.add()`
- The `async` pipe auto-subscribes and unsubscribes — prefer it in templates
- Avoid nested subscriptions; use `switchMap`/`concatMap`/`mergeMap` for flattening
- Don't use `subscribe()` just to set a local variable; use `| async` or signals
- Handle errors in observable chains with `catchError` — unhandled errors kill the stream

### Signals (Angular 16+)
- Use `signal()` for local reactive state; `computed()` for derived values
- `effect()` for side effects; prefer declarative patterns over imperative effects
- Don't mix signals and RxJS without explicit conversion: `toSignal()` / `toObservable()`

### Template
- Avoid calling methods from templates that have side effects
- `[innerHTML]` binding is an XSS risk; use `DomSanitizer` only with trusted content
- Use `@defer` blocks for lazy-loading heavy template sections (Angular 17+)
- Structural directives: prefer `@if`/`@for` control flow over `*ngIf`/`*ngFor` (Angular 17+)

### Performance
- Use `trackBy` with `@for` / `*ngFor` for stable DOM reuse in lists
- Lazy-load feature modules with `loadChildren`; avoid eager-loading everything
- Beware of memory leaks from non-cancelled subscriptions and un-detached event listeners
- `runOutsideAngular` for non-Angular async operations to avoid triggering change detection

## Validation Criteria

**True Positive**: The pattern causes memory leaks, change detection issues, or security vulnerabilities.

**False Positive**: The pattern is required by a specific Angular version constraint or library integration.
