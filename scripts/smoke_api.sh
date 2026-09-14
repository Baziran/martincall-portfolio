#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
PYTHON_BIN="${PYTHON_BIN:-../../venv/bin/python}"

curl_bounded() {
  local max_time="$1"
  shift
  curl --fail --silent --show-error --connect-timeout 3 --max-time "$max_time" "$@"
}

echo "health: ${BASE_URL}/api/health"
curl_bounded 5 "${BASE_URL}/api/health" >/dev/null
echo "readiness: ${BASE_URL}/api/ready"
curl_bounded 5 "${BASE_URL}/api/ready" >/dev/null

INSTRUMENTS_JSON="$(curl_bounded 10 "${BASE_URL}/api/instruments")"
MARKET_QUERY="$("${PYTHON_BIN}" -c '
import json
import sys
from urllib.parse import urlencode

payload = json.load(sys.stdin)
items = payload.get("items") if isinstance(payload, dict) else None
if not isinstance(items, list) or not items:
    raise SystemExit("smoke requires one persisted watchlist instrument")
item = items[0]
instrument_id = str(item.get("instrument_id") or "")
route_fingerprint = str(item.get("route_fingerprint") or "")
if not instrument_id or not route_fingerprint:
    raise SystemExit("smoke instrument is missing exact route identity")
print(urlencode({
    "instrument_id": instrument_id,
    "expected_route_fingerprint": route_fingerprint,
    "interval": "5m",
    "range": "1d",
    "chart_only": "true",
}))
' <<<"${INSTRUMENTS_JSON}")"

echo "market chart-only: ${BASE_URL}/api/market"
curl_bounded 30 "${BASE_URL}/api/market?${MARKET_QUERY}" >/dev/null

echo "smoke ok"
