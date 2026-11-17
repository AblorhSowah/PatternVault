"""Test plan: extraction/structure.py, analyzer.py, categorizer.py
- tree-sitter structure for all 10 bundled grammars (offline, no network)
- malformed / empty / huge input never raises
- manifest parsing + framework/version detection across ecosystems
- convention detection (naming, indentation)
- categorizer returns explainable evidence and ignores noise
"""

from pathlib import Path

import pytest

from app.extraction.analyzer import analyze_project, clean_version, detect_frameworks, parse_manifest
from app.extraction.categorizer import categorize, primary_category
from app.extraction.language import detect_language, file_role
from app.extraction.structure import MAX_SOURCE_BYTES, extract_structure

pytestmark = pytest.mark.unit

SAMPLES = {
    "python": (
        "import os\nfrom x import y\n\nclass A(B):\n    def m(self, a, b):\n        pass\n\ndef f(x):\n    pass\n",
        {"A", "m", "f"},
    ),
    "javascript": (
        "import a from 'a';\nclass A { m(x) { return 1 } }\nfunction f() {}\nconst g = (a) => a;\n",
        {"A", "m", "f", "g"},
    ),
    "typescript": (
        "import a from 'a';\ninterface I { x: number }\nclass A { m(x: number) { return 1 } }\nenum E { A }\ntype T = string;\nfunction f(): void {}\n",
        {"I", "A", "m", "E", "T", "f"},
    ),
    "tsx": ("import React from 'react';\nexport function App() { return <div/>; }\n", {"App"}),
    "php": (
        "<?php\nnamespace App;\nuse Foo\\Bar;\ninterface I {}\ntrait T {}\nclass C { public function m($a) {} }\nfunction f() {}\n",
        {"I", "T", "C", "m", "f"},
    ),
    "go": ('package main\nimport "fmt"\ntype S struct{}\nfunc (s S) M() {}\nfunc F() {}\n', {"S", "M", "F"}),
    "java": ("import java.util.List;\npublic class A { public void m(int a) {} }\ninterface I {}\n", {"A", "m", "I"}),
    "ruby": ("require 'json'\nmodule M\n class C\n  def m(a); end\n end\nend\n", {"M", "C", "m"}),
    "rust": (
        "use std::fmt;\nstruct S;\nenum E {A}\ntrait T {}\nfn f() {}\nimpl S { fn m(&self) {} }\n",
        {"S", "E", "T", "f", "m"},
    ),
    "c_sharp": ("using System;\nclass A { void M(int a) {} }\ninterface I {}\n", {"A", "M", "I"}),
}


@pytest.mark.parametrize("lang", list(SAMPLES))
def test_structure_per_language(lang):
    src, expected = SAMPLES[lang]
    st = extract_structure(lang, src)
    assert st.parse_ok
    names = {s.name for s in st.symbols}
    assert expected <= names, f"{lang}: missing {expected - names}"
    if lang not in ("ruby",):
        assert st.imports, f"{lang}: no imports found"


def test_python_methods_have_container_and_params_and_decorators():
    st = extract_structure("python", "class A:\n    @staticmethod\n    def m(a, b, c):\n        pass\n")
    m = next(s for s in st.symbols if s.name == "m")
    assert (m.kind, m.container, m.params) == ("method", "A", 3) and m.decorators


def test_routes_detected_and_deduplicated():
    st = extract_structure("python", "@router.get('/users')\ndef a(): pass\n@router.post('/users')\ndef b(): pass\n")
    assert st.routes == [{"method": "GET", "path": "/users"}, {"method": "POST", "path": "/users"}]
    assert extract_structure("php", "<?php\nRoute::post('/login', [A::class,'x']);").routes == [
        {"method": "POST", "path": "/login"}
    ]
    assert {"method": "GET", "path": "/p"} in extract_structure("javascript", "app.get('/p', h)").routes


