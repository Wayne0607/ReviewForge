"""Session handling (PR-C of the cross-PR demo).

Imports cache_load from demo_app.db (the risky primitive added in PR-A) and
loads an untrusted client cookie through it -> cross-PR insecure-deserialization.
Also defines token_hash() which is a CLEAN control (sha256 is appropriate here).
"""

import hashlib

from demo_app.db import cache_load


def load_session(cookie_bytes):
    # VULN: cookie_bytes comes from the client and is pickle-loaded via cache_load
    return cache_load(cookie_bytes)


def token_hash(token: str) -> str:
    # CLEAN: sha256 of a random session token is fine; should NOT be flagged weak-crypto
    return hashlib.sha256(token.encode()).hexdigest()
