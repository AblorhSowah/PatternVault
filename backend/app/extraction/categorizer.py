"""Explainable, rule-based pattern categorisation.

Every category has weighted evidence rules (path, imports/identifiers, prompt keywords). The result carries
the evidence so the UI can show *why* something was classified the way it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CategoryScore:
    category: str
    score: float
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Rule:
    category: str
    pattern: re.Pattern[str]
    weight: float
    scope: str  # path | code | prompt
    label: str


def _r(category: str, pattern: str, weight: float, scope: str, label: str) -> _Rule:
    return _Rule(category, re.compile(pattern, re.I), weight, scope, label)


RULES: tuple[_Rule, ...] = (
    # authentication
    _r("authentication", r"(^|/)(auth|login|signin|session|sanctum|passport)[\w./-]*", 3, "path", "auth-related path"),
    _r(
        "authentication",
        r"\b(login|logout|signin|sign_in|authenticate|jwt|oauth|password_hash|bcrypt|argon2|sanctum|passport|csrf|session_start|remember_me|refresh_token|access_token)\b",
        2,
        "code",
        "authentication identifiers",
    ),
    _r(
        "authentication",
        r"\b(login|log in|sign ?in|authenticat\w+|jwt|oauth|sso|password reset|register user|signup)\b",
        3,
        "prompt",
        "authentication in request",
    ),
    # authorization
    _r(
        "authorization",
        r"(^|/)(policies|policy|permissions?|roles?|gates?|rbac|acl|guards?)[\w./-]*",
        3,
        "path",
        "authorization-related path",
    ),
    _r(
        "authorization",
        r"\b(authorize|can\(|hasRole|has_role|has_permission|permission_required|Gate::|policy|rbac|isAdmin|is_admin|require_role)\b",
        2,
        "code",
        "authorization identifiers",
    ),
    _r(
        "authorization",
        r"\b(authori[sz]ation|permissions?|roles?|rbac|access control|admin only)\b",
        3,
        "prompt",
        "authorization in request",
    ),
    # database
    _r(
        "database-schema",
        r"(^|/)(migrations?|alembic|schema|models?|entities)[\w./-]*",
        3,
        "path",
        "schema/migration path",
    ),
    _r(
        "database-schema",
        r"\b(create_table|CREATE TABLE|Schema::create|op\.create_table|ForeignKey|relationship\(|Column\(|migrate|hasMany|belongsTo|ALTER TABLE|@Entity)\b",
        2,
        "code",
        "schema definitions",
    ),
    _r(
        "database-schema",
        r"\b(database|schema|migration|table|model|orm|relationship|foreign key|index(es)?)\b",
        2,
        "prompt",
        "database in request",
    ),
    # api
    _r(
        "api-endpoint",
        r"(^|/)(routes?|routers?|controllers?|endpoints?|api|handlers?|views)[\w./-]*",
        2,
        "path",
        "api/controller path",
    ),
    _r(
        "api-endpoint",
        r"@(app|router)\.(get|post|put|delete|patch)|Route::(get|post|put|delete|apiResource|resource)|\b(APIRouter|express\.Router|@RestController|@(Get|Post)Mapping|HttpResponse|JsonResponse|res\.json)\b",
        3,
        "code",
        "route/handler definitions",
    ),
    _r("api-endpoint", r"\b(api|endpoint|rest|graphql|route|controller|crud)\b", 2, "prompt", "api in request"),
    # error handling
    _r(
        "error-handling",
        r"\b(try\s*[:{]|catch\s*\(|except\s+\w|raise\s+\w|throw\s+new|HTTPException|abort\(|errorHandler|ExceptionHandler|on_error|logging\.exception)\b",
        1.5,
        "code",
        "exception handling constructs",
    ),
    _r(
        "error-handling",
        r"\b(error handling|exceptions?|retry|fallback|graceful|resilien\w+)\b",
        3,
        "prompt",
        "error handling in request",
    ),
    # testing
    _r(
        "testing",
        r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|(\.|_)(test|spec)\.|Test\.(php|java|cs)$",
        4,
        "path",
        "test file path",
    ),
    _r(
        "testing",
        r"\b(pytest|unittest|describe\(|it\(|expect\(|assert\w*\(|@Test|TestCase|beforeEach|mock|fixture)\b",
        2,
        "code",
        "test constructs",
    ),
    _r("testing", r"\b(tests?|testing|unit test|coverage|tdd)\b", 2, "prompt", "testing in request"),
    # validation
    _r(
        "validation",
        r"\b(validate|validator|Validator|pydantic|BaseModel|zod|joi|yup|FormRequest|rules\(\)|@Valid|serializers?)\b",
        2,
        "code",
        "validation constructs",
    ),
    _r("validation", r"\b(validation|validate|sanitiz\w+|input checks?)\b", 3, "prompt", "validation in request"),
    # configuration / devops
    _r(
        "configuration",
        r"(^|/)(config|settings|configuration|\.github/workflows|docker|compose|k8s|helm)[\w./-]*|(Dockerfile|docker-compose[\w.-]*\.ya?ml|\.env\.example|pyproject\.toml|package\.json|composer\.json)$",
        2,
        "path",
        "configuration path",
    ),
    _r(
        "configuration",
        r"\b(config|configuration|settings|environment variables?|docker|deploy\w*|ci/cd|pipeline)\b",
        2,
        "prompt",
        "configuration in request",
    ),
    # ui
    _r(
        "ui-component",
        r"(^|/)(components?|pages?|views?|templates?|resources/views|ui|widgets)(/|$)|\.(vue|svelte|tsx|jsx|blade\.php|html)$",
        3,
        "path",
        "ui path",
    ),
    _r(
        "ui-component",
        r"\b(useState|useEffect|defineComponent|@Component|render\(|<template>|className=|v-model|@click)\b",
        2,
        "code",
        "ui constructs",
    ),
    _r(
        "ui-component",
        r"\b(ui|component|page|form|button|modal|dashboard|layout|frontend|styling)\b",
        2,
        "prompt",
        "ui in request",
    ),
    # background jobs / queues
    _r("background-jobs", r"(^|/)(jobs?|queues?|workers?|tasks?|schedul\w+)(/|\.)", 3, "path", "job/queue path"),
    _r(
        "background-jobs",
        r"\b(celery|@shared_task|ShouldQueue|dispatch\(|BullMQ|cron|schedule\w*|asyncio\.create_task|queue\.add)\b",
        2,
        "code",
        "job constructs",
    ),
    _r("background-jobs", r"\b(background (job|task)|queue|worker|cron|scheduled?)\b", 3, "prompt", "jobs in request"),
    # caching / performance
    _r(
        "caching", r"\b(cache|redis|memcached|lru_cache|@cached|Cache::remember|ttl)\b", 2, "code", "caching constructs"
    ),
    _r("caching", r"\b(caching|cache|redis|performance|memoi[sz]\w+)\b", 3, "prompt", "caching in request"),
    # logging / observability
    _r(
        "logging",
        r"\b(logging\.getLogger|logger\.(info|error|warning|debug)|console\.(log|error)|Log::(info|error)|structlog|winston|pino)\b",
        1.5,
        "code",
        "logging constructs",
    ),
    _r("logging", r"\b(logging|logs?|observability|metrics|tracing|monitoring)\b", 2, "prompt", "logging in request"),
    # file handling / integrations
    _r(
        "integration",
        r"\b(requests\.(get|post)|httpx|axios|fetch\(|curl_|Http::|stripe|twilio|webhook|smtp|sendgrid|boto3|s3)\b",
        2,
        "code",
        "external service calls",
    ),
    _r(
        "integration",
        r"\b(integrat\w+|webhook|third[- ]party|payment|email|sms|stripe|s3|upload)\b",
        2,
        "prompt",
        "integration in request",
    ),
)

CATEGORY_LABELS = {
    "authentication": "Authentication",
    "authorization": "Authorization",
    "database-schema": "Database schema",
    "api-endpoint": "API endpoint",
    "error-handling": "Error handling",
    "testing": "Testing",
    "validation": "Validation",
    "configuration": "Configuration",
    "ui-component": "UI component",
    "background-jobs": "Background jobs",
    "caching": "Caching",
    "logging": "Logging",
    "integration": "Integration",
    "general": "General",
}


def categorize(
    *, paths: list[str] | None = None, code: str = "", prompt: str = "", top: int = 3
) -> list[CategoryScore]:
    """Score categories. Code evidence per rule is capped so one noisy construct cannot dominate."""
    scores: dict[str, CategoryScore] = {}
    paths = paths or []
    for rule in RULES:
        hits = 0
        if rule.scope == "path":
            hits = sum(1 for p in paths if rule.pattern.search(p))
            hits = min(hits, 3)
        elif rule.scope == "code" and code:
            hits = min(len(rule.pattern.findall(code)), 4)
        elif rule.scope == "prompt" and prompt:
            hits = 1 if rule.pattern.search(prompt) else 0
        if hits:
            cs = scores.setdefault(rule.category, CategoryScore(rule.category, 0.0))
            cs.score += rule.weight * hits
            cs.evidence.append(f"{rule.label} ×{hits}")
    ranked = sorted(scores.values(), key=lambda c: -c.score)
    return [c for c in ranked if c.score >= 3][:top]


def primary_category(**kw: Any) -> str:
    ranked = categorize(**kw, top=1)
    return ranked[0].category if ranked else "general"
