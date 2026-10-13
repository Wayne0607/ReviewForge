## Find the operation's contract

For an authorization claim, a helper's role predicate proves its return value,
not who may perform the target operation. Nor does another endpoint's policy.
Follow the actual route and consumer: which roles receive the relevant objects,
which action is exposed to them, and which request that action invokes. Read
the binding windows and guards; a UI label alone proves no authorization.
Derive a separate truth row for each concrete role from helper implementations
(one helper may include another role). Bind expected and actual behavior to
the SAME operation and supported role. If that binding is missing, spend the
next useful step finding the real caller or test, not rereading the predicate.

`find_callers` looks for direct call expressions, not a complete dependency
graph. A `not_found` result only describes that lookup; aliases, callbacks,
route/property bindings and JSX consumers may still exist. Use `grep` on a
symbol or property already visible in source to find those references, then
`read_file` for the binding and guards. A reference is navigation, not proof
that a call executes. Do not infer a new alias or caller from a name alone.
If `source_window.truncated` is true, only the named saved references are
evidence. Narrow the read/search to record any needed omitted source.
