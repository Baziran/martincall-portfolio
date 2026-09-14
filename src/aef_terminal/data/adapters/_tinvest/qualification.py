from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
    require_provider_identity,
)


_ASSET_CLASS_BY_KIND = {
    "INSTRUMENT_TYPE_BOND": "bond",
    "INSTRUMENT_TYPE_SHARE": "stock",
    "INSTRUMENT_TYPE_CURRENCY": "forex",
    "INSTRUMENT_TYPE_ETF": "etf",
    "INSTRUMENT_TYPE_FUTURES": "future",
    "INSTRUMENT_TYPE_SP": "structured_product",
    "INSTRUMENT_TYPE_OPTION": "option",
    "INSTRUMENT_TYPE_CLEARING_CERTIFICATE": "clearing_certificate",
    "INSTRUMENT_TYPE_INDEX": "index",
    "INSTRUMENT_TYPE_COMMODITY": "commodity",
    "INSTRUMENT_TYPE_DFA": "dfa",
}


class TInvestQualificationError(ValueError):
    """Raised when T-Invest cannot prove one exact instrument identity."""


class TInvestSyncInstrumentsService(Protocol):
    def find_instrument(self, request: object) -> object: ...

    def get_instrument_by(self, request: object) -> object: ...


class TInvestSyncServices(Protocol):
    instruments: TInvestSyncInstrumentsService


@dataclass(frozen=True, slots=True)
class TInvestInstrumentCandidate:
    instrument_uid: str
    position_uid: str
    figi: str
    ticker: str
    class_code: str
    instrument_type: str
    instrument_kind: str
    asset_class: str
    name: str
    api_trade_available: bool
    for_qualified_investor: bool
    weekend_trading_available: bool
    lot: int | None
    first_1m_candle_at: datetime | None
    first_1d_candle_at: datetime | None
    provider: Literal["tinvest"] = field(default="tinvest", init=False)

    @property
    def provider_contract_id(self) -> str:
        return self.instrument_uid


@dataclass(frozen=True, slots=True)
class TInvestInstrumentBinding:
    candidate: TInvestInstrumentCandidate
    provider: Literal["tinvest"] = field(default="tinvest", init=False)
    identity_scope: Literal["contract"] = field(default="contract", init=False)

    @property
    def instrument_uid(self) -> str:
        return self.candidate.instrument_uid

    @property
    def provider_contract_id(self) -> str:
        return self.candidate.instrument_uid

    @property
    def asset_class(self) -> str:
        return self.candidate.asset_class


def _required_text(item: object, field_name: str) -> str:
    value = getattr(item, field_name, None)
    try:
        return require_exact_identity_text(value, field=field_name)
    except (TypeError, ValueError) as exc:
        raise TInvestQualificationError(f"TINVEST_INSTRUMENT_FIELD_INVALID:{field_name}") from exc


def _optional_text(item: object, field_name: str) -> str:
    value = getattr(item, field_name, "")
    if not isinstance(value, str):
        raise TInvestQualificationError(f"TINVEST_INSTRUMENT_FIELD_INVALID:{field_name}")
    return value


def _required_bool(item: object, field_name: str) -> bool:
    value = getattr(item, field_name, None)
    if type(value) is not bool:
        raise TInvestQualificationError(f"TINVEST_INSTRUMENT_FIELD_INVALID:{field_name}")
    return value


def _optional_lot(item: object) -> int | None:
    value = getattr(item, "lot", None)
    if isinstance(value, bool):
        raise TInvestQualificationError("TINVEST_INSTRUMENT_FIELD_INVALID:lot")
    if value in (None, 0):
        return None
    if not isinstance(value, int) or value < 0:
        raise TInvestQualificationError("TINVEST_INSTRUMENT_FIELD_INVALID:lot")
    return value


def _optional_aware_datetime(item: object, field_name: str) -> datetime | None:
    value = getattr(item, field_name, None)
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TInvestQualificationError(f"TINVEST_INSTRUMENT_FIELD_INVALID:{field_name}")
    return value


def _typed_instrument_kind(item: object) -> tuple[str, str]:
    value = getattr(item, "instrument_kind", None)
    kind = getattr(value, "name", None)
    if not isinstance(kind, str) or kind not in _ASSET_CLASS_BY_KIND:
        raise TInvestQualificationError("TINVEST_INSTRUMENT_KIND_UNSUPPORTED")
    return kind, _ASSET_CLASS_BY_KIND[kind]


