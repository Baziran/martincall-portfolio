    function workspaceSet(key, value, options = {}) {
      if (workspacePopout) return;
      const serialized = String(value);
      workspaceSessionSet(key, serialized);
      if (options.markDirty !== false) workspaceSessionMarkDirty(key);
      setServerSettingValue(workspaceKey(key), serialized);
    }

    function workspaceSetBool(key, value) {
      workspaceSet(key, value ? "true" : "false");
    }

    function syncWorkspacePageState() {
      if (workspacePopout) return;
      if (!state) return;
      if (instrumentForId(state.instrumentId)) {
        workspaceSet("instrumentId", state.instrumentId);
      }
      workspaceSet("timeframe", state.timeframe);
      workspaceSet("sideTab", state.sideTab || "instruments");
      workspaceSet("theme", state.settings?.theme || "dark", { markDirty: false });
      workspaceSet("viewPreset", normalizeViewPreset(state.settings?.viewPreset));
      workspaceSetBool("gexFocusMode", Boolean(state.settings?.gexFocusMode));
    }

    function chartWindowUrlForInstrument(instrumentId) {
      const url = new URL(window.location.href);
      url.searchParams.set("slot", state.workspaceSlot || activeWorkspaceSlot);
      url.searchParams.set("popout", "1");
      url.searchParams.set("instrument_id", typeof instrumentId === "string" ? instrumentId : "");
      url.searchParams.set("timeframe", state.timeframe);
      url.searchParams.set("range", state.range || defaultRangeFor(state.timeframe));
      return `${url.pathname}${url.search}${url.hash}`;
    }
