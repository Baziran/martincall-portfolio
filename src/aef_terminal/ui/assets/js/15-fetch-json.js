    const inFlightJsonRequests = new Map();
    const SHARED_FETCH_CACHE_PREFIX = "aef:sharedFetch:";
    const SHARED_FETCH_CACHE_MAX_ENTRIES = 48;
    const SHARED_FETCH_CACHE_MAX_AGE_MS = 10 * 60 * 1000;

    function normalizeErrorPayload(response) {
      if (!response || typeof response !== "object") return null;
      const detail = response.detail;
      if (detail && typeof detail === "object" && detail.error && typeof detail.error === "object") {
        return detail;
      }
      return response;
    }

    function apiErrorMessage(response, fallback = "request failed") {
      const payload = normalizeErrorPayload(response);
      if (!payload || typeof payload !== "object") return fallback;
      const structured = payload.error && typeof payload.error === "object" ? payload.error : null;
      if (structured?.message) {
        const code = structured.code ? `[${structured.code}] ` : "";
        return `${code}${structured.message}`;
      }
      const detail = payload.detail;
      if (typeof detail === "string" && detail.trim()) return detail.trim();
      if (Array.isArray(detail)) {
        const messages = detail
          .map(item => {
            if (!item || typeof item !== "object") return "";
            const message = String(item.msg || item.message || "").trim();
            if (!message) return "";
            const location = Array.isArray(item.loc)
              ? item.loc.filter(part => String(part) !== "query").map(String).join(".")
              : "";
            return location ? `${location}: ${message}` : message;
          })
          .filter(Boolean)
          .slice(0, 3);
        if (messages.length) return messages.join("; ");
      }
      return payload.message || fallback;
    }

    function requestErrorMessage(error, fallback = "request failed") {
      if (!error) return fallback;
      if (typeof error === "string") return error;
      const payload = error.payload && typeof error.payload === "object" ? normalizeErrorPayload(error.payload) : null;
      if (payload) return apiErrorMessage(payload, fallback);
      return error.message || fallback;
    }

    function requestErrorCode(error) {
      const payload = error?.payload && typeof error.payload === "object"
        ? normalizeErrorPayload(error.payload)
        : null;
      return String(payload?.error?.code || "").trim();
    }

    function fetchJsonDedupeKey(url, options = {}) {
      const method = String(options.method || "GET").toUpperCase();
      if (method !== "GET" || options.body || options.dedupe === false) return "";
      return `${method} ${String(url)}`;
    }

    function sharedFetchAbortError() {
      return new DOMException("Request aborted", "AbortError");
    }

    function consumeSharedJsonRequest(entry, signal = null) {
      entry.consumers += 1;
      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        entry.consumers = Math.max(Number(entry.consumers || 0) - 1, 0);
        if (!entry.settled && entry.consumers === 0 && !entry.controller.signal.aborted) {
          entry.controller.abort("no active fetch consumers");
        }
      };
      if (signal?.aborted) {
        release();
        return Promise.reject(sharedFetchAbortError());
      }
      return new Promise((resolve, reject) => {
        const onAbort = () => {
          signal?.removeEventListener("abort", onAbort);
          release();
          reject(sharedFetchAbortError());
        };
        signal?.addEventListener("abort", onAbort, { once: true });
        entry.promise.then(
          payload => {
            signal?.removeEventListener("abort", onAbort);
            release();
            resolve(payload);
          },
          error => {
            signal?.removeEventListener("abort", onAbort);
            release();
            reject(error);
          },
        );
      });
    }

    function readSharedFetchEnvelope(key, ttlMs) {
      if (!key || !Number.isFinite(Number(ttlMs)) || Number(ttlMs) <= 0) return null;
      try {
        const storageKey = `${SHARED_FETCH_CACHE_PREFIX}${key}`;
        const envelope = JSON.parse(localStorage.getItem(storageKey) || "null");
        if (!envelope || typeof envelope !== "object") return null;
        const age = Date.now() - Number(envelope.fetched_at || 0);
        if (age < 0 || age > Number(ttlMs)) {
          localStorage.removeItem(storageKey);
          return null;
        }
        return envelope.payload;
      } catch (_) {
        return null;
      }
    }

    function sharedFetchCacheEntries(now = Date.now()) {
      const entries = [];
      for (const key of localStorageKeys()) {
        if (!String(key).startsWith(SHARED_FETCH_CACHE_PREFIX)) continue;
        try {
          const envelope = JSON.parse(localStorage.getItem(key) || "null");
          const fetchedAt = Number(envelope?.fetched_at || 0);
          const ttl = Number(envelope?.ttl || 0);
          entries.push({ key, fetchedAt, ttl, stale: !fetchedAt || now - fetchedAt > Math.max(ttl, SHARED_FETCH_CACHE_MAX_AGE_MS) });
        } catch (_) {
          entries.push({ key, fetchedAt: 0, ttl: 0, stale: true });
        }
      }
      return entries;
    }

    function pruneSharedFetchCache(options = {}) {
      const keepKey = options.keepKey ? `${SHARED_FETCH_CACHE_PREFIX}${options.keepKey}` : "";
      const now = Date.now();
      let removed = 0;
      const entries = sharedFetchCacheEntries(now);
      for (const entry of entries) {
        if (entry.key === keepKey || !entry.stale) continue;
        try {
          localStorage.removeItem(entry.key);
          removed += 1;
        } catch (_) {
          // Keep fetch cache pruning best-effort.
        }
      }
      const remaining = sharedFetchCacheEntries(now)
        .filter(entry => entry.key !== keepKey)
        .sort((a, b) => Number(a.fetchedAt || 0) - Number(b.fetchedAt || 0));
      const overflow = Math.max(0, remaining.length - SHARED_FETCH_CACHE_MAX_ENTRIES);
      for (const entry of remaining.slice(0, overflow)) {
        try {
          localStorage.removeItem(entry.key);
          removed += 1;
        } catch (_) {
          // Keep fetch cache pruning best-effort.
        }
      }
      if (removed && window.mcDebugStep) window.mcDebugStep("shared fetch cache", `pruned ${removed}`);
      return removed;
    }

    function writeSharedFetchEnvelope(key, payload, ttlMs) {
      if (!key || !Number.isFinite(Number(ttlMs)) || Number(ttlMs) <= 0) return;
      try {
        pruneSharedFetchCache({ keepKey: key });
        rawLocalStorageSetItem(`${SHARED_FETCH_CACHE_PREFIX}${key}`, JSON.stringify({
          key,
          payload,
          fetched_at: Date.now(),
          ttl: Number(ttlMs),
        }));
      } catch (_) {
        // localStorage can be full or unavailable in private contexts.
      }
    }

    async function fetchJson(url, options = {}) {
      const consumerSignal = options.signal || null;
      if (consumerSignal?.aborted) throw sharedFetchAbortError();
      const key = fetchJsonDedupeKey(url, options);
      const sharedTtlMs = Number(options.sharedTtlMs || 0);
      const telemetryReason = String(options.telemetryReason || "").trim();
      const cachedPayload = readSharedFetchEnvelope(key, sharedTtlMs);
      if (cachedPayload !== null) return cachedPayload;
      if (key && inFlightJsonRequests.has(key)) {
        const existing = inFlightJsonRequests.get(key);
        if (!existing.controller.signal.aborted) {
          return consumeSharedJsonRequest(existing, consumerSignal);
        }
        inFlightJsonRequests.delete(key);
      }
      const perfToken = window.mcPerfStart ? window.mcPerfStart("fetch") : null;
      const perfLabel = String(url || "").split("?")[0] || "fetch";
      if (window.mcTelemetryInc) {
        window.mcTelemetryInc("fetch.total");
        window.mcTelemetryInc(`fetch.route:${perfLabel}`);
      }
      const fetchOptions = { ...options };
      delete fetchOptions.dedupe;
      delete fetchOptions.sharedTtlMs;
      delete fetchOptions.telemetryReason;
      if (telemetryReason && window.mcTelemetryInc) {
        window.mcTelemetryInc(`fetch.reason:${perfLabel}:${telemetryReason}`);
      }
      const timeoutMs = Number(fetchOptions.timeoutMs || 0);
      delete fetchOptions.timeoutMs;
      const warnMs = Number(fetchOptions.warnMs || 0);
      delete fetchOptions.warnMs;
      let timeoutController = null;
      let timeoutTimer = null;
      let timeoutTriggered = false;
      let upstreamAbort = null;
      const upstreamSignal = key ? null : (fetchOptions.signal || null);
      if (key || (Number.isFinite(timeoutMs) && timeoutMs > 0)) {
        timeoutController = new AbortController();
        fetchOptions.signal = timeoutController.signal;
        if (upstreamSignal?.aborted) {
          timeoutController.abort(upstreamSignal.reason);
        } else if (upstreamSignal) {
          upstreamAbort = () => timeoutController.abort(upstreamSignal.reason);
          upstreamSignal.addEventListener("abort", upstreamAbort, { once: true });
        }
        if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
          timeoutTimer = window.setTimeout(() => {
            timeoutTriggered = true;
            timeoutController.abort();
          }, timeoutMs);
        }
      }
      const request = (async () => {
        try {
          const headersPerf = window.mcPerfStart ? window.mcPerfStart("fetchHeaders") : null;
          const response = await fetch(url, fetchOptions);
          if (window.mcPerfEnd) window.mcPerfEnd(headersPerf, perfLabel, 250);
          const serverTiming = String(response.headers?.get?.("server-timing") || "");
          if (serverTiming && window.mcRecordBackendTiming) {
            serverTiming.split(",").forEach(item => {
              const parts = item.trim().split(";");
              const name = String(parts.shift() || "request").trim();
              const durationPart = parts.find(part => part.trim().startsWith("dur="));
              const duration = Number(String(durationPart || "").trim().slice(4));
              if (name && Number.isFinite(duration) && duration >= 0) {
                window.mcRecordBackendTiming(`${perfLabel}:${name}`, duration, perfLabel);
              }
            });
          }
          const bodyPerf = window.mcPerfStart ? window.mcPerfStart("fetchBody") : null;
          const text = await response.text();
          if (window.mcPerfEnd) window.mcPerfEnd(bodyPerf, perfLabel, 120);
          const responseBytes = new TextEncoder().encode(text).length;
          window.mcTelemetryInc(`fetch.bytes:${perfLabel}`, responseBytes);
          window.mcTelemetryMax(`fetch.bytes.max:${perfLabel}`, responseBytes);
          if (!response.ok) {
            let payload = null;
            const errorParsePerf = window.mcPerfStart ? window.mcPerfStart("fetchJsonParse") : null;
            try {
              const parsed = JSON.parse(text);
              if (parsed && typeof parsed === "object") payload = parsed;
            } catch (_) {
              // Keep plain HTTP error text when body is not JSON.
            } finally {
              if (window.mcPerfEnd) window.mcPerfEnd(errorParsePerf, perfLabel, 40);
            }
            const normalized = payload ? normalizeErrorPayload(payload) : null;
            const error = new Error(
              normalized ? apiErrorMessage(normalized, `HTTP ${response.status}`) : `HTTP ${response.status}: ${text.slice(0, 180) || response.statusText}`,
            );
            if (normalized) error.payload = normalized;
            else if (payload) error.payload = payload;
            throw error;
          }
          const parsePerf = window.mcPerfStart ? window.mcPerfStart("fetchJsonParse") : null;
          try {
            const payload = JSON.parse(text);
            writeSharedFetchEnvelope(key, payload, sharedTtlMs);
            return payload;
          } catch (error) {
            throw new Error(`Invalid JSON from server: ${text.slice(0, 180) || error.message}`);
          } finally {
            if (window.mcPerfEnd) window.mcPerfEnd(parsePerf, perfLabel, 40);
          }
        } catch (error) {
          if (timeoutTriggered && error?.name === "AbortError") {
            const timeoutError = new Error(`Request timed out after ${Math.round(timeoutMs)}ms`);
            timeoutError.name = "TimeoutError";
            throw timeoutError;
          }
          throw error;
        } finally {
          if (timeoutTimer) window.clearTimeout(timeoutTimer);
          if (upstreamSignal && upstreamAbort) upstreamSignal.removeEventListener("abort", upstreamAbort);
          const fetchWarnMs = Number.isFinite(warnMs) && warnMs > 0 ? warnMs : (perfLabel === "/api/storage" ? 650 : 500);
          if (window.mcPerfEnd) window.mcPerfEnd(perfToken, perfLabel, fetchWarnMs);
        }
      })();
      if (key) {
        const entry = {
          promise: request,
          controller: timeoutController,
          consumers: 0,
          settled: false,
        };
        inFlightJsonRequests.set(key, entry);
        request.then(
          () => {
            entry.settled = true;
            if (inFlightJsonRequests.get(key) === entry) inFlightJsonRequests.delete(key);
          },
          () => {
            entry.settled = true;
            if (inFlightJsonRequests.get(key) === entry) inFlightJsonRequests.delete(key);
          },
        );
        return consumeSharedJsonRequest(entry, consumerSignal);
      }
      return request;
    }