def qualify_tinvest_instrument(item: object) -> TInvestInstrumentCandidate:
    """Admit exact typed identity fields from one provider response item."""

    kind, asset_class = _typed_instrument_kind(item)
    return TInvestInstrumentCandidate(
        instrument_uid=_required_text(item, "uid"),
        position_uid=_optional_text(item, "position_uid"),
        figi=_optional_text(item, "figi"),
        ticker=_required_text(item, "ticker"),
        class_code=_required_text(item, "class_code"),
        instrument_type=_required_text(item, "instrument_type"),
        instrument_kind=kind,
        asset_class=asset_class,
        name=_required_text(item, "name"),
        api_trade_available=_required_bool(item, "api_trade_available_flag"),
        for_qualified_investor=_required_bool(item, "for_qual_investor_flag"),
        weekend_trading_available=_required_bool(item, "weekend_flag"),
        lot=_optional_lot(item),
        first_1m_candle_at=_optional_aware_datetime(item, "first_1min_candle_date"),
        first_1d_candle_at=_optional_aware_datetime(item, "first_1day_candle_date"),
    )


def qualify_tinvest_search_items(
    items: Iterable[object],
    *,
    max_results: int = 50,
) -> tuple[TInvestInstrumentCandidate, ...]:
    candidates = _qualified_tinvest_search_items(items)
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
        raise TInvestQualificationError("TINVEST_SEARCH_LIMIT_INVALID")
    return candidates[:max_results]


def _qualified_tinvest_search_items(
    items: Iterable[object],
) -> tuple[TInvestInstrumentCandidate, ...]:
    candidates: list[TInvestInstrumentCandidate] = []
    by_uid: dict[str, TInvestInstrumentCandidate] = {}
    for item in items:
        candidate = qualify_tinvest_instrument(item)
        prior = by_uid.get(candidate.instrument_uid)
        if prior is not None:
            if prior != candidate:
                raise TInvestQualificationError("TINVEST_DUPLICATE_UID_CONFLICT")
            continue
        by_uid[candidate.instrument_uid] = candidate
        candidates.append(candidate)
    return tuple(candidates)


def _ranked_tinvest_search_items(
    items: Iterable[object],
    *,
    normalized_query: str,
    max_results: int,
) -> tuple[TInvestInstrumentCandidate, ...]:
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
        raise TInvestQualificationError("TINVEST_SEARCH_LIMIT_INVALID")
    query_key = normalized_query.casefold()
    candidates = _qualified_tinvest_search_items(items)

    def rank(candidate: TInvestInstrumentCandidate) -> int:
        exact_fields = (
            candidate.ticker,
            candidate.instrument_uid,
            candidate.figi,
        )
        return 0 if any(value.casefold() == query_key for value in exact_fields) else 1

    return tuple(sorted(candidates, key=rank)[:max_results])


def tinvest_candidate_payload(
    candidate: TInvestInstrumentCandidate,
) -> dict[str, Any]:
    """Build one canonical exact-contract payload from a qualified provider fact."""

    if not isinstance(candidate, TInvestInstrumentCandidate):
        raise TypeError("TINVEST_CANDIDATE_REQUIRED")
    uid = require_exact_identity_text(
        candidate.instrument_uid,
        field="instrument_uid",
    )
    identity: dict[str, Any] = {
        "provider": "tinvest",
        "asset_class": candidate.asset_class,
        "identity_scope": "contract",
        "provider_contract_id": uid,
        "instrument_uid": uid,
        "position_uid": candidate.position_uid,
        "figi": candidate.figi,
        "ticker": candidate.ticker,
        "class_code": candidate.class_code,
        "instrument_type": candidate.instrument_type,
        "instrument_kind": candidate.instrument_kind,
        "api_trade_available": candidate.api_trade_available,
        "for_qualified_investor": candidate.for_qualified_investor,
        "weekend_trading_available": candidate.weekend_trading_available,
        "lot": candidate.lot,
        "first_1m_candle_at": _json_datetime(candidate.first_1m_candle_at),
        "first_1d_candle_at": _json_datetime(candidate.first_1d_candle_at),
    }
    payload: dict[str, Any] = {
        "instrument_id": f"tinvest|contract|{uid}",
        "key": candidate.ticker,
        "instrument_key": candidate.ticker,
        "display": candidate.ticker,
        "name": candidate.name,
        "provider": "tinvest",
        "provider_symbol": uid,
        "provider_contract_id": uid,
        "asset_class": candidate.asset_class,
        "contract_identity": identity,
    }
    return dict(require_provider_identity(payload, provider="tinvest"))


