"""Test plan: security/secrets.py
- each provider token format is detected and replaced by [REDACTED:kind]
- generic key=value assignments are redacted; placeholders / env lookups / code expressions are NOT
- redaction is idempotent, preserves surrounding text and reports 1-based line numbers
- no false positives on ordinary code
"""

import pytest

from app.security.secrets import contains_secret, find_secrets, redact, redact_obj

pytestmark = pytest.mark.unit

# Synthetic, non-functional tokens built at runtime so this file doesn't itself look like a leak.
FAKE = {
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
    "github": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "slack": "xoxb-" + "123456789012-abcdefghijkl",
    "stripe": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "google": "AIza" + "SyA-1234567890abcdefghijklmnopqrstu",
    "anthropic": "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "openai": "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "jwt": "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
}


@pytest.mark.parametrize(
    "kind,value",
    [
        ("aws-access-key-id", FAKE["aws"]),
        ("github-token", FAKE["github"]),
        ("slack-token", FAKE["slack"]),
        ("stripe-key", FAKE["stripe"]),
        ("google-api-key", FAKE["google"]),
        ("anthropic-key", FAKE["anthropic"]),
        ("openai-key", FAKE["openai"]),
        ("jwt", FAKE["jwt"]),
    ],
)
def test_provider_tokens_detected(kind, value):
    text = f"token is {value} ok"
    res = redact(text)
    assert value not in res.text
    assert f"[REDACTED:{kind}]" in res.text
    assert res.count == 1


def test_private_key_block_redacted_fully():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabcdef\n-----END RSA PRIVATE KEY-----"
    res = redact(f"before\n{pem}\nafter")
    assert "MIIEow" not in res.text and "before" in res.text and "after" in res.text
    assert res.findings[0].kind == "private-key"


def test_truncated_private_key_still_redacted():
    res = redact("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk")
    assert "b3BlbnNz" not in res.text


@pytest.mark.parametrize(
    "line",
    [
        'password = "Sup3rS3cret!"',
        "DB_PASSWORD: 'correct-horse-battery'",
        '"api_key": "abcd1234efgh5678"',
        "client_secret='9f8e7d6c5b4a'",
        "export API_TOKEN=abcdef1234567890",
        "AUTH_TOKEN=zzzzzzzz1234567",
    ],
)
def test_assignments_redacted(line):
    res = redact(line)
    assert "[REDACTED:" in res.text, res.text


@pytest.mark.parametrize(
    "line",
    [
        'password = "your_password_here"',
        'api_key = "<API_KEY>"',
        'secret = os.environ["SECRET"]',
        "token = process.env.TOKEN",
        'password = "${DB_PASSWORD}"',
        'api_key = "changeme"',
        "password = request.form['password']",
        'password_field = "password"',
        "token = get_token()",
        'api_key = "xxxxxxxx"',
    ],
)
def test_placeholders_and_expressions_not_redacted(line):
    assert not contains_secret(line), line


def test_url_credentials_redacted_user_kept():
    res = redact("postgres://admin:s3cr3tpw@db.internal:5432/app")
    assert "s3cr3tpw" not in res.text and "admin" in res.text and "db.internal" in res.text


def test_bearer_header_redacted():
    res = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnop" not in res.text


def test_idempotent():
    once = redact(f"k={FAKE['github']}\npassword='hunter2hunter2'").text
    assert redact(once).text == once


def test_line_numbers_are_one_based():
    text = f"line1\nline2\nkey = {FAKE['aws']}\n"
    assert find_secrets(text)[0].line == 3


def test_ordinary_code_untouched():
    code = """
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())

SECRET_KEY = settings.SECRET_KEY
token_length = 32
user = User(name="alice", email="a@example.com")
sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
"""
    res = redact(code)
    assert res.text == code and res.count == 0


def test_redact_obj_recurses():
    obj = {"a": [f"x {FAKE['github']}"], "b": {"c": 5, "d": f"{FAKE['aws']}"}}
    out = redact_obj(obj)
    assert FAKE["github"] not in str(out) and FAKE["aws"] not in str(out) and out["b"]["c"] == 5
