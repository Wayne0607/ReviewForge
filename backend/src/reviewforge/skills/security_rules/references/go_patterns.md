# Go-Specific Security Patterns

## Command Injection
- `exec.Command()` with user-controlled input as command name or arguments
- Shell: `exec.Command("bash", "-c", userInput)` — bypasses argument separation
- Check: is user input passed to `exec.Command` without whitelisting?
- Safe pattern: use `exec.Command("fixed", "--flag", userInput)` where userInput is a single argument, never `-c`
- `os/exec` combined with user-controlled env vars: `cmd.Env = append(os.Environ(), "USER_VAR="+userInput)`

## SQL Injection
- `db.Query(fmt.Sprintf("SELECT * FROM users WHERE name = '%s'", userInput))`
- `db.Exec("UPDATE ... WHERE id = " + userInput)`
- ORM bypass with raw SQL: `.Raw("SELECT ... WHERE " + cond)`
- Check: is every `?` placeholder paired with an argument? No string concatenation in query strings?
- Safe pattern: `db.Query("SELECT * FROM users WHERE name = ?", userInput)`

## XSS in Templates
- `template.HTML(userInput)` — marks string as safe HTML, bypassing escaping
- `template.JS(userInput)` / `template.CSS(userInput)` / `template.URL(userInput)` — same for JS/CSS/URL contexts
- `html/template` auto-escapes by default; `text/template` does NOT — use the right package
- Check: is `template.HTML()` receiving user-generated content without sanitization?

## Unsafe Usage
- `unsafe.Pointer` conversions that break type safety
- `unsafe.String` / `unsafe.StringData` misuse
- `reflect` + `unsafe` combinations for accessing unexported fields
- CGo: `C.CString` memory leaks (must call `C.free`)