def test_structure_never_raises():
    assert extract_structure("python", "def (((:::").parse_ok in (True, False)
    assert extract_structure("python", "").symbols == []
    assert extract_structure("cobol", "x").parse_ok is False
    big = extract_structure("python", "x = 1\n" * (MAX_SOURCE_BYTES // 6 + 10))
    assert big.parse_ok is False and "too large" in (big.error or "")


def test_language_and_role_detection():
    assert (
        detect_language("a/b.blade.php") == "php"
        and detect_language("x.TSX") == "tsx"
        and detect_language("README") is None
    )
    assert (
        file_role("tests/test_a.py") == "test" and file_role("app/Http/Controllers/AuthController.php") == "controller"
    )
    assert file_role("database/migrations/2024_01_01_000000_create_users.php") == "migration"
    assert file_role("src/lib/thing.py") == "util" and file_role("foo.py") == "source"


def test_manifests_parse():
    py = parse_manifest(
        "pyproject.toml",
        '[project]\ndependencies=["fastapi>=0.110","sqlalchemy[asyncio]==2.0.1"]\n[project.optional-dependencies]\ndev=["pytest"]',
    )
    assert {(d.name, d.is_dev) for d in py} == {("fastapi", False), ("sqlalchemy", False), ("pytest", True)}
    assert [
        (d.name, d.version_spec) for d in parse_manifest("requirements.txt", "Django==4.2.1  # web\n# c\n-r base.txt\n")
    ] == [("django", "==4.2.1")]
    npm = parse_manifest("package.json", '{"dependencies":{"react":"^18.2.0"},"devDependencies":{"vitest":"^1.0.0"}}')
    assert {(d.name, d.is_dev) for d in npm} == {("react", False), ("vitest", True)}
    comp = parse_manifest("composer.json", '{"require":{"php":"^8.2","ext-json":"*","laravel/framework":"^11.0"}}')
    assert [d.name for d in comp] == ["laravel/framework"]
    assert {
        (d.name, d.version_spec)
        for d in parse_manifest("Cargo.toml", '[dependencies]\ntokio="1"\naxum={version="0.7"}')
    } == {("tokio", "1"), ("axum", "0.7")}
    assert parse_manifest("package.json", "{not json") == []
    assert parse_manifest("go.mod", "module x\nrequire github.com/gin-gonic/gin v1.9.1\n")[0].version_spec == "v1.9.1"


def test_framework_detection_and_versions():
    deps = parse_manifest(
        "composer.json",
        '{"require":{"laravel/framework":"^11.2","laravel/sanctum":"^4.0"},"require-dev":{"phpunit/phpunit":"^11"}}',
    )
    fw = {f["name"]: f for f in detect_frameworks(deps)}
    assert fw["Laravel"]["version"] == "11.2" and fw["PHPUnit"]["dev"] is True
    assert clean_version("~=2.0.1,<3") == "2.0.1" and clean_version("*") == ""


def test_analyze_project_conventions(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["fastapi==0.110.0"]')
    (tmp_path / "svc.py").write_text(
        "def get_user_name():\n    return 'a'\n\ndef set_user_name(v):\n    return 'b'\n\nclass UserService:\n    pass\n"
    )
    (tmp_path / ".env").write_text("SECRET=1")
    a = analyze_project(tmp_path)
    assert a.primary_language == "python"
    assert a.frameworks[0]["name"] == "FastAPI" and a.frameworks[0]["version"] == "0.110.0"
    assert a.conventions["function_naming"] == "snake_case" and a.conventions["class_naming"] == "PascalCase"
    assert a.conventions["indentation"] == "4 spaces"
    assert a.file_count == 2  # .env excluded


def test_categorizer_evidence_and_thresholds():
    cats = categorize(
        paths=["app/auth/login.py"], code="def login(): jwt.encode(); bcrypt.hashpw()", prompt="add login with JWT"
    )
    assert cats[0].category == "authentication" and cats[0].evidence
    assert primary_category(paths=["a.py"], code="x = 1", prompt="") == "general"
    assert primary_category(paths=["tests/test_x.py"], code="def test_a(): assert 1") == "testing"
    assert (
        primary_category(paths=["database/migrations/0001_create.py"], code="op.create_table('t')") == "database-schema"
    )