def tinvest_binding_payload(
    binding: TInvestInstrumentBinding,
) -> dict[str, Any]:
    if not isinstance(binding, TInvestInstrumentBinding):
        raise TypeError("TINVEST_BINDING_REQUIRED")
    return tinvest_candidate_payload(binding.candidate)


def _find_request(query: str) -> object:
    from t_tech.invest.grpc import FindInstrumentRequest

    return FindInstrumentRequest(query=query)


def tinvest_uid_request(instrument_uid: str) -> object:
    from t_tech.invest.grpc import InstrumentIdType, InstrumentRequest

    return InstrumentRequest(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
        id=instrument_uid,
    )


def _sdk_async_client() -> Any:
    from t_tech.invest.grpc import AsyncClient

    return AsyncClient


def _sdk_client() -> Any:
    from t_tech.invest.grpc import Client

    return Client


def _async_client(token: str) -> Any:
    return _sdk_async_client()(
        token=token,
        instrument_methods_to_cache=(),
        warmup_cache_methods=(),
    )


def _sync_client(token: str) -> Any:
    return _sdk_client()(
        token=token,
        instrument_methods_to_cache=(),
        warmup_cache_methods=(),
    )


def _search_items(response: object) -> Sequence[object]:
    items = getattr(response, "instruments", None)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise TInvestQualificationError("TINVEST_SEARCH_RESPONSE_INVALID")
    return items


def _normalized_query(query: object) -> str:
    if not isinstance(query, str):
        raise TInvestQualificationError("TINVEST_SEARCH_QUERY_INVALID")
    normalized_query = query.strip()
    if not normalized_query:
        raise TInvestQualificationError("TINVEST_SEARCH_QUERY_INVALID")
    return normalized_query


def _exact_uid(instrument_uid: object) -> str:
    try:
        return require_exact_identity_text(instrument_uid, field="instrument_uid")
    except (TypeError, ValueError) as exc:
        raise TInvestQualificationError("TINVEST_INSTRUMENT_UID_INVALID") from exc


def _binding_from_response(
    response: object,
    *,
    exact_uid: str,
) -> TInvestInstrumentBinding:
    item = getattr(response, "instrument", None)
    if item is None:
        raise TInvestQualificationError("TINVEST_BIND_RESPONSE_INVALID")
    candidate = qualify_tinvest_instrument(item)
    if candidate.instrument_uid != exact_uid:
        raise TInvestQualificationError("TINVEST_BIND_UID_MISMATCH")
    return TInvestInstrumentBinding(candidate=candidate)


def _search_tinvest_instruments_sync(
    services: TInvestSyncServices,
    query: object,
    *,
    max_results: int = 50,
) -> tuple[TInvestInstrumentCandidate, ...]:
    normalized_query = _normalized_query(query)
    response = services.instruments.find_instrument(_find_request(normalized_query))
    return _ranked_tinvest_search_items(
        _search_items(response),
        normalized_query=normalized_query,
        max_results=max_results,
    )


def _bind_tinvest_instrument_sync(
    services: TInvestSyncServices,
    instrument_uid: object,
) -> TInvestInstrumentBinding:
    exact_uid = _exact_uid(instrument_uid)
    response = services.instruments.get_instrument_by(tinvest_uid_request(exact_uid))
    return _binding_from_response(response, exact_uid=exact_uid)


def search_tinvest_payloads_sync(
    token: object,
    query: object,
    *,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    with _open_tinvest_sync_services(token) as services:
        return [
            tinvest_candidate_payload(candidate)
            for candidate in _search_tinvest_instruments_sync(
                services,
                query,
                max_results=max_results,
            )
        ]


def bind_tinvest_payload_sync(
    token: object,
    instrument_uid: object,
) -> dict[str, Any]:
    with _open_tinvest_sync_services(token) as services:
        return tinvest_binding_payload(_bind_tinvest_instrument_sync(services, instrument_uid))


@contextmanager
def _open_tinvest_sync_services(token: object) -> Iterator[TInvestSyncServices]:
    """Open official blocking services for worker-thread reference operations."""

    exact_token = _exact_token(token)
    with _sync_client(exact_token) as services:
        yield services


@asynccontextmanager
async def open_tinvest_services(token: object) -> AsyncIterator[Any]:
    """Open the official raw async services with SDK-owned caches disabled."""

    exact_token = _exact_token(token)
    async with _async_client(exact_token) as services:
        yield services


def _exact_token(token: object) -> str:
    try:
        return require_exact_identity_text(token, field="tinvest_token")
    except (TypeError, ValueError) as exc:
        raise TInvestQualificationError("TINVEST_TOKEN_REQUIRED") from exc


def _json_datetime(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None
