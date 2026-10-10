## Defect scope

Review changed behavior that violates a supported contract and causes a concrete incorrect result. Answering a factual question is not proof of a defect: establish the obligation and the changed consequence for the same witness.

Missing test assertions, extra coverage, documentation improvements, style and naming alone are outside scope. A changed test can be defective if it now fails incorrectly or masks an observed failure; merely lacking an assertion is a request for more coverage. A method for obtaining an exit code proves no obligation for every test to assert it. Do not infer a required policy from the candidate's claim or from absence of an operation.

Use actual caller/test/config contracts or a supplied standard bound to the concrete implementation. Keep unproved cases UNKNOWN; never invent an expected obligation to match a factual answer. This scope is review policy, not repository Observation evidence.
