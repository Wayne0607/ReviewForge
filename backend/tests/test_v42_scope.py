import pytest

from reviewforge.review_v42.contracts import Run
from reviewforge.review_v42.scope import compile_scopes


def test_scope_retains_overlaps_and_unsupported():
    run = Run(id="r", repo="x/y", base="base", head="head")
    regions = [
        {"id": "a", "path": "x", "patch": "full", "start_line": 1, "end_line": 4},
        {"id": "b", "path": "x", "patch": "next", "start_line": 2, "end_line": 5},
        {"id": "c", "path": "x.bin", "unsupported": True},
    ]
    assert compile_scopes(run, regions)[0].regions == regions
    with pytest.raises(ValueError):
        compile_scopes(run, [{"id": "a", "path": "x"}, {"id": "a", "path": "y"}])
