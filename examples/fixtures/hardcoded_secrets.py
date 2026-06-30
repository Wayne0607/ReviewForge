"""Hardcoded secrets patterns for evaluation."""

# Hardcoded API keys
OPENAI_API_KEY = "sk-proj-abc123def456ghi789jkl012mno345pqr678stu901"
AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
DATABASE_PASSWORD = "admin123456"
JWT_SECRET = "super-secret-jwt-key-do-not-share"

# Hardcoded in function
def connect_db():
    """Hardcoded credentials in function body."""
    password = "root123"
    conn_str = f"postgresql://admin:{password}@localhost/mydb"
    return conn_str


# Hardcoded token in dict
CONFIG = {
    "api_key": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "secret": "my-github-secret-token",
    "debug": True,
}
