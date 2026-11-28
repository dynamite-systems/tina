"""clean_env keeps every environment input tina reads out of the tests (#75).

The variable names are found by scanning `src/tina` rather than listed here, so
a new credential or path variable that is missing from clean_env's tuple fails
this file instead of leaking a developer's real value into the suite. Each one
is seeded with a synthetic value, then a child pytest session running the real
`tests/conftest.py` asserts it is gone.
"""

from __future__ import annotations

import ast
import importlib
import shlex
import sys
from pathlib import Path

import pytest

import tina

SRC = Path(tina.__file__).parent
CONFTEST = Path(__file__).parents[1] / "conftest.py"

#: Named in #75; seeded even if the scan below ever stops finding them.
REPORTED = ("GH_TOKEN", "GITHUB_TOKEN_COMMAND", "TINA_TRACKS_DIR", "TINA_ARTIFACTS_DIR")

#: Helpers that read the variable they are handed; their call sites are scanned.
INDIRECT = {("tina.credentials", "var"), ("tina.sources.base", "candidate")}

#: Calls whose every positional argument except these indexes names a variable.
ENV_READERS = {"require_env": {1}, "token_command": set()}


def _is_os_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_name_args(node: ast.AST) -> list[ast.expr]:
    """The arguments of `node` that name an environment variable it reads."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and _is_os_environ(func.value):
            return node.args[:1]
        if isinstance(func, ast.Attribute) and func.attr == "getenv":
            return node.args[:1]
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in ENV_READERS:
            return [a for i, a in enumerate(node.args) if i not in ENV_READERS[name]]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_os_environ(node.value)
    ):
        return [node.slice]
    return []


def _resolve(expr: ast.expr, module: object) -> str | None:
    """A string literal, or a module constant such as `credentials.GITHUB_TOKEN_COMMAND`."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        value = getattr(module, expr.id, None)
    elif isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        value = getattr(getattr(module, expr.value.id, None), expr.attr, None)
    else:
        return None
    return value if isinstance(value, str) else None


def environment_inputs() -> set[str]:
    """Every variable name `src/tina` reads from the environment."""
    names: set[str] = set()
    unresolved: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        dotted = ".".join(("tina", *path.relative_to(SRC).with_suffix("").parts))
        dotted = dotted.removesuffix(".__init__")
        module = importlib.import_module(dotted)
        for node in ast.walk(ast.parse(path.read_text())):
            for arg in _env_name_args(node):
                name = _resolve(arg, module)
                if name is None:
                    unresolved.add((dotted, ast.unparse(arg)))
                else:
                    names.add(name)
    assert unresolved == INDIRECT, "an environment read the scan cannot name"
    return names


def test_scan_finds_the_reported_inputs() -> None:
    assert set(REPORTED) <= environment_inputs()


def test_clean_env_removes_every_seeded_input(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = sorted(environment_inputs() | set(REPORTED))
    marker = pytester.path / "helper-ran"
    helper = [sys.executable, "-c", f"open({str(marker)!r}, 'w'); print('inherited-token')"]
    for name in names:
        monkeypatch.setenv(name, f"synthetic-{name.lower()}")
    # An inherited credential helper: proof it ran is the marker file.
    monkeypatch.setenv("GITHUB_TOKEN_COMMAND", shlex.join(helper))

    pytester.makeconftest(CONFTEST.read_text())
    pytester.makepyfile(
        test_probe=f"""
        import os
        from pathlib import Path

        import pytest

        from tina import credentials
        from tina.sources.base import SourceError
        from tina.sources.github import GitHubSource

        NAMES = {names!r}
        MARKER = Path({str(marker)!r})

        def test_seeded_inputs_are_removed():
            assert [n for n in NAMES if n in os.environ] == []

        def test_inherited_helper_is_not_run():
            assert credentials.token_command(credentials.GITHUB_TOKEN_COMMAND) is None
            with pytest.raises(SourceError, match="GITHUB_TOKEN"):
                GitHubSource(repo="acme/widgets")
            assert not MARKER.exists()

        def test_a_test_sets_its_own_fake_credential(monkeypatch):
            monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
            source = GitHubSource(repo="acme/widgets")
            assert source.client.headers["Authorization"] == "Bearer fake-token"
            assert not MARKER.exists()
        """
    )

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=3)
    assert not marker.exists()
