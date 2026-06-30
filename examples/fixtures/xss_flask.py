"""XSS patterns in Flask for evaluation."""

from flask import Flask, request, make_response

app = Flask(__name__)


@app.route("/unsafe")
def unsafe_page():
    """XSS: user input directly in HTML response."""
    name = request.args.get("name", "")
    return f"<h1>Hello {name}</h1>"


@app.route("/unsafe2")
def unsafe_page2():
    """XSS: user input in JavaScript context."""
    query = request.args.get("q", "")
    return f"<script>var search = '{query}';</script>"


@app.route("/unsafe3")
def unsafe_page3():
    """XSS: user input via string concatenation."""
    username = request.args.get("user", "")
    html = "<div>Welcome, " + username + "!</div>"
    return html


@app.route("/safe")
def safe_page():
    """Safe: uses Markup escape. Should NOT be flagged."""
    from markupsafe import escape
    name = request.args.get("name", "")
    return f"<h1>Hello {escape(name)}</h1>"
