    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function maxRightGapBarsForView() {
      const timeframeMinutes = Math.max(Number(intervalMinutesFromState()) || 1, 1);
      const timeCap = Math.ceil((MAX_RIGHT_GAP_HOURS * 60) / timeframeMinutes);
      return Math.min(MAX_RIGHT_GAP_BARS, Math.max(DEFAULT_RIGHT_GAP_BARS, timeCap));
    }

    function clampRightGapBars(value) {
      return clamp(Math.round(Number(value) || DEFAULT_RIGHT_GAP_BARS), DEFAULT_RIGHT_GAP_BARS, maxRightGapBarsForView());
    }

    function rightGapBars() {
      view.rightGapBars = clampRightGapBars(view.rightGapBars);
      return view.rightGapBars;
    }

    let viewPanelRenderTimer = 0;

    function scheduleViewPanelRender(delayMs = 120) {
      if (viewPanelRenderTimer) clearTimeout(viewPanelRenderTimer);
      viewPanelRenderTimer = setTimeout(() => {
        viewPanelRenderTimer = 0;
        if (state.snapshot) renderPanel(state.snapshot);
      }, Math.max(Number(delayMs) || 0, 0));
    }

    function refreshView(options = {}) {
      if (!state.snapshot) return;
      if (!options.preserveSmoothOffset) view.smoothOffsetBars = 0;
      clampView(state.snapshot);
      if (options.panel === false) {
        // Caller owns panel refresh.
      } else if (Number.isFinite(Number(options.panelDelayMs)) && Number(options.panelDelayMs) > 0) {
        scheduleViewPanelRender(Number(options.panelDelayMs));
      } else {
        renderPanel(state.snapshot);
      }
      renderCharts(state.snapshot, { overlayImmediate: true });
    }

    function timeViewportVirtualOffset() {
      return Number(view.offset || 0)
        + Number(view.historyPullOffsetBars || 0)
        - Math.max(rightGapBars() - DEFAULT_RIGHT_GAP_BARS, 0);
    }

    function historyPullLimitBars() {
      return clamp(Math.round((Number(view.barsVisible) || DEFAULT_BARS_VISIBLE) * 0.75), 24, HISTORY_PAGE_MAX_BARS);
    }

    function applyTimeViewportVirtualOffset(virtualOffsetFloat) {
      const virtualOffset = Math.round(Number(virtualOffsetFloat) || 0);
      const maxOffset = maxOffsetFor(state.snapshot);
      view.historyPullOffsetBars = view.dragActive
        ? clamp(virtualOffset - maxOffset, 0, historyPullLimitBars())
        : 0;
      view.smoothOffsetBars = clamp(Number(virtualOffsetFloat) - virtualOffset, -0.499, 0.499);
      if (virtualOffset < 0) {
        view.offset = 0;
        view.rightGapBars = clampRightGapBars(DEFAULT_RIGHT_GAP_BARS - virtualOffset);
      } else {
        view.offset = clamp(virtualOffset, 0, maxOffset);
        view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
      }
      view.rightGapManual = view.rightGapBars > DEFAULT_RIGHT_GAP_BARS;
      clampView(state.snapshot);
      return { virtualOffset, maxOffset };
    }

    function panTimeByBars(deltaBars) {
      if (!state.snapshot || !Number.isFinite(Number(deltaBars)) || Number(deltaBars) === 0) return;
      if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(180);
      const applied = applyTimeViewportVirtualOffset(timeViewportVirtualOffset() + Number(deltaBars));
      view.followLatest = applied.virtualOffset <= 0;
      updateChartNavigationControls();
      scheduleViewStatePersist();
      refreshView({ panelDelayMs: 140, preserveSmoothOffset: true });
      maybeLoadMoreHistory();
    }

    function anchoredTimeViewportVirtualOffset({
      startBarsVisible,
      startVirtualOffset,
      startRightGapBars,
      nextBarsVisible,
      startAnchorFraction,
      anchorFraction = startAnchorFraction,
    }) {
      const initialBars = clamp(Math.round(Number(startBarsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      const barsVisible = clamp(Math.round(Number(nextBarsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      const initialGap = clampRightGapBars(startRightGapBars);
      const initialFraction = clamp(Number(startAnchorFraction) || 0, 0, 1);
      const fraction = clamp(Number(anchorFraction) || 0, 0, 1);
      const anchorBarsFromLatest = Number(startVirtualOffset)
        + initialGap - DEFAULT_RIGHT_GAP_BARS
        + initialBars
        - initialFraction * (initialBars + initialGap);
      const defaultGapOffset = anchorBarsFromLatest
        - barsVisible
        + fraction * (barsVisible + DEFAULT_RIGHT_GAP_BARS);
      return defaultGapOffset >= 0 || fraction === 0
        ? Math.max(defaultGapOffset, 0)
        : defaultGapOffset / fraction;
    }

    function zoomTime(factor, options = {}) {
      if (!state.snapshot) return;
      if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(180);
      const previousBarsVisible = clamp(Math.round(Number(view.barsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      const nextBarsVisible = clamp(Math.round(previousBarsVisible * factor), 24, MAX_BARS_VISIBLE);
      if (nextBarsVisible === previousBarsVisible) return;
      const anchorFraction = clamp(Number(options.anchorFraction ?? 1), 0, 1);
      const previousVirtualOffset = timeViewportVirtualOffset();
      const previousRightGapBars = rightGapBars();
      view.barsVisible = nextBarsVisible;
      const anchoredVirtualOffset = anchoredTimeViewportVirtualOffset({
        startBarsVisible: previousBarsVisible,
        startVirtualOffset: previousVirtualOffset,
        startRightGapBars: previousRightGapBars,
        nextBarsVisible,
        startAnchorFraction: anchorFraction,
      });
      const applied = applyTimeViewportVirtualOffset(anchoredVirtualOffset);
      view.followLatest = applied.virtualOffset <= 0;
      updateChartNavigationControls();
      scheduleViewStatePersist();
      refreshView({ panelDelayMs: 140, preserveSmoothOffset: true });
      maybeLoadMoreHistory();
    }

    function pinchTimeViewport({
      startBarsVisible,
      startVirtualOffset,
      startRightGapBars,
      startDistance,
      currentDistance,
      startMidpointFraction,
      midpointFraction,
    }) {
      const initialBars = clamp(Math.round(Number(startBarsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      const initialDistance = Math.max(Number(startDistance) || 0, 24);
      const distance = Math.max(Number(currentDistance) || 0, 24);
      const barsVisible = clamp(Math.round(initialBars * initialDistance / distance), 24, MAX_BARS_VISIBLE);
      return {
        barsVisible,
        virtualOffset: anchoredTimeViewportVirtualOffset({
          startBarsVisible: initialBars,
          startVirtualOffset,
          startRightGapBars,
          nextBarsVisible: barsVisible,
          startAnchorFraction: startMidpointFraction,
          anchorFraction: midpointFraction,
        }),
      };
    }

    function zoomPrice(factor) {
      if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(180);
      view.priceZoom = clamp(view.priceZoom * factor, MIN_PRICE_ZOOM, MAX_PRICE_ZOOM);
      scheduleViewStatePersist();
      refreshView({ panelDelayMs: 140 });
    }

    function fitPriceHeight() {
      view.priceZoom = 1;
      view.priceShift = 0;
      scheduleViewStatePersist();
      refreshView({ panelDelayMs: 140, overlayImmediate: true });
    }

    function wheelFactor(delta, sensitivity) {
      return Math.exp(delta * sensitivity);
    }

    function resetView() {
      cancelHistoryDemand({ abort: true });
      view.barsVisible = DEFAULT_BARS_VISIBLE;
      view.offset = 0;
      view.priceZoom = 1;
      view.priceShift = 0;
      view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
      view.rightGapManual = false;
      view.followLatest = true;
      view.historyPullOffsetBars = 0;
      updateChartNavigationControls();
      persistViewState();
      refreshView();
    }

    function historyViewportAnchor(snapshot) {
      const visible = visibleBars(snapshot);
      const anchorBar = visible.bars[0] || null;
      const anchorTs = String(anchorBar?.ts || "");
      return { ts: anchorTs, screenIndex: 0 };
    }

    function historyAnchorIndex(bars, anchorTs) {
      const anchorText = String(anchorTs || "").trim();
      const numericTarget = Number(anchorText);
      const target = anchorText && Number.isFinite(numericTarget)
        ? numericTarget
        : Date.parse(anchorText);
      if (!Number.isFinite(target) || !Array.isArray(bars)) return -1;
      let low = 0;
      let high = bars.length - 1;
      while (low <= high) {
        const middle = Math.floor((low + high) / 2);
        const candidate = Date.parse(bars[middle]?.ts || "");
        if (candidate === target) return middle;
        if (!Number.isFinite(candidate) || candidate > target) high = middle - 1;
        else low = middle + 1;
      }
      return -1;
    }

    function applyHistoryViewportAnchor(snapshot, options = {}) {
      const bars = snapshot?.bars || [];
      const previousBars = options.previousSnapshot?.bars?.length ?? options.previousBars;
      const addedBars = Math.max(bars.length - Math.max(Number(previousBars) || 0, 0), 0);
      if (view.followLatest) {
        view.offset = 0;
        view.historyPullOffsetBars = 0;
        return true;
      }
      const count = Math.min(
        clamp(Math.round(Number(view.barsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE),
        bars.length,
      );
      const commitAnchor = !view.followLatest && options.previousSnapshot?.bars?.length
        ? historyViewportAnchor(options.previousSnapshot)
        : null;
      const anchorTs = commitAnchor?.ts || options.historyAnchorTs;
      const anchorScreenIndex = commitAnchor?.ts ? commitAnchor.screenIndex : options.historyAnchorScreenIndex;
      const anchorIndex = historyAnchorIndex(bars, anchorTs);
      if (anchorIndex >= 0) {
        const screenIndex = clamp(Math.round(Number(anchorScreenIndex) || 0), 0, Math.max(count - 1, 0));
        const targetStart = clamp(anchorIndex - screenIndex, 0, Math.max(bars.length - count, 0));
        view.offset = clamp(bars.length - Math.min(targetStart + count, bars.length), 0, maxOffsetFor(snapshot));
      } else {
        view.offset = clamp(Math.max(Number(options.previousOffset) || 0, 0), 0, maxOffsetFor(snapshot));
      }
      const revealBars = view.dragActive
        ? clamp(Math.round(Number(view.historyPullOffsetBars) || 0), 0, addedBars)
        : 0;
      view.offset = clamp(view.offset + revealBars, 0, maxOffsetFor(snapshot));
      view.historyPullOffsetBars = 0;
      if (view.dragActive) {
        const startFutureGap = Math.max(Number(view.dragStartRightGapBars) - DEFAULT_RIGHT_GAP_BARS, 0);
        view.dragStartOffset = view.offset - (Number(view.dragCurrentDeltaBars) || 0) + startFutureGap;
        view.dragStartHistoryPullOffsetBars = 0;
      }
      return anchorIndex >= 0;
    }

    function updateChartNavigationControls() {
      const liveToggle = document.getElementById("live-toggle");
      if (liveToggle) {
        liveToggle.hidden = Boolean(view.followLatest);
        liveToggle.classList.toggle("active", view.followLatest);
      }
    }

    function scheduleHistorySuccessNoticeClear(notice) {
      if (notice?.owner !== "history" || notice.code !== "HISTORY_PAGE_LOADED") return false;
      setTimeout(() => {
        if (state.notices?.history?.sequence !== notice.sequence) return;
        if (!clearUiNotice("history", notice.code)) return;
        if (state.snapshot) renderPanel(state.snapshot);
      }, HISTORY_SUCCESS_NOTICE_MS);
      return true;
    }

    function cancelHistoryDemand(options = {}) {
      const history = state.requests.history;
      history.queued = false;
      history.intentActive = false;
      view.historyPullOffsetBars = 0;
      if (options.abort === true && history.abort) history.abort.abort();
    }

    function resetHistoryDemand() {
      const history = state.requests.history;
      history.sequence += 1;
      cancelHistoryDemand({ abort: true });
      history.loading = false;
      history.exhausted = false;
    }

    function historyPrefetchNeeded() {
      const bars = state.snapshot?.bars || [];
      if (!bars.length || state.requests.history.exhausted) return false;
      if (view.barsVisible > bars.length) return true;
      const remainingBars = maxOffsetFor(state.snapshot) - Number(view.offset || 0);
      const prefetchBars = clamp(
        Math.round((Number(view.barsVisible) || DEFAULT_BARS_VISIBLE) * HISTORY_PREFETCH_VIEWPORTS),
        HISTORY_PAGE_MIN_BARS,
        HISTORY_PAGE_MAX_BARS,
      );
      return remainingBars <= prefetchBars;
    }

    async function requestMoreHistory() {
      const history = state.requests.history;
      if (!state.snapshot || history.loading || history.exhausted || !history.queued) return;
      history.queued = false;
      history.loading = true;
      const requestSequence = history.sequence + 1;
      history.sequence = requestSequence;
      const earliest = state.snapshot.bars.find(bar => barIsConfirmedClosed(bar));
      const beforeTs = state.snapshot.meta?.client_history_next_before_ts || earliest?.ts || "";
      let loadedBars = 0;
      try {
        if (!beforeTs) return;
        setUiNotice("history", "HISTORY_LOADING", "Loading older bars from local DB...", { state: "loading" });
        renderPanel(state.snapshot);
        loadedBars = await loadHistoryPage({
          beforeTs,
          limit: clamp(
            Math.round((Number(view.barsVisible) || DEFAULT_BARS_VISIBLE) * HISTORY_PAGE_VIEWPORTS),
            HISTORY_PAGE_MIN_BARS,
            HISTORY_PAGE_MAX_BARS,
          ),
        });
      } finally {
        if (history.sequence !== requestSequence) return;
        history.loading = false;
        if (!loadedBars) view.historyPullOffsetBars = 0;
        clearUiNotice("history", "HISTORY_LOADING");
        if (state.snapshot) renderPanel(state.snapshot);
        const continueExactIntent = (
          history.intentActive === true
          && loadedBars > 0
          && historyPrefetchNeeded()
        );
        history.queued = continueExactIntent;
        if (continueExactIntent) setTimeout(() => void requestMoreHistory(), 0);
        else history.intentActive = false;
      }
    }

    function maybeLoadMoreHistory() {
      const history = state.requests.history;
      if (!historyPrefetchNeeded()) {
        history.queued = false;
        history.intentActive = false;
        return;
      }
      history.intentActive = true;
      history.queued = true;
      void requestMoreHistory();
    }

    function setFollowLatest(value, options = {}) {
      const followLatest = Boolean(value);
      const refreshLiveTail = followLatest && (!view.followLatest || options.refreshTail === true);
      const liveTailCovered = refreshLiveTail && snapshotCoversCanonicalLiveTail(state.snapshot);
      view.followLatest = followLatest;
      if (followLatest) {
        cancelHistoryDemand({ abort: true });
        state.requests.history.exhausted = false;
        view.offset = 0;
        view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
        view.rightGapManual = false;
        setServerSettingValue("aef:rightGapBars", view.rightGapBars);
        setServerSettingValue("aef:rightGapManual", "false");
        if (refreshLiveTail && !liveTailCovered) {
          if (state.requests.history.loading && state.requests.market.abort) state.requests.market.abort.abort();
          if (typeof restoreCanonicalLiveTail === "function") restoreCanonicalLiveTail();
          setUiNotice("history", "HISTORY_RETURNING_TO_TAIL", "Returning to the current market tail...", { state: "loading" });
          connectChartStream(false);
          load({
            force: true,
            liveTailRefresh: true,
            range: initialRangeFor(state.timeframe),
            queueAnalysis: true,
            requireLatestAnalysis: true,
          });
        } else if (refreshLiveTail) {
          clearUiNotice("history");
          connectChartStream(false);
        }
      }
      setServerSettingValue("aef:followLatest", view.followLatest);
      updateChartNavigationControls();
      refreshView();
    }

    function chartNavigationKeydown(event) {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.target?.closest?.("input, textarea, select, button, [contenteditable='true'], [role='dialog']")) return;
      const page = Math.max(Math.round((Number(view.barsVisible) || DEFAULT_BARS_VISIBLE) * 0.9), 1);
      const actions = {
        ArrowLeft: () => panTimeByBars(1),
        ArrowRight: () => panTimeByBars(-1),
        PageUp: () => panTimeByBars(page),
        PageDown: () => panTimeByBars(-page),
        Home: () => panTimeByBars(maxOffsetFor(state.snapshot) - Number(view.offset || 0)),
        End: () => setFollowLatest(true, { refreshTail: true }),
      };
      const action = actions[event.key];
      if (!action || !state.snapshot) return;
      event.preventDefault();
      action();
    }

    let chartNavigationKeyboardReady = false;

    function setupChartNavigationKeyboard() {
      if (chartNavigationKeyboardReady) return;
      chartNavigationKeyboardReady = true;
      document.addEventListener("keydown", chartNavigationKeydown);
    }
