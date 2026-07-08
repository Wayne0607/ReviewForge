"""Cross-PR negative control: import only the historically safe symbol."""

from cross_pr_live.risky_ops import normalize_account_id


def build_account_label(raw_account_id: str) -> str:
    account_id = normalize_account_id(raw_account_id)
    return f"account:{account_id}"
