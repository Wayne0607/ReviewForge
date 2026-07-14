# Frontend Security Patterns

## Cross-Site Scripting (XSS)

### React
- `dangerouslySetInnerHTML={{ __html: userContent }}` — direct HTML injection
- `ref.current.innerHTML = userContent` — same risk via refs
- `href="javascript:..."` in links with user-controlled values
- Check: is user content escaping properly applied before `dangerouslySetInnerHTML`?

### Vue
- `v-html="userContent"` — renders raw HTML, bypassing escaping
- Dynamic component with user-controlled name: `<component :is="userComponent">`
- SSR: server-rendered content with unescaped user data
- Check: use `v-text` or `{{ }}` interpolation instead (auto-escapes)

### Angular
- Ordinary `[innerHTML]="userContent"` is sanitized by Angular; do not report it alone
- `bypassSecurityTrustHtml(userContent)` — explicitly marks as safe, skipping sanitization
- `bypassSecurityTrustScript()`, `bypassSecurityTrustStyle()`, `bypassSecurityTrustResourceUrl()`, `bypassSecurityTrustUrl()`
- Check: only use bypass functions with trusted, static content (never user input); require bypass evidence rather than treating every `[innerHTML]` binding as vulnerable

### Svelte
- `{@html userContent}` — raw HTML rendering
- Manual DOM manipulation in `onMount`: `element.innerHTML = ...`
- Check: use `{userContent}` (auto-escapes) unless content is sanitized

## Client-Side Data Exposure

- `localStorage.setItem('token', jwt)` — tokens accessible to any JS on the page (XSS → token theft)
- `sessionStorage` is slightly better (cleared on tab close) but still accessible via XSS
- Console.log of sensitive data: `console.log(user)` with PII
- Source maps in production exposing source code
- `.env` variables prefixed with `NEXT_PUBLIC_` / `VITE_` / `REACT_APP_` are bundled into client JS

## Open Redirect

- `window.location.href = userParam` — redirect to attacker-controlled URL
- `window.location.assign(userParam)` / `window.location.replace(userParam)`
- Next.js: `router.push(userParam)` without validation
- Safe: validate against allowlist of known paths; use relative paths only

## CSRF Protection

- State-changing requests (POST, PUT, DELETE) without CSRF token or SameSite cookie
- CORS misconfiguration: `Access-Control-Allow-Origin: *` with credentials
- Safe: SameSite=Strict/Lax cookies, CSRF tokens, Origin/Referer header validation
