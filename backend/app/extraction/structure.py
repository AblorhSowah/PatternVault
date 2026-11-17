"""Code structure extraction with tree-sitter (grammars are bundled in the wheels - works offline)."""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import tree_sitter as ts

from app.extraction.language import PARSEABLE

MAX_SOURCE_BYTES = 1_000_000
MAX_NODES = 300_000


@dataclass(frozen=True)
class Symbol:
    kind: str  # class | function | method | interface | enum | type | struct | trait | module
    name: str
    line: int
    end_line: int
    params: int = 0
    container: str | None = None
    decorators: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "name": self.name, "line": self.line, "end_line": self.end_line}
        if self.params:
            d["params"] = self.params
        if self.container:
            d["container"] = self.container
        if self.decorators:
            d["decorators"] = list(self.decorators)
        return d


@dataclass
class Structure:
    language: str
    parse_ok: bool = True
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    routes: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "symbols": [s.to_dict() for s in self.symbols],
            "imports": self.imports,
            "routes": self.routes,
        }


_GRAMMARS: dict[str, tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "php": ("tree_sitter_php", "language_php"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
}


@cache
def _parser(language: str) -> ts.Parser:
    try:
        module, factory = _GRAMMARS[language]
    except KeyError as exc:
        raise ValueError(f"no grammar for {language}") from exc
    return ts.Parser(ts.Language(getattr(importlib.import_module(module), factory)()))


# node type -> symbol kind, per language
_SYMBOL_NODES: dict[str, dict[str, str]] = {
    "python": {"class_definition": "class", "function_definition": "function"},
    "javascript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "generator_function_declaration": "function",
    },
    "typescript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "type_alias_declaration": "type",
        "abstract_class_declaration": "class",
    },
    "php": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
        "enum_declaration": "enum",
        "function_definition": "function",
        "method_declaration": "method",
    },
    "go": {"function_declaration": "function", "method_declaration": "method", "type_spec": "type"},
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "method_declaration": "method",
        "constructor_declaration": "method",
    },
    "ruby": {"class": "class", "module": "module", "method": "method", "singleton_method": "method"},
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
    },
    "c_sharp": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "record_declaration": "class",
    },
}
_SYMBOL_NODES["tsx"] = _SYMBOL_NODES["typescript"]

_IMPORT_NODES: dict[str, set[str]] = {
    "python": {"import_statement", "import_from_statement"},
    "javascript": {"import_statement"},
    "typescript": {"import_statement"},
    "php": {"namespace_use_declaration"},
    "go": {"import_spec"},
    "java": {"import_declaration"},
    "ruby": set(),
    "rust": {"use_declaration"},
    "c_sharp": {"using_directive"},
}
_IMPORT_NODES["tsx"] = _IMPORT_NODES["typescript"]

_CONTAINERS = {"class", "interface", "trait", "module", "impl", "struct", "enum"}
_PARAM_FIELDS = ("parameters", "formal_parameters", "parameter_list")

_ROUTE_RES = (
    # FastAPI / Flask / Express / Laravel / Spring style route registration
    re.compile(r"""@\s*\w+(?:\.\w+)*\.(get|post|put|delete|patch|route)\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\b(?:app|router|api|server)\.(get|post|put|delete|patch|use|all)\(\s*['"`]([^'"`]+)['"`]"""),
    re.compile(r"""Route::(get|post|put|delete|patch|any|resource|apiResource)\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""@(Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*)?\{?\s*['"]([^'"]*)['"]"""),
    re.compile(r"""\bpath\(\s*['"]([^'"]*)['"]\s*,\s*(\w[\w.]*)"""),
)


def _text(node: ts.Node | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text is not None else ""


def _count_params(node: ts.Node) -> int:
    for f in _PARAM_FIELDS:
        p = node.child_by_field_name(f)
        if p is not None:
            return sum(1 for c in p.named_children if c.type not in {"comment"})
    return 0


def _symbol_name(node: ts.Node, language: str) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(name)
    if node.type == "impl_item":
        return _text(node.child_by_field_name("type"))
    return ""


def extract_structure(language: str, source: str) -> Structure:
    """Parse ``source`` and list classes/functions/imports/routes. Never raises on bad input."""
    st = Structure(language=language)
    data = source.encode("utf-8", errors="replace")
    if language not in PARSEABLE:
        st.parse_ok = False
        st.error = "no grammar for language"
        return st
    if len(data) > MAX_SOURCE_BYTES:
        st.parse_ok = False
        st.error = "source too large"
        return st
    try:
        tree = _parser(language).parse(data)
    except Exception as exc:  # parser failures must not break capture
        st.parse_ok = False
        st.error = f"parse failed: {exc}"
        return st
    sym_nodes = _SYMBOL_NODES.get(language, {})
    imp_nodes = _IMPORT_NODES.get(language, set())
    stack: list[tuple[ts.Node, str | None]] = [(tree.root_node, None)]
    visited = 0
    while stack:
        node, container = stack.pop()
        visited += 1
        if visited > MAX_NODES:
            st.error = "node limit reached (partial)"
            break
        child_container = container
        kind = sym_nodes.get(node.type)
        if (
            kind
            and language in {"javascript", "typescript", "tsx"}
            and node.type == "method_definition"
            and container is None
        ):
            kind = None  # object-literal method, not a class member
        if kind:
            name = _symbol_name(node, language)
            if name:
                decos: tuple[str, ...] = ()
                if language == "python" and node.parent is not None and node.parent.type == "decorated_definition":
                    decos = tuple(
                        _text(c).strip().split("\n")[0][:80] for c in node.parent.children if c.type == "decorator"
                    )
                sym_kind = "method" if kind == "function" and container else kind
                st.symbols.append(
                    Symbol(
                        sym_kind,
                        name,
                        node.start_point[0] + 1,
                        node.end_point[0] + 1,
                        _count_params(node),
                        container,
                        decos,
                    )
                )
                if kind in _CONTAINERS:
                    child_container = name
        elif language in {"javascript", "typescript", "tsx"} and node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is not None and val.type in {"arrow_function", "function_expression", "function"}:
                nm = _text(node.child_by_field_name("name"))
                if nm:
                    st.symbols.append(
                        Symbol(
                            "function",
                            nm,
                            node.start_point[0] + 1,
                            node.end_point[0] + 1,
                            _count_params(val),
                            container,
                        )
                    )
        elif node.type in imp_nodes:
            st.imports.append(_text(node).strip().split("\n")[0][:200])
        elif language == "ruby" and node.type == "call":
            m = node.child_by_field_name("method")
            if _text(m) in {"require", "require_relative"}:
                st.imports.append(_text(node)[:200])
        for child in reversed(node.children):
            stack.append((child, child_container))
    st.symbols.sort(key=lambda s: (s.line, s.name))
    for rx in _ROUTE_RES:
        for rm in rx.finditer(source):
            st.routes.append({"method": rm.group(1).upper(), "path": rm.group(2)})
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for r in st.routes:
        key = (r["method"], r["path"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    st.routes = unique[:100]
    return st
