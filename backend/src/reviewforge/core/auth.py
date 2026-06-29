"""极简 API Token 鉴权依赖。"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException


def require_token(authorization: str = Header(default="")) -> None:
    """Bearer Token 鉴权。非 mock 模式下必填。"""
    expected = os.environ.get("REVIEWFORGE_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="API token 未配置")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
