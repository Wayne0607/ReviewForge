from python.service import normalize


def test_normalize() -> None:
    assert normalize(" Value ") == "value"
