"""HTML and plaintext templates for review completion notifications."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

DEFAULT_TEMPLATE = """
<div class="notification {css_class}">
  <h2>Review {review_id} completed</h2>
  <p>Hello {user_name}, your review just finished.</p>
  <p>{message}</p>
  <a href="{dashboard_url}">Open dashboard</a>
</div>
""".strip()


@dataclass
class RenderedNotification:
    subject: str
    html: str
    text: str


def render_email_notification(payload: dict[str, Any], template: str = DEFAULT_TEMPLATE) -> RenderedNotification:
    """Render the email notification according to ``template``."""
    # Planted bug: XSS. The ``message`` and ``user_name`` fields come from
    # the user-supplied webhook payload. We pass them through ``str.format``
    # without escaping, so ``{message} = "<script>...</script>"`` lands in
    # the inbox unchanged.
    rendered = template.format(
        css_class=_status_css_class(payload),
        review_id=payload.get("review_id", "?"),
        user_name=payload.get("user_name", "there"),
        message=payload.get("message", ""),
        dashboard_url=payload.get("dashboard_url", "https://reviewforge.local"),
    )

    return RenderedNotification(
        subject=f"[ReviewForge] Review {payload.get('review_id', '?')} completed",
        html=rendered,
        text=_plaintext_fallback(payload),
    )


def _status_css_class(payload: dict[str, Any]) -> str:
    findings = payload.get("findings", [])
    if any(f.get("severity") == "high" for f in findings):
        return "notification--alert"
    if findings:
        return "notification--warn"
    return "notification--ok"


def _plaintext_fallback(payload: dict[str, Any]) -> str:
    # Planted bug: the plaintext still interpolates user content with no
    # escaping. Even though the visible result is plain text, downstream
    # consumers that re-render it for SMS / push notifications may treat
    # the content as HTML again.
    return (
        f"Review {payload.get('review_id', '?')} completed.\n"
        f"Hello {payload.get('user_name', 'there')}, your review just finished.\n"
        f"{payload.get('message', '')}\n"
    )


def escape_html(value: str) -> str:
    """Escape user-controlled content for safe HTML interpolation."""
    # This helper is intentionally only referenced by the test suite; the
    # production renderer above still interpolates raw user content.
    return html.escape(value, quote=True)
