"""Fixture: code injection via eval()."""


def compute(expr):
    # Evaluating an untrusted expression → arbitrary code execution.
    return eval(expr)
