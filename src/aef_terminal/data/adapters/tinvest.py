from __future__ import annotations

from aef_terminal.data.adapters._tinvest.provider import (
    TInvestDataProvider as _TInvestDataProvider,
)


class TInvestDataProvider(_TInvestDataProvider):
    """Public discovery boundary for the exact-UID T-Invest provider."""
