"""`tina-source-github`: Tina's GitHub Issues connector, as a connector process.

This is the reference connector. It wraps the in-process `GitHubSource` in
the protocol's server loop and adds the two things that were Tina's and are
now the connector's: the structured-inputs query builder (`repo` + `labels`)
and the existence check artifact verification needs for a github.com URL —
done here, with this process's credentials, which never leave it. Tina itself
talks to it over stdio like any connector — nothing in Tina imports this
module at run time; the console script is the whole surface.

The options table a track gives it::

    [smoke.options]
    repo = "acme/api"          # required
    labels = ["needs-triage"]  # optional; with no `query`, the connector builds one
"""

from __future__ import annotations

import re
import sys
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from tina import __version__
from tina import query as query_builders
from tina.connector.protocol import Lifecycle
from tina.connector.server import Connector, OptionsError, serve
from tina.models import WorkItem
from tina.sources.base import ClaimPrognosis, SourceError
from tina.sources.github import GitHubSource
from tina.verify import api_url

_GITHUB_WEB_HOSTS = frozenset({"github.com", "www.github.com"})

_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


class Options(BaseModel):
    """What `[<track>.options]` may say to this connector. Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    labels: list[str] = []


class GitHubConnector(Connector):
    name = "github"
    version = __version__

    def __init__(self, options: dict[str, Any], lifecycle: Lifecycle) -> None:
        super().__init__(options, lifecycle)
        try:
            parsed = Options.model_validate(options)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'options'}: {err['msg']}"
                for err in exc.errors()
            )
            raise OptionsError(
                problems, fix='Set repo = "owner/name"; labels is optional.'
            ) from None
        if not _REPO.fullmatch(parsed.repo):
            raise OptionsError(f'repo {parsed.repo!r} must be "owner/name"')
        if any('"' in label for label in parsed.labels):
            raise OptionsError("labels may not contain quotes")
        self.parsed = parsed
        self.source = GitHubSource(
            repo=parsed.repo,
            blocked_label=lifecycle.blocked_label,
            claim_policy=lifecycle.claim,
            claim_label=lifecycle.claim_label,
        )

    # -- the Source contract, delegated -----------------------------------------------

    def login(self) -> str:
        return self.source.login()

    def query(self, q: str) -> list[WorkItem]:
        return self.source.query(q)

    def get(self, item_id: str) -> WorkItem:
        return self.source.get(item_id)

    def matches(self, item_id: str, q: str) -> bool:
        return self.source.matches(item_id, q)

    def claim(self, item: WorkItem) -> bool:
        return self.source.claim(item)

    def claim_prognosis(self, item: WorkItem) -> ClaimPrognosis:
        return self.source.claim_prognosis(item)

    def claimed(self, q: str) -> list[WorkItem]:
        return self.source.claimed(q)

    def annotate(self, item: WorkItem, comment: str) -> None:
        self.source.annotate(item, comment)

    def block(self, item: WorkItem) -> None:
        self.source.block(item)

    # -- capabilities -----------------------------------------------------------------------

    def build_query(self) -> str:
        """`repo` and `labels` to an issue search that excludes the blocked and
        claim labels — the universal predicates for this tracker."""
        return query_builders.github_query(
            self.parsed.repo,
            self.parsed.labels,
            claim_label=self.lifecycle.claim_label if self.lifecycle.claim == "label" else None,
            blocked_label=self.lifecycle.blocked_label,
        )

    def verify_artifact(self, url: str) -> bool | None:
        """Whether a trusted GitHub artifact exists, checked here with this
        process's own credentials and 401 refresh semantics — never handed to
        Tina. None for a URL that is not on an exact trusted GitHub host."""
        api_path = self._api_path(url)
        if api_path is not None:
            return self._check_api(api_path)

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() != "https" or host not in _GITHUB_WEB_HOSTS or _port(parts) != 443:
            return None

        target = api_url(url)
        mapped_path = self._api_path(target)
        if mapped_path is not None:
            return self._check_api(mapped_path)
        return self._check_web(url)

    def _api_path(self, url: str) -> str | None:
        parts = urlsplit(url)
        api = urlsplit(self.source.api_base)
        if (
            parts.scheme.lower() != api.scheme.lower()
            or (parts.hostname or "").lower() != (api.hostname or "").lower()
            or _port(parts) != _port(api)
        ):
            return None
        prefix = api.path.rstrip("/")
        if prefix and parts.path != prefix and not parts.path.startswith(f"{prefix}/"):
            return None
        path = parts.path[len(prefix) :] or "/"
        if not path.startswith("/"):
            return None
        return f"{path}?{parts.query}" if parts.query else path

    def _check_api(self, path: str) -> bool:
        try:
            response = self.source._request("GET", path)
        except SourceError:
            return False
        return 200 <= response.status_code < 400

    def _check_web(self, url: str) -> bool:
        try:
            response = self.source.client.get(url)
            if response.status_code == 401 and self.source._token_command is not None:
                self.source.client.headers["Authorization"] = f"Bearer {self.source._fresh_token()}"
                response = self.source.client.get(url)
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 400


def _port(parts: SplitResult) -> int | None:
    try:
        if parts.port is not None:
            return parts.port
    except ValueError:
        return -1
    scheme = parts.scheme.lower()
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def main() -> None:
    sys.exit(serve(GitHubConnector))


if __name__ == "__main__":  # pragma: no cover
    main()
