"""Weak cryptography patterns for evaluation."""

import hashlib


def hash_password_md5(password):
    """Weak crypto: MD5 for password hashing."""
    return hashlib.md5(password.encode()).hexdigest()


def hash_password_sha1(password):
    """Weak crypto: SHA1 for password hashing."""
    return hashlib.sha1(password.encode()).hexdigest()


def verify_token(token, secret):
    """Weak: HMAC with MD5."""
    import hmac
    return hmac.new(secret.encode(), token.encode(), hashlib.md5).hexdigest()


def hash_password_bcrypt(password):
    """Safe: bcrypt. Should NOT be flagged."""
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())
