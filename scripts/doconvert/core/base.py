"""Interface for each conversion subcommand."""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod

from .context import Context


class ConvertFunction(ABC):
    """One CLI subcommand that converts documents."""

    name: str
    help: str

    @abstractmethod
    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Register positional args and flags for this function."""

    @abstractmethod
    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Execute conversion. Return process exit code."""
