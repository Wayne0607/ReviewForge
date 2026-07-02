---
name: react_patterns
description: React and TypeScript code review rules
category: style
reviewer_type: style
languages: [typescript, javascript]
frameworks: [react, next]
---

# React Patterns Review

## Key Areas

### Component Design
- Components > 200 lines should be split
- Props interface should be explicit (no `any`)
- Avoid inline object/array literals in JSX (re-render cause)

### Hooks
- Missing dependency arrays in useEffect
- Hooks called conditionally
- Stale closure bugs

### State Management
- Unnecessary useState for derived values
- Missing loading/error states
- Race conditions in async effects

### Performance
- Missing React.memo for expensive components
- Missing useMemo/useCallback for expensive computations
- Large bundle imports (importing entire lodash)

### TypeScript
- `any` type usage (unless justified)
- Missing return types on complex functions
- Type assertions (`as`) without validation

## Validation Criteria

**True Positive**: The issue causes bugs, performance problems, or maintenance burden.

**False Positive**: The pattern is intentional for simplicity, or it's in a prototype/demo file.
