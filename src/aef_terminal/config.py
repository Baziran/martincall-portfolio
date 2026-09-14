import json
import os
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from aef_terminal.data.instrument_identity import require_exact_instrument_id_sequence
from aef_terminal.runtime.ibkr_settings import (
    IbkrRuntimeSettingsSnapshot,
    ibkr_runtime_settings_snapshot,
)

_SECRET_FILE_CACHE: dict[str, str] | None = None


def _env_bool(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default)
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_INVALID")


def _exact_config_bool(name: str, value: str) -> bool:
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_INVALID")


def _env_instrument_id_tuple(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON array of exact instrument IDs") from exc
    try:
        return require_exact_instrument_id_sequence(payload, allow_empty=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a JSON array of exact instrument IDs: {exc}") from exc


def _secret_file_values() -> dict[str, str]:
    global _SECRET_FILE_CACHE
    if _SECRET_FILE_CACHE is not None:
        return _SECRET_FILE_CACHE
    values: dict[str, str] = {}
    path = os.getenv("AEF_SECRET_FILE")
    if path:
        try:
            for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            values = {}
    _SECRET_FILE_CACHE = values
    return values


def env_or_secret(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    value_file = os.getenv(f"{name}_FILE")
    if value_file:
        try:
            file_value = Path(value_file).read_text(encoding="utf-8").strip()
        except OSError:
            file_value = ""
        if file_value:
            return file_value
    return _secret_file_values().get(name, default)


def _resolve_telegram_enabled() -> bool:
    explicit = env_or_secret("AEF_TELEGRAM_ENABLED")
    if explicit is not None and str(explicit).strip() != "":
        return _exact_config_bool("AEF_TELEGRAM_ENABLED", str(explicit))
    return bool(env_or_secret("AEF_TELEGRAM_BOT_TOKEN") and env_or_secret("AEF_TELEGRAM_CHAT_ID"))


def _resolve_discord_signals_enabled() -> bool:
    explicit = env_or_secret("AEF_DISCORD_SIGNALS_ENABLED")
    if explicit is not None and str(explicit).strip() != "":
        return _exact_config_bool("AEF_DISCORD_SIGNALS_ENABLED", str(explicit))
    return bool(
        env_or_secret("AEF_DISCORD_SIGNALS_CHANNEL_IDS")
        and env_or_secret("AEF_DISCORD_SIGNALS_AUTHOR_ID")
        and env_or_secret("AEF_DISCORD_SIGNALS_BRIDGE_SECRET")
    )


@dataclass(frozen=True)
class AppConfig:
    _ibkr_runtime_snapshot: InitVar[IbkrRuntimeSettingsSnapshot | None] = None
    project_root: Path = Path(__file__).resolve().parents[2]
    data_root: Path = project_root.parent / "data"
    base_timeframe: str = "1m"
    decision_timeframe: str = "5m"
    timezone: str = "America/New_York"
    database_url: str | None = os.getenv("AEF_DATABASE_URL") or None
    tinvest_token: str | None = field(
        default_factory=lambda: env_or_secret("AEF_TINVEST_TOKEN") or None,
        repr=False,
    )
    ibkr_host: str = field(default_factory=lambda: os.getenv("AEF_IBKR_HOST", "127.0.0.1"))
    ibkr_gateway_login_control_host: str = field(
        default_factory=lambda: os.getenv("AEF_IBKR_GATEWAY_LOGIN_CONTROL_HOST", "")
    )
    ibkr_gateway_login_control_port: int = field(
        default_factory=lambda: int(os.getenv("AEF_IBKR_GATEWAY_LOGIN_CONTROL_PORT", "0"))
    )
    ibkr_gateway_login_control_timeout_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("AEF_IBKR_GATEWAY_LOGIN_CONTROL_TIMEOUT_SECONDS", "2.0")
        )
    )
    ibkr_gateway_login_control_cooldown_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("AEF_IBKR_GATEWAY_LOGIN_CONTROL_COOLDOWN_SECONDS", "30.0")
        )
    )
    ibkr_port: int = field(init=False)
    ibkr_client_id: int = field(default_factory=lambda: int(os.getenv("AEF_IBKR_CLIENT_ID", "17")))
    ibkr_quote_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_QUOTE_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 1000)
            )
        )
    )
    ibkr_chart_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_CHART_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 2000)
            )
        )
    )
    ibkr_history_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_HISTORY_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 2500)
            )
        )
    )
    ibkr_lookup_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_LOOKUP_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 7500)
            )
        )
    )
    ibkr_market_data_type: int = field(init=False)
    ibkr_gex_host: str = field(
        default_factory=lambda: (
            os.getenv("AEF_IBKR_GEX_HOST") or os.getenv("AEF_IBKR_HOST", "127.0.0.1")
        )
    )
    ibkr_gex_port: int = field(init=False)
    ibkr_gex_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_GEX_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 3000)
            )
        )
    )
    ibkr_gex_live_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_GEX_LIVE_CLIENT_ID",
                str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 3100),
            )
        )
    )
    ibkr_option_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_OPTION_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 3500)
            )
        )
    )
    option_target_theoretical_iv: float = field(
        default_factory=lambda: float(os.getenv("AEF_OPTION_TARGET_THEORETICAL_IV", "0.20"))
    )
    option_target_risk_free_rate: float = field(
        default_factory=lambda: float(os.getenv("AEF_OPTION_TARGET_RISK_FREE_RATE", "0.052"))
    )
    option_target_dividend_yield: float = field(
        default_factory=lambda: float(os.getenv("AEF_OPTION_TARGET_DIVIDEND_YIELD", "0.0"))
    )
    ibkr_gex_market_data_type: int = field(init=False)
    ibkr_gex_expiry_mode: str = field(
        default_factory=lambda: os.getenv("AEF_IBKR_GEX_EXPIRY_MODE", "hybrid")
    )
    ibkr_gex_batch_size: int = field(
        default_factory=lambda: int(os.getenv("AEF_IBKR_GEX_BATCH_SIZE", "8"))
    )
    ibkr_gex_wait_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_IBKR_GEX_WAIT_SECONDS", "2.0"))
    )
    ibkr_gex_batch_pause_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_IBKR_GEX_BATCH_PAUSE_SECONDS", "0.25"))
    )
    ibkr_gex_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_IBKR_GEX_TIMEOUT_SECONDS", "65.0"))
    )
    ibkr_gex_risk_free_rate: float = field(
        default_factory=lambda: float(os.getenv("AEF_IBKR_GEX_RISK_FREE_RATE", "0.052"))
    )
    ibkr_gex_dividend_yield: float = field(
        default_factory=lambda: float(os.getenv("AEF_IBKR_GEX_DIVIDEND_YIELD", "0.0135"))
    )
    ibkr_gex_dividend_yield_override_active: bool = field(
        default_factory=lambda: "AEF_IBKR_GEX_DIVIDEND_YIELD" in os.environ
    )
    gex_scheduler_enabled: bool = field(
        default_factory=lambda: _env_bool("AEF_GEX_SCHEDULER_ENABLED", "false")
    )
    gex_scheduler_instrument_ids: tuple[str, ...] = field(
        default_factory=lambda: _env_instrument_id_tuple("AEF_GEX_SCHEDULER_INSTRUMENT_IDS")
    )
    research_capture_enabled: bool = field(
        default_factory=lambda: _env_bool("AEF_RESEARCH_CAPTURE_ENABLED", "false")
    )
    research_capture_instrument_ids: tuple[str, ...] = field(
        default_factory=lambda: _env_instrument_id_tuple("AEF_RESEARCH_CAPTURE_INSTRUMENT_IDS")
    )
    research_capture_timeframe: str = field(
        default_factory=lambda: os.getenv("AEF_RESEARCH_CAPTURE_TIMEFRAME", "5m")
    )
    research_capture_reconcile_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_RESEARCH_CAPTURE_RECONCILE_SECONDS", "15"))
    )
    vix_instrument_id: str = field(default_factory=lambda: os.getenv("AEF_VIX_INSTRUMENT_ID", ""))
    ibkr_tick_client_id: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_IBKR_TICK_CLIENT_ID", str(int(os.getenv("AEF_IBKR_CLIENT_ID", "17")) + 4000)
            )
        )
    )
    ibkr_contract_cache_size: int = field(
        default_factory=lambda: int(os.getenv("AEF_IBKR_CONTRACT_CACHE_SIZE", "256"))
    )
    ibkr_readonly: bool = field(default_factory=lambda: _env_bool("AEF_IBKR_READONLY", "true"))
    quote_stream_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_QUOTE_STREAM_SECONDS", "0.3"))
    )
    quote_snapshot_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_QUOTE_SNAPSHOT_SECONDS", "5"))
    )
    quote_stream_cleanup_grace_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_QUOTE_STREAM_CLEANUP_GRACE_SECONDS", "1.0"))
    )
    ws_heartbeat_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_WS_HEARTBEAT_SECONDS", "15.0"))
    )
    host_sleep_check_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_HOST_SLEEP_CHECK_SECONDS", "15.0"))
    )
    host_sleep_gap_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_HOST_SLEEP_GAP_SECONDS", "90.0"))
    )
    chart_stream_recovery_tail: int = field(
        default_factory=lambda: int(os.getenv("AEF_CHART_STREAM_RECOVERY_TAIL", "120"))
    )
    chart_stream_max_recovery_tail: int = field(
        default_factory=lambda: int(os.getenv("AEF_CHART_STREAM_MAX_RECOVERY_TAIL", "500"))
    )
    chart_stream_poll_seconds: float = field(
        default_factory=lambda: float(os.getenv("AEF_CHART_STREAM_POLL_SECONDS", "3.0"))
    )
    closed_session_maintenance_enabled: bool = field(
        default_factory=lambda: _env_bool("AEF_CLOSED_SESSION_MAINTENANCE_ENABLED", "true")
    )
    closed_session_maintenance_timezone: str = field(
        default_factory=lambda: os.getenv(
            "AEF_CLOSED_SESSION_MAINTENANCE_TIMEZONE",
            "Asia/Jerusalem",
        )
    )
    closed_session_maintenance_start_local: str = field(
        default_factory=lambda: os.getenv(
            "AEF_CLOSED_SESSION_MAINTENANCE_START_LOCAL",
            "00:05",
        )
    )
    closed_session_maintenance_end_local: str = field(
        default_factory=lambda: os.getenv(
            "AEF_CLOSED_SESSION_MAINTENANCE_END_LOCAL",
            "01:00",
        )
    )
    closed_session_maintenance_active_poll_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("AEF_CLOSED_SESSION_MAINTENANCE_ACTIVE_POLL_SECONDS", "30")
        )
    )
    closed_session_maintenance_idle_poll_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("AEF_CLOSED_SESSION_MAINTENANCE_IDLE_POLL_SECONDS", "1800")
        )
    )
    closed_session_maintenance_repair_timeout_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("AEF_CLOSED_SESSION_MAINTENANCE_REPAIR_TIMEOUT_SECONDS", "30")
        )
    )
    closed_session_maintenance_max_provider_admissions: int = field(
        default_factory=lambda: int(
            os.getenv("AEF_CLOSED_SESSION_MAINTENANCE_MAX_PROVIDER_ADMISSIONS", "1")
        )
    )
    closed_session_maintenance_admission_window_seconds: float = field(
        default_factory=lambda: float(
            os.getenv(
                "AEF_CLOSED_SESSION_MAINTENANCE_ADMISSION_WINDOW_SECONDS",
                str(12 * 60 * 60),
            )
        )
    )
    closed_session_maintenance_max_provider_admissions_per_window: int = field(
        default_factory=lambda: int(
            os.getenv(
                "AEF_CLOSED_SESSION_MAINTENANCE_MAX_PROVIDER_ADMISSIONS_PER_WINDOW",
                "32",
            )
        )
    )
    closed_session_maintenance_failure_threshold: int = field(
        default_factory=lambda: int(
            os.getenv("AEF_CLOSED_SESSION_MAINTENANCE_FAILURE_THRESHOLD", "3")
        )
    )
    closed_session_maintenance_breaker_backoff_seconds: float = field(
        default_factory=lambda: float(
            os.getenv(
                "AEF_CLOSED_SESSION_MAINTENANCE_BREAKER_BACKOFF_SECONDS",
                str(6 * 60 * 60),
            )
        )
    )
    tick_retention_hours: int = int(os.getenv("AEF_TICK_RETENTION_HOURS", "6"))
    tick_chunk_minutes: int = int(os.getenv("AEF_TICK_CHUNK_MINUTES", "30"))
    telegram_enabled: bool = field(default_factory=lambda: _resolve_telegram_enabled())
    telegram_bot_token: str | None = env_or_secret("AEF_TELEGRAM_BOT_TOKEN") or None
    telegram_chat_id: str | None = env_or_secret("AEF_TELEGRAM_CHAT_ID") or None
    telegram_timeout_seconds: float = float(
        env_or_secret("AEF_TELEGRAM_TIMEOUT_SECONDS", "5") or "5"
    )
    telegram_interactive_enabled: bool = str(
        env_or_secret("AEF_TELEGRAM_INTERACTIVE_ENABLED", "false")
    ).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    telegram_interactive_poll_timeout_seconds: float = float(
        env_or_secret("AEF_TELEGRAM_INTERACTIVE_POLL_TIMEOUT_SECONDS", "25") or "25"
    )
    discord_signals_enabled: bool = field(
        default_factory=lambda: _resolve_discord_signals_enabled()
    )
    discord_signals_bridge_secret: str | None = (
        env_or_secret("AEF_DISCORD_SIGNALS_BRIDGE_SECRET") or None
    )
    discord_signals_channel_ids: str = env_or_secret("AEF_DISCORD_SIGNALS_CHANNEL_IDS") or ""
    discord_signals_author_id: str = env_or_secret("AEF_DISCORD_SIGNALS_AUTHOR_ID") or ""
    discord_signals_author_name: str = (
        env_or_secret("AEF_DISCORD_SIGNALS_AUTHOR_NAME", "GAA") or "GAA"
    )
    discord_signals_retention_hours: int = int(
        env_or_secret("AEF_DISCORD_SIGNALS_RETENTION_HOURS", "72") or "72"
    )
    discord_signals_history_limit: int = int(
        env_or_secret("AEF_DISCORD_SIGNALS_HISTORY_LIMIT", "500") or "500"
    )

    def __post_init__(self, _ibkr_runtime_snapshot: IbkrRuntimeSettingsSnapshot | None) -> None:
        runtime_snapshot = _ibkr_runtime_snapshot or ibkr_runtime_settings_snapshot()
        object.__setattr__(self, "ibkr_port", runtime_snapshot.port)
        object.__setattr__(self, "ibkr_gex_port", runtime_snapshot.gex_port)
        object.__setattr__(self, "ibkr_market_data_type", runtime_snapshot.market_data_type)
        object.__setattr__(
            self,
            "ibkr_gex_market_data_type",
            runtime_snapshot.gex_market_data_type,
        )
