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
