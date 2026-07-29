"""Notification subsystem for ReviewForge.

Dispatches review completion events to configured channels (email, webhook,
in-app inbox). The dispatcher is intentionally decoupled from the engine so
that it can be reused by other long-running workflows.
"""

from reviewforge.notify.dispatcher import (
    NotificationDispatcher,
    NotificationResult,
    dispatch_review_completed,
)
from reviewforge.notify.preferences import (
    UserPreferences,
    load_user_preferences,
)

__all__ = [
    "NotificationDispatcher",
    "NotificationResult",
    "UserPreferences",
    "dispatch_review_completed",
    "load_user_preferences",
]
