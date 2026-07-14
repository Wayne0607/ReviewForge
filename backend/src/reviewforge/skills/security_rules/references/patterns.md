# Common Security Vulnerability Patterns

This reference covers language-agnostic security patterns.

## SQL Injection
- Any string concatenation or interpolation in SQL queries
- ORM raw query methods without parameterization
- Dynamic table/column/group-by names from user input (can't be parameterized → whitelist)

## XSS (Cross-Site Scripting)
- User input rendered into HTML without escaping
- Framework-specific sinks:
  - React: `dangerouslySetInnerHTML`
  - Vue: `v-html`
  - Angular: `bypassSecurityTrustHtml` or another explicit sanitizer bypass; plain `[innerHTML]` is sanitized by default
  - Svelte: `{@html}`
  - Vanilla: `innerHTML`, `outerHTML`, `document.write()`

## Path Traversal
- User-controlled file paths without `..` filtering or path normalization
- Check: is `os.path.join`/`path.resolve` used BEFORE accessing the file system?
- Check: is the resolved path verified to be within an allowed directory?

## Hardcoded Secrets
- API keys, tokens, passwords, private keys in source code
- Environment variable defaults that look like real credentials
- Embedded credentials in config files, CI scripts, Dockerfiles
