"""Code injection patterns (eval/exec) for evaluation."""


def calculate(expr):
    """Code injection via eval."""
    return eval(expr)


def run_code(code_str):
    """Code injection via exec."""
    exec(code_str)


def dynamic_import(module_name):
    """Code injection via __import__."""
    return __import__(module_name)


def safe_calc(expr):
    """Safe: ast.literal_eval. Should NOT be flagged."""
    import ast
    return ast.literal_eval(expr)
