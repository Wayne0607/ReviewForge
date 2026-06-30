"""Missing CSRF protection patterns for evaluation."""

from flask import Flask, request

app = Flask(__name__)


@app.route("/transfer", methods=["POST"])
def transfer_money():
    """CSRF: state-changing POST without CSRF token validation."""
    amount = request.form.get("amount")
    to_account = request.form.get("to")
    # No CSRF token check
    return do_transfer(amount, to_account)


@app.route("/delete", methods=["POST"])
def delete_account():
    """CSRF: destructive action without CSRF protection."""
    user_id = request.form.get("user_id")
    # No CSRF token check
    delete_user(user_id)
    return "Deleted"


def do_transfer(amount, to_account):
    """Stub."""
    return f"Transferred {amount} to {to_account}"


def delete_user(user_id):
    """Stub."""
    pass
