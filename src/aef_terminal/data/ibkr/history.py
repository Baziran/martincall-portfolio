from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.data.instrument_identity import (
    instrument_is_futures_root,
    require_provider_identity,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryRoute,
)
from aef_terminal.domain import Bar, BarProviderRequest
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason


IBKR_CONTINUOUS_HISTORY_ROUTE = CanonicalHistoryRoute("provider_native", "provider_managed")


def ibkr_history_runtime_contracts() -> tuple[type[Any], Any]:
    """Resolve the canonical history feed and manager without an adapter import cycle."""

    from aef_terminal.data.ibkr.bars import IbkrBarFeed
    from aef_terminal.data.ibkr.manager import ibkr_market_data_manager

    return IbkrBarFeed, ibkr_market_data_manager


def write_continuous_history(
    store: Any | None,
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
    revision_sequence: int | None = None,
) -> CanonicalBarCommitReceipt:
    from aef_terminal.data.providers import route_instrument

    if store is None:
        raise RuntimeError("Missing IBKR provider-native futures canonical store")
    if not bars:
        empty_sequence = (
            int(revision_sequence)
            if revision_sequence is not None
            else int(store.reserve_bar_revision_sequence())
        )
        return CanonicalBarCommitReceipt(revision_sequence=empty_sequence)
    qualified = require_provider_identity(instrument, provider="ibkr")
    if not instrument_is_futures_root(qualified):
        raise ValueError("IBKR_CANONICAL_HISTORY_REQUIRES_FUTURES_ROOT")
    route = route_instrument(qualified, expected_source="ibkr")
    # IBKR may expose the same conId for CONTFUT and its front FUT. The exact
    # provider contract type, not the id alone, proves continuous-series lineage.
    provenances = [bar.provenance for bar in bars]
    if any(provenance is None for provenance in provenances):
        raise ValueError("IBKR_CONTINUOUS_HISTORY_PROVENANCE_REQUIRED")
    provider_contract_ids = {
        provenance.provider_contract_id for provenance in provenances if provenance is not None
    }
    provider_contract_types = {
        provenance.provider_contract_type for provenance in provenances if provenance is not None
    }
    request_types = {
        provenance.request_type for provenance in provenances if provenance is not None
    }
    data_types = {provenance.data_type for provenance in provenances if provenance is not None}
    if len(provider_contract_ids) != 1:
        raise ValueError("IBKR_CONTINUOUS_HISTORY_CONTRACT_MIXED")
    if provider_contract_types != {"CONTFUT"}:
        raise ValueError("IBKR_CONTINUOUS_HISTORY_CONTRACT_TYPE_MISMATCH")
    if not request_types.issubset(
        {
            BarProviderRequest.HISTORICAL,
            BarProviderRequest.KEEP_UP_TO_DATE,
        }
    ):
        raise ValueError("IBKR_CONTINUOUS_HISTORY_REQUEST_TYPE_MISMATCH")
    if data_types != {route.adapter.bar_data_type(route.instrument)}:
        raise ValueError("IBKR_CONTINUOUS_HISTORY_DATA_TYPE_MISMATCH")
    provider_contract_id = next(iter(provider_contract_ids))
    provider_contract_type = next(iter(provider_contract_types))
    data_type = next(iter(data_types))
    allowed_request_types = frozenset(
        {
            BarProviderRequest.HISTORICAL,
            BarProviderRequest.KEEP_UP_TO_DATE,
        }
    )
    for bar in bars:
        reject_reason = authoritative_bar_reject_reason(
            route.provider,
            bar,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            allowed_request_types=allowed_request_types,
            provider_contract_id=provider_contract_id,
            provider_contract_type=provider_contract_type,
            data_type=data_type,
        )
        if reject_reason is not None:
            raise ValueError(f"IBKR_CONTINUOUS_HISTORY_BAR_REJECTED_{reject_reason.upper()}")
    return store.write_futures_canonical_bars(
        bars,
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        series_type=IBKR_CONTINUOUS_HISTORY_ROUTE.series_type,
        roll_policy=IBKR_CONTINUOUS_HISTORY_ROUTE.roll_policy,
        metadata={
            "identity_scope": "future_root",
            "provider_contract_type": provider_contract_type,
            "provider_contract_id": provider_contract_id,
            "data_type": data_type,
            "series_authority": "provider",
        },
        revision_sequence=revision_sequence,
    )
