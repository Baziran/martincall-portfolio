    const AUTO_REFRESH_MS = 10000;
    const QUOTE_REFRESH_MS = 1000;
    const CHART_STREAM_MARKET_REFRESH_MS = 120000;
    const SCREENER_REFRESH_MS = 5000;
    const SCREENER_STREAM_REFRESH_MS = 20000;
    const SHARED_OBJECT_SYNC_MS = 30000;
    const QUOTE_STREAM_RECONNECT_MS = 1500;
    const QUOTE_STREAM_STALE_MS = 6000;
    const CHART_STREAM_STALE_MS = 20000;
    const SHARED_STREAM_OWNER_TTL_MS = 8000;
    const PASSIVE_RENDER_MS = 2000;
    const IDLE_CHART_RENDER_MS = 5000;
    const CLOSED_SESSION_CHART_RENDER_MS = 15000;
    const LIVE_CANDLE_REPAIR_MS = 5000;
    const LIVE_QUOTE_DISPLAY_TTL_MS = QUOTE_STREAM_STALE_MS;
    const PRICE_AXIS_WIDTH = 70;
    const CHART_LEFT_PAD = 14;
    const DEFAULT_BARS_VISIBLE = 120;
    const MAX_BARS_VISIBLE = 5000;
    const CHART_CANDLE_DETAIL_MIN_STEP_PX = 2;
    const CHART_CANDLE_OVERVIEW_MAX_STEP_PX = 0.75;
    const CHART_OVERVIEW_COLUMN_WIDTH_PX = 1;
    const HISTORY_PAGE_MIN_BARS = 300;
    const HISTORY_PAGE_MAX_BARS = 1000;
    const HISTORY_PAGE_VIEWPORTS = 2;
    const HISTORY_PREFETCH_VIEWPORTS = 2;
    const HISTORY_SUCCESS_NOTICE_MS = 2500;
    const DEFAULT_RIGHT_GAP_BARS = 0;
    const MAX_RIGHT_GAP_BARS = 720;
    const MAX_RIGHT_GAP_HOURS = 7 * 24;
    const TIME_ZOOM_SENSITIVITY = 0.00075;
    const PRICE_ZOOM_SENSITIVITY = 0.0022;
    const MIN_PRICE_ZOOM = 0.08;
    const MAX_PRICE_ZOOM = 12;
    const TIMEFRAMES = [
      { label: "1H", interval: "60m" },
      { label: "15M", interval: "15m" },
      { label: "5M", interval: "5m" },
      { label: "3M", interval: "3m" },
      { label: "1M", interval: "1m" },
    ];
    const RANGE_STEPS = {
      "1m": ["5d", "7d", "14d", "31d"],
      "3m": ["5d", "7d", "14d", "31d", "2mo", "3mo"],
      "5m": ["5d", "7d", "14d", "31d", "2mo", "3mo", "6mo"],
      "15m": ["5d", "7d", "14d", "31d", "2mo", "3mo", "6mo", "1y"],
      "60m": ["5d", "7d", "14d", "31d", "2mo", "3mo", "6mo", "1y", "2y", "5y"],
    };
    const DEFAULT_RANGE_BY_INTERVAL = {
      "1m": "5d",
      "3m": "5d",
      "5m": "5d",
      "15m": "5d",
      "60m": "31d",
    };
    const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    const CHANNEL_LEVELS = [0, 0.25, 0.5, 0.75, 1];
    const CHANNEL_GHOST_COPIES_DEFAULT = 3;
    const CHANNEL_GHOST_COPIES_MAX = 10;
    const DRAWING_LOCAL_EDIT_GRACE_MS = 8000;
    const EMA233_LEN = 233;
    const CANVAS_MONO_FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
    const DRAWING_POINT_COUNTS = { line: 2, zone: 2, fib: 2, channel: 3, ellipse: 3, text: 1 };
    const PATH_MIN_POINTS = 2;
    const DRAWING_MAX_ANCHOR_POINTS = 512;
