"""Small Python service used by the pinned-workspace tests."""


def normalize(value: str) -> str:
    """Normalize a value before it is handed to the service."""

    return value.strip().lower()


def run(value: str) -> str:
    return normalize(value)
