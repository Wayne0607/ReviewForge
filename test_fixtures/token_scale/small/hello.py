"""Small module - single file token benchmark.
Contains exactly 1 bug to measure baseline token cost per finding.
"""


def greet(name: str) -> str:
    """Return a greeting for the given name."""
    return f"Hello, {name}!"


def calculate_score(values: list[int]) -> float:
    """Calculate average score."""
    if not values:
        return 0.0
    return sum(values) / len(values)
