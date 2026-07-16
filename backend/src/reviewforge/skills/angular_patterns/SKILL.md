---
name: angular_patterns
description: Angular 代码审查规则。当审查 Angular 项目的 .ts 文件（组件/服务/Directive）时套用。检查 DI、RxJS 订阅管理、Signals、Change Detection、模板安全（[innerHTML] XSS）。
category: style
reviewer_type: style
languages: [typescript]
frameworks: [angular]
---

# Angular Patterns Review

## When to Apply
- 审查 Angular 项目的 `.ts` 组件、服务、指令、管道文件
- 审查包含 `@Component`/`@Injectable`/`@Directive` 装饰器的文件

## When NOT to Apply
- **测试文件**（`*.spec.ts`）→ 测试规则放宽
- **配置文件**（`angular.json`, `environment.*.ts`）→ 不同规则
- **生成的代码**（`*.ngfactory.ts`, 自动生成的 service proxies）→ 不审查
- **纯工具/helper 文件**（无 Angular 装饰器的 TypeScript）→ Angular 特定规则不适用；用 `code_quality` 或 `typescript_best_practices`
- **NgModule 定义文件**（仅包含 `declarations`/`imports`/`providers` 数组）→ 样板代码

## Security（必查，最高优先级）

### XSS
- Do not treat an imported helper that merely returns `SafeHtml` as a rendered XSS sink. Require a binding/mount in the changed code, or a direct `bypassSecurityTrust*` call introduced by the diff; historical helper risks are handled by cross-PR analysis.
- Angular 默认会清洗普通 `[innerHTML]` 绑定；不要仅凭绑定本身报 XSS。只有数据被包装为不可信 `SafeHtml`、调用 sanitizer bypass，或存在明确绕过证据时才报告。
- `bypassSecurityTrustHtml(userContent)` 清洗不可信数据 → **error**
- `bypassSecurityTrustScript/Style/ResourceUrl/Url` → **error**

### Client-Side Secrets
- API key、token 在组件/服务中硬编码 → **error**
- Environment 文件中的敏感值 → **error**

## Key Areas

### Change Detection
- Default: use `OnPush` strategy unless specifically needed
- Avoid function calls in template bindings — they run on every CD cycle
- `runOutsideAngular` for non-Angular async operations

### Dependency Injection
- Prefer `inject()` over constructor injection (Angular 14+)
- Services: `providedIn: 'root'` for singletons, component-level for scoped state
- Avoid circular DI (Service A → Service B → Service A)

### RxJS & Observables
- Always unsubscribe from long-lived observables
- `async` pipe auto-subscribes — prefer in templates
- No nested subscriptions; use `switchMap`/`concatMap`/`mergeMap`
- Handle errors: `catchError` prevents stream death

### Signals (Angular 16+)
- `signal()` for local reactive state; `computed()` for derived values
- `effect()` with caution; prefer declarative over imperative

### Template
- New control flow: `@if`/`@for`/`@switch` over `*ngIf`/`*ngFor` (Angular 17+)
- `trackBy` with `@for` / `*ngFor` for list stability
- Lazy-load with `loadChildren`; `@defer` for heavy sections

## Validation Criteria

**True Positive**: Causes memory leaks, CD issues, or security vulnerabilities. Confidence > 0.7.

**False Positive**:
- `*ngIf`/`*ngFor` in Angular < 17 项目（新语法不可用）
- `subscribe()` 后手动 unsubscribe — 不用 `async` pipe 但正确处理了清理
- 服务没有 `providedIn: 'root'` 而是 module-provided — 在 NgModule 架构中这是正常的
- `any` 类型在复杂的 generic 推导或第三方类型不完善时
