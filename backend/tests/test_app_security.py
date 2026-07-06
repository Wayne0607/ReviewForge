from reviewforge.app import _is_sensitive_fallback_path


def test_spa_fallback_blocks_sensitive_probe_paths():
    assert _is_sensitive_fallback_path("/.env")
    assert _is_sensitive_fallback_path("/backend/.env")
    assert _is_sensitive_fallback_path("/.git/config")
    assert _is_sensitive_fallback_path("/wp-config.php")


def test_spa_fallback_allows_normal_frontend_routes():
    assert not _is_sensitive_fallback_path("/")
    assert not _is_sensitive_fallback_path("/reviews/abc123")
    assert not _is_sensitive_fallback_path("/dashboard")
