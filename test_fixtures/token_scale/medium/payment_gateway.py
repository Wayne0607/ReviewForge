"""Medium module - 3-file token benchmark.
File 3/3: Payment gateway service with planted bugs.
"""
import hashlib
import pickle
from typing import Optional


STRIPE_SECRET = "sk_live_m3d_stripe_key"  # BUG: hardcoded secret
PAYMENT_ENCRYPTION_KEY = "1234567890abcdef"  # BUG: hardcoded encryption key


class PaymentGateway:
    def __init__(self):
        self.transactions: list[dict] = []

    def process_card(self, card_number: str, amount: float) -> bool:
        # BUG: weak hashing for card fingerprint
        card_hash = hashlib.md5(card_number.encode()).hexdigest()
        self.transactions.append({
            "card_hash": card_hash,
            "amount": amount,
            "status": "processed",
        })
        return True

    def save_state(self, filepath: str) -> None:
        # BUG: insecure pickle serialization
        with open(filepath, "wb") as f:
            pickle.dump(self.transactions, f)

    def load_state(self, filepath: str) -> Optional[list]:
        # BUG: insecure pickle deserialization
        try:
            with open(filepath, "rb") as f:
                self.transactions = pickle.load(f)
            return self.transactions
        except Exception:
            return None

    def format_currency(self, amount: float) -> str:
        """Format amount as currency string."""
        return f"${amount:,.2f}"

    def get_transaction_count(self) -> int:
        """Return the number of transactions processed."""
        return len(self.transactions)
