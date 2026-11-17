"""Precision corpus for the secret detector and the static scanner.

These detectors also drive redaction of captured code, so false positives corrupt legitimate knowledge and false
negatives leak secrets. Every line below was either a real false positive found on PatternVault's own source, or a
representative true positive.
"""

import pytest

from app.security.scanner import scan_text
from app.security.secrets import contains_secret, redact

pytestmark = pytest.mark.unit

NOT_SECRETS = [
    "op.execute(\"CREATE VIRTUAL TABLE t USING fts5(a, b, tokenize='porter unicode61')\")",
    'status = {"passed": "success", "skipped": "skipped"}.get(report.status, "failed")',
    'bypass = "enabled-for-all-users"',
    'compass = "north-north-west"',
    'secretary = "Alice Johnson"',
    'tokens_per_second = "fast-enough"',
    'passenger = "Mr Smith"',
    'sort_key = "created_at_desc"',
    'cache_key = "user:123:profile"',
    'primary_key = "identifier"',
    'password_field = "password"',
    'token = "<your-token>"',
    'api_key = "${API_KEY}"',
    "tokenizer = 'whitespace-and-punctuation'",
    'keyword = "authentication-flow"',
]
SECRETS = [
    'secretKey = "s3cr3tvalue123"',
    'apiKey: "abcd1234efgh5678"',
    'DB_PASSWORD = "hunter2hunter2"',
    "access_token: 'abcdef1234567890'",
    'client-secret = "9f8e7d6c5b4a3b2c"',
    '"password": "Tr0ub4dor&3xyz"',
    'authToken = "ZXlKaGJHY2lPaUpJVXpJMU5pSXNJblI1Y0NJNklrcFhWQ0o5"',
    "export STRIPE_SECRET_KEY=abcdef1234567890xyz",
    "passwd = 'correct-horse-battery'",
    'privateKey = "MIIEvQIBADANBgkqh"',
    "private_key: 'MIIEvQIBADANBgkqh'",
]


@pytest.mark.parametrize("line", NOT_SECRETS)
def test_identifiers_that_merely_contain_secret_words_are_not_secrets(line):
    assert not contains_secret(line), line
    assert redact(line).text == line  # legitimate code is never altered


@pytest.mark.parametrize("line", SECRETS)
def test_secret_named_identifiers_are_detected(line):
    assert contains_secret(line), line
    assert "[REDACTED:" in redact(line).text


def rules(path, text):
    return {f["rule"] for f in scan_text(path, text) if f["severity"] in ("high", "critical")}


@pytest.mark.parametrize(
    "path,text",
    [
        ("a.js", "const m = /x/.exec(html);"),
        ("a.ts", "import { spawn } from 'node:child_process';"),
        ("a.py", "m = pattern.exec(text)"),
        ("a.py", "cursor.execute('SELECT 1', ())"),
        ("a.py", "subprocess.run(['git', 'status'], check=True)"),
        ("a.ts", "execFile('git', ['status'])"),
        ("a.py", "x = parse_expression('1+1')"),
    ],
)
def test_scanner_has_no_false_positives_on_safe_code(path, text):
    assert rules(path, text) == set(), (path, text)


@pytest.mark.parametrize(
    "path,text,rule",
    [
        ("a.py", "eval(user_input)", "PV-EVAL"),
        ("a.py", "exec(code)", "PV-EVAL"),
        ("a.js", "eval(x)", "PV-EVAL"),
        ("a.js", "new Function('return ' + x)", "PV-EVAL"),
        ("a.ts", "child_process.exec(`ls ${dir}`)", "PV-CMD-INJECTION-JS"),
        ("a.ts", "execSync('rm ' + name)", "PV-CMD-INJECTION-JS"),
        ("a.py", 'cursor.execute(f"SELECT * FROM t WHERE id={uid}")', "PV-SQLI-PY"),
        ("a.py", "subprocess.run(cmd, shell=True)", "PV-SHELL-TRUE"),
        ("a.js", "el.innerHTML = userHtml", "PV-XSS-INNERHTML"),
        ("a.py", "requests.get(u, verify=False)", "PV-TLS-VERIFY"),
    ],
)
def test_scanner_detects_real_vulnerabilities(path, text, rule):
    assert rule in rules(path, text)


def test_inline_suppression_and_audit_semantics():
    code = "eval(x)  # pv-ignore: PV-EVAL\nexec(y)\nsubprocess.run(c, shell=True)  # nosec\nz = eval(q)  # pv-ignore: PV-SQLI-PY\n"
    found = [(f["rule"], f["line"]) for f in scan_text("a.py", code)]
    assert ("PV-EVAL", 1) not in found and ("PV-SHELL-TRUE", 3) not in found  # suppressed (rule-specific / blanket)
    assert ("PV-EVAL", 2) in found and ("PV-EVAL", 4) in found  # other rules and other lines are NOT suppressed


@pytest.mark.parametrize(
    "line",
    [
        'token = "probe-token-" + "x" * 24',
        'password = "pre" + suffix',
        'secret = "abc_%s" % name',
        'key_prefix_token = "tok_"',
    ],
)
def test_prefix_and_concatenated_literals_are_not_secrets(line):
    assert not contains_secret(line)
