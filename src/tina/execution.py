"""Callable types for library injection at the worker's invocation boundary."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from tina.config import HarnessConfig
from tina.harness import HarnessResult
from tina.models import OutcomeReport


class Runner(Protocol):
    """Run the configured harness in the supplied temporary workspace."""

    def __call__(
        self,
        config: HarnessConfig,
        prompt: str,
        workdir: Path,
        /,
        *,
        model: str | None,
        env: dict[str, str] | None,
    ) -> HarnessResult: ...


type Verifier = Callable[[OutcomeReport], OutcomeReport]
type Capture = Callable[[Path | None, Path | None, str], None]
