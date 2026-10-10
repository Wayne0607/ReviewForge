### Python concurrency contracts

Apply only after tracing the actual Python producer, concrete object, start method and use. Same method names do not establish stdlib behavior; custom wrappers require their own contract.

- CPython's `get_context(method).Process` creates a context-specific class. It shares `BaseProcess` with the module's default `multiprocessing.Process` facade but need not inherit that facade. Follow factory → stored object → type guard → operation; do not assume `isinstance(obj, multiprocessing.Process)` accepts every context-created process. [CPython context types](https://github.com/python/cpython/blob/3.13/Lib/multiprocessing/context.py).
- On POSIX, completed children are reaped on a subsequent process start or `active_children()`; `is_alive()` also reaps a completed child. Missing explicit `join()` alone does not prove an accumulating zombie leak. Trace completion, polling, replacement and their timing. Automatic reaping does not stop a still-running hung child. Killing can disrupt shared locks/queues; joining before queue draining can block. Cite the actual lifecycle and shared-resource binding. [Python lifecycle guidelines](https://docs.python.org/3.13/library/multiprocessing.html#programming-guidelines).
- A retry counter can deliberately limit total attempts. A reset is required only if the actual policy specifies consecutive failures or a recovery window; initialization to zero does not establish that policy. Compare before/after and the caller/test/config contract. Missing policy evidence stays UNKNOWN.

These are investigation rules, not target-repository observations or automatic verdicts. Version/platform-specific claims require the corresponding binding.
