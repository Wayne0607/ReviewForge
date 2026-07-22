---
name: typescript_correctness
description: Evidence-first review of observable TypeScript semantic bugs — async, nullish, unions, schemas, ORM, serialization
category: correctness
reviewer_type: correctness
languages: [typescript]
---

# TypeScript Correctness Review

Review changed behavior for concrete semantic bugs. Report only when a trigger in the diff leads to an observable wrong result supported by repository evidence.

## Evidence rules

- Quote the smallest code fragment and trace one concrete input to a wrong result.
- For wrong argument/callee claims, search for declarations or require two sibling calls agreeing on the contract.
- Do not report naming, formatting, type annotations, style, missing tests, speculative robustness, or refactoring.

## High-signal defects

### Promise/async lifecycle

- Unawaited async in `.forEach`/`.map` callback — fire-and-forget when sequential completion was intended.
- Missing `await` on a promise that sets shared state — race with subsequent reads.
- `Promise.all` vs `Promise.allSettled` — does the caller handle rejection or discard all on one failure?
- Async cleanup in `finally`/`onUnmount` not awaited — teardown silently skipped.

### Nullish vs falsy/default semantics

- `value || default` when `value` can be `0`, `""`, `false` — use `value ?? default` for null/undefined only.
- Destructuring `{ prop = fallback }` — default activates on `undefined`, NOT `null`.
- Optional chaining `obj?.prop` returning `undefined` fed into a function that rejects it.

### Union narrowing and runtime validation

- `as` cast on external input bypassing runtime check — narrowed type is a lie if input differs.
- Discriminated union without exhaustiveness check — missing branch silently falls through.
- Type predicate `(x is T)` that is too permissive — narrows incorrectly.

### Zod schema: transform/default/optional

- `.optional()` (undefined) vs `.nullable()` (null) — check which the runtime actually sends.
- `.default()` only activates on `undefined`, not missing keys in strict mode.
- `.transform()` discards the original type — downstream code expecting input shape gets output shape.

### Prisma/ORM atomicity and transaction boundaries

- Multiple operations that must succeed together but lack `$transaction` — partial commit on failure.
- `findFirst` (returns null) vs `findUnique` (throws) — does caller handle the null case?
- `$queryRaw` with string interpolation — SQL injection. Use tagged template.
- `update` with relation connect referencing non-existent ID — FK violation at runtime.

### OAuth/token refresh state

- Token refresh without lock/queue — concurrent refreshes invalidate each other.
- Stale token used after refresh — pending request used old token while new one is in memory.
- Refresh token rotation without persisting new token — next refresh fails.

### Date/timezone/unit comparisons

- `new Date(string)` parses UTC/local depending on format; `new Date(y,m,d)` is always local — mixing produces off-by-one-day.
- Comparing dates with `===` compares identity, not value — use `.getTime()`.
- Milliseconds vs seconds — `Date.now()` returns ms, many APIs expect seconds.
- `toLocaleDateString()` without explicit `timeZone` — differs between server and client.

### Server/client serialization boundaries

- `Date` serialized becomes string — client receives string, not Date.
- `BigInt` cannot be `JSON.stringify` — throws or stringifies depending on engine.
- `undefined` dropped by `JSON.stringify` — optional fields disappear from payload.
- Class instances lose methods during serialization — only data survives.

### Object spread/mutation

- Shallow spread `{ ...obj }` does not clone nested objects — nested mutations affect original.
- Spread order: `{ ...defaults, ...input }` vs `{ ...input, ...defaults }` — last one wins.
- Mutating a parameter object directly — caller's reference is affected.

### Wrong caller/callee/argument/return contracts

- Arguments in wrong order when types are compatible (two strings, two numbers).
- Returning wrong unit (seconds vs ms) or shape (array vs single object).
- Wrong method on similar interface — `find` vs `filter`, `some` vs `every`.

## Validation criteria

**True positive**: Reachable with realistic input AND produces observable wrong result. Confidence > 0.7.

**False positive**: Type annotations/naming/style; test/example/generated code; speculative "might fail if..."; explicit guard or comment; missing tests/documentation.
