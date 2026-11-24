"""Unit tests for `tina.connectors.github` artifact verification and token semantics."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tina.connector.protocol import Lifecycle
from tina.connectors.github import GitHubConnector


def _token_command(monkeypatch: pytest.MonkeyPatch, tokens: list[str], tmp_path: Path) -> None:
    counter = tmp_path / "n"
    script = tmp_path / "mint.py"
    script.write_text(
        "import pathlib\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "n = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(n + 1))\n"
        f"print({tokens!r}[n])\n"
    )
    monkeypatch.setenv("GITHUB_TOKEN_COMMAND", f"{sys.executable} {script}")


def _attach_mock(
    connector: GitHubConnector, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    connector.source.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=connector.source.client.headers,
    )


def test_verify_artifact_preserves_minted_token_over_stale_env_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale GITHUB_TOKEN in the environment must not overwrite the minted client token."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_stale_env")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _token_command(monkeypatch, ["ghs_minted"], tmp_path)

    connector = GitHubConnector({"repo": "acme/api"}, Lifecycle())
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Authorization", "")))
        return httpx.Response(200, json={})

    _attach_mock(connector, handler)

    assert connector.verify_artifact("https://github.com/acme/api/pull/7") is True
    assert seen == [("https://api.github.com/repos/acme/api/issues/7", "Bearer ghs_minted")]


def test_verify_artifact_refreshes_expired_token_on_401_after_long_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 401 after a long agent run re-mints via GITHUB_TOKEN_COMMAND and retries once."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _token_command(monkeypatch, ["ghs_expired", "ghs_fresh"], tmp_path)

    connector = GitHubConnector({"repo": "acme/api"}, Lifecycle())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        seen.append(auth)
        if auth == "Bearer ghs_fresh":
            return httpx.Response(200, json={})
        return httpx.Response(401, json={"message": "Bad credentials"})

    _attach_mock(connector, handler)

    assert connector.verify_artifact("https://github.com/acme/api/pull/7") is True
    assert seen == ["Bearer ghs_expired", "Bearer ghs_fresh"]
    assert connector.source.client.headers["Authorization"] == "Bearer ghs_fresh"


def test_verify_artifact_returns_false_after_second_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second 401 after refresh is a real failure and returns False."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _token_command(monkeypatch, ["ghs_expired", "ghs_still_bad"], tmp_path)

    connector = GitHubConnector({"repo": "acme/api"}, Lifecycle())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={"message": "Bad credentials"})

    _attach_mock(connector, handler)

    assert connector.verify_artifact("https://github.com/acme/api/pull/7") is False
    assert seen == ["Bearer ghs_expired", "Bearer ghs_still_bad"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.github.com/acme/api/pull/7",
        "https://sub.api.github.com/repos/acme/api/issues/7",
        "https://github.com.evil.test/acme/api/pull/7",
        "http://github.com/acme/api/pull/7",
        "http://api.github.com/repos/acme/api/issues/7",
        "https://github.com:8443/acme/api/pull/7",
        "https://example.test/elsewhere",
    ],
)
def test_verify_artifact_does_not_send_tokens_to_untrusted_hosts_or_schemes(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """Only exact trusted HTTPS GitHub hosts or the configured API base receive credentials."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.delenv("GITHUB_TOKEN_COMMAND", raising=False)

    connector = GitHubConnector({"repo": "acme/api"}, Lifecycle())
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200)

    _attach_mock(connector, handler)

    assert connector.verify_artifact(url) is None
    assert sent == []


def test_verify_artifact_routes_direct_api_url_and_unrecognized_web_path_with_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Direct API URLs and unrecognized github.com paths keep minted auth and 401 refresh."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_stale_env")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _token_command(monkeypatch, ["ghs_one", "ghs_two", "ghs_three"], tmp_path)

    connector = GitHubConnector({"repo": "acme/api"}, Lifecycle())
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        seen.append((str(request.url), auth))
        if auth in {"Bearer ghs_two", "Bearer ghs_three"} and (
            request.url.host == "api.github.com" or auth == "Bearer ghs_three"
        ):
            return httpx.Response(200, json={})
        return httpx.Response(401)

    _attach_mock(connector, handler)

    assert connector.verify_artifact("https://api.github.com/repos/acme/api/issues/7") is True
    assert connector.verify_artifact("https://github.com/acme/api/blob/main/README.md") is True
    assert seen == [
        ("https://api.github.com/repos/acme/api/issues/7", "Bearer ghs_one"),
        ("https://api.github.com/repos/acme/api/issues/7", "Bearer ghs_two"),
        ("https://github.com/acme/api/blob/main/README.md", "Bearer ghs_two"),
        ("https://github.com/acme/api/blob/main/README.md", "Bearer ghs_three"),
    ]
