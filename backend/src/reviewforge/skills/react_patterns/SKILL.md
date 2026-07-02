---
name: react_patterns
description: React/Next.js 代码审查规则。当审查 JSX/TSX 文件（React 项目）时套用。检查组件设计、Hooks 使用、状态管理、性能、安全（XSS）。
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [react, next]
---

# React/Next.js Patterns Review

## When to Apply
- 审查 React 项目的 `.jsx`/`.tsx` 文件
- 审查 Next.js 项目的页面和组件

## When NOT to Apply
- **测试文件**（`*.test.tsx`, `*.spec.tsx`, `__tests__/`）→ 测试组件简化规则
- **Storybook stories**（`*.stories.tsx`）→ 演示代码
- **生成的类型定义**（`*.d.ts`）→ 不审查
- **配置文件**（`next.config.*`, `vite.config.*`）→ 不同规则
- **第三方组件库的内部代码**（`node_modules/`）→ 不审查
- **服务端代码**（`*.server.ts`, API routes 的纯数据处理逻辑）→ Hooks 规则不适用

## Security（必查，最高优先级）

### XSS
- `dangerouslySetInnerHTML={{ __html: userContent }}` → **error**
- `ref.current.innerHTML = userContent` → **error**
- `href="javascript:{userInput}"` → **error**
- `eval()` / `new Function()` 在客户端代码中 → **error**

### Client-Side Secrets
- API key、token 在组件代码中硬编码 → **error**
- `NEXT_PUBLIC_*` 环境变量含敏感凭证 → **error**（被打包进客户端 JS）

## Key Areas

### Component Design
- Components > 200 lines should be split
- Props interface should be explicit (no `any`)
- Avoid inline object/array literals in JSX (causes re-renders)

### Hooks
- Missing dependency arrays in `useEffect` → **warning**
- Hooks called conditionally → **error** (violates Rules of Hooks)
- Stale closure bugs → **warning**
- `useCallback`/`useMemo` 过度使用在简单场景 → **info**（不用反而更快）

### State Management
- Unnecessary `useState` for derived values → **warning**（应用 `useMemo` 或直接计算）
- Race conditions in async effects (no cleanup/abort) → **warning**
- Missing loading/error states in data fetching → **warning**

### Performance
- Missing `React.memo` for expensive pure components
- Large bundle imports (import all of lodash) → **warning**
- Missing `key` prop in list rendering → **error**

## Validation Criteria

**True Positive**: Causes bugs, performance problems, or security issues. Confidence > 0.7.

**False Positive**:
- `useCallback`/`useMemo` 未在简单组件中使用 — 过早优化反而有害
- `useEffect` 空依赖数组但内部只用 props/state 的初始值 — 可能是故意的
- 条件渲染中没有 loading/error 但数据由 Suspense/ErrorBoundary 处理
- `any` 类型在复杂的泛型推导场景中有时是唯一可行方案
