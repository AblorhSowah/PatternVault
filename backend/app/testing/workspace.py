"""Target workspace detection: what kind of project is this and which tools can run in it."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TargetWorkspace:
    root: Path
    python: str
    package: dict[str, Any] = field(default_factory=dict)
    has_python_project: bool = False
    has_node_project: bool = False
    tests_dir: Path | None = None
    _mods: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def detect(cls, root: Path) -> TargetWorkspace:
        root = root.resolve()
        py = sys.executable
        for cand in (
            root / ".venv" / "bin" / "python",
            root / "venv" / "bin" / "python",
            root.parent / ".venv" / "bin" / "python",
        ):
            if cand.exists():
                py = str(cand)
                break
        pkg: dict[str, Any] = {}
        pj = root / "package.json"
        if pj.is_file():
            try:
                pkg = json.loads(pj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pkg = {}
        has_py = any(
            (root / n).exists()
            for n in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini")
        ) or any(root.glob("*.py"))
        tests = next((root / d for d in ("tests", "test", "spec") if (root / d).is_dir()), None)
        return cls(root, py, pkg, has_py, pj.is_file(), tests)

    def python_has(self, module: str) -> bool:
        if module not in self._mods:
            code = f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"
            try:
                self._mods[module] = (
                    subprocess.run([self.python, "-c", code], capture_output=True, timeout=20, check=False).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                self._mods[module] = False
        return self._mods[module]

    def node_dep(self, name: str) -> bool:
        return name in {**self.package.get("dependencies", {}), **self.package.get("devDependencies", {})}

    def node_ready(self) -> bool:
        return self.has_node_project and (self.root / "node_modules").is_dir()

    def script(self, name: str) -> str | None:
        return (self.package.get("scripts") or {}).get(name)

    def is_vscode_extension(self) -> tuple[Path, dict[str, Any]] | None:
        for base in (self.root, self.root / "vscode-extension"):
            pj = base / "package.json"
            if pj.is_file():
                try:
                    d = json.loads(pj.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (d.get("engines") or {}).get("vscode"):
                    return base, d
        return None

    def is_patternvault(self) -> bool:
        return (self.root / "backend" / "app" / "database" / "engine.py").is_file()
