# ReviewForge Notify Subsystem

Sends review completion events to user-configured channels.

## Channels

The dispatcher supports three channels:

- `email` — sends an email via the internal SMTP relay.
- `webhook` — POSTs a JSON payload to a user-supplied URL.
- `inbox` — persists the notification to the user's in-app inbox.

## Configuration

Channel-level credentials are read from the per-user `user_prefs` table. The
shared SMTP credentials live in `channels.py` and are documented inline.

## Usage

```python
from reviewforge.notify import dispatch_review_completed

result = dispatch_review_completed(
    user_id="u-42",
    review_payload={"review_id": "r-7", "findings": []},
    channels=["email", "inbox"],
)
```

## Testing

Run the dispatcher checks with:

```bash
uv run pytest backend/tests/notify -q
```

See `tests/notify/test_dispatcher.py` for the (currently minimal) coverage.

## Operations

The preferred rollout is to enable the new module behind the
`REVIEWFORGE_ENABLE_NOTIFY` flag, defaulting to off, then flip it on once
the dashboard "Notification preferences" panel lands.

## Localization

Templates currently ship in English. The localization table is owned by
the dashboard team and will be wired in once the i18n registry lands.
