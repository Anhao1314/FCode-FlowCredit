"""Finance domain: synthetic filings, evidence authority and read-only tools.

This package is the example domain, not the kernel. A different domain would
provide its own ``build_*_tools`` and its own authority policy; nothing in
``runtime`` imports this package.
"""

from __future__ import annotations

from .authority import evaluate_fact_authority
from .fixtures import FinanceWorld
from .tools import build_finance_tools

__all__ = ["FinanceWorld", "build_finance_tools", "evaluate_fact_authority"]
