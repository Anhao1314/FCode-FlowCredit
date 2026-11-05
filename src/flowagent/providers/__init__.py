"""Providers: proposal sources. Phase 1 ships only the offline deterministic mock."""

from __future__ import annotations

from .base import Proposal, Provider
from .mock import MockProvider

__all__ = ["MockProvider", "Proposal", "Provider"]
