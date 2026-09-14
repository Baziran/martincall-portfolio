    const WORKSPACE_SLOTS = ["1", "2", "3", "4"];

    function normalizeWorkspaceSlot(value) {
      const slot = String(value || "1");
      return WORKSPACE_SLOTS.includes(slot) ? slot : "1";
    }

    function workspaceSlotFromUrl() {
      try {
        return new URL(window.location.href).searchParams.get("slot");
      } catch {
        return null;
      }
    }

    function workspaceUrlValue(key) {
      try {
        return new URL(window.location.href).searchParams.get(key);
      } catch {
        return null;
      }
    }

    const workspacePopout = workspaceUrlValue("popout") === "1";
    const activeWorkspaceSlot = normalizeWorkspaceSlot(
      workspaceSlotFromUrl()
      || sessionStorage.getItem("aef:workspaceSlot")
      || "1",
    );
    sessionStorage.setItem("aef:workspaceSlot", activeWorkspaceSlot);

    function workspaceKey(key, slot = activeWorkspaceSlot) {
      return `aef:workspace:${normalizeWorkspaceSlot(slot)}:${key}`;
    }

    function normalizeThemeSetting(value, fallback = "dark") {
      const normalized = String(value || "").trim().toLowerCase();
      if (normalized === "light" || normalized === "dark") return normalized;
      return fallback;
    }

    function workspaceSessionSet(key, value, slot = activeWorkspaceSlot) {
      try {
        sessionStorage.setItem(workspaceKey(key, slot), String(value));
      } catch {
        // Session storage is optional; server settings remain authoritative.
      }
    }

    function workspaceSessionValue(key, slot = activeWorkspaceSlot) {
      try {
        return sessionStorage.getItem(workspaceKey(key, slot));
      } catch {
        return null;
      }
    }

    function workspaceSessionDirtyKey(key, slot = activeWorkspaceSlot) {
      return `${workspaceKey(key, slot)}:dirty`;
    }

    function workspaceSessionMarkDirty(key, slot = activeWorkspaceSlot) {
      try {
        sessionStorage.setItem(workspaceSessionDirtyKey(key, slot), "1");
      } catch {
        // Dirty tracking is a wake/resume guard; persistence remains best-effort.
      }
    }

    function workspaceSessionClearDirty(key, slot = activeWorkspaceSlot) {
      try {
        sessionStorage.removeItem(workspaceSessionDirtyKey(key, slot));
      } catch {
        // Session storage is optional.
      }
    }

    function workspaceSessionDirty(key, slot = activeWorkspaceSlot) {
      try {
        return sessionStorage.getItem(workspaceSessionDirtyKey(key, slot)) === "1";
      } catch {
        return false;
      }
    }

    function workspaceServerValue(key, slot = activeWorkspaceSlot) {
      return serverSettingValue(workspaceKey(key, slot));
    }

    function workspaceStoredValue(key, slot = activeWorkspaceSlot, options = {}) {
      const urlValue = workspaceUrlValue(key);
      if (workspacePopout && normalizeWorkspaceSlot(slot) === activeWorkspaceSlot && urlValue !== null) return urlValue;
      if (options.preferServer) {
        const serverValue = workspaceServerValue(key, slot);
        if (serverValue !== null) return serverValue;
      }
      const sessionValue = workspaceSessionValue(key, slot);
      if (sessionValue !== null) return sessionValue;
      const serverValue = workspaceServerValue(key, slot);
      if (serverValue !== null) {
        workspaceSessionSet(key, serverValue, slot);
        return serverValue;
      }
      return null;
    }

    function workspaceValue(key) {
      return workspaceStoredValue(key, activeWorkspaceSlot);
    }

    function workspaceAuthoritativeValue(key, fallback = null, slot = activeWorkspaceSlot) {
      const sessionValue = workspaceSessionValue(key, slot);
      const serverValue = workspaceServerValue(key, slot);
      if (serverStorageReady && workspaceSessionDirty(key, slot) && sessionValue !== null) {
        if (serverValue === sessionValue) {
          workspaceSessionClearDirty(key, slot);
          if (key === "theme" && window.mcTelemetrySet) window.mcTelemetrySet("theme_source", "server_confirmed");
        } else {
          if (key === "theme" && window.mcTelemetrySet) window.mcTelemetrySet("theme_source", "session_dirty");
          return sessionValue;
        }
      }
      const value = workspaceStoredValue(key, slot, { preferServer: Boolean(serverStorageReady) });
      if (key === "theme" && window.mcTelemetrySet) {
        const source = value === serverValue && serverValue !== null ? "server" : value === sessionValue && sessionValue !== null ? "session" : value === null ? "fallback" : "workspace";
        window.mcTelemetrySet("theme_source", source);
      }
      return value === null ? fallback : value;
    }

    const initialSymbol = "";
    const initialInstrumentIdFromUrl = workspaceUrlValue("instrument_id");
    const initialInstrumentId = String(
      initialInstrumentIdFromUrl !== null
        ? initialInstrumentIdFromUrl
        : workspaceValue("instrumentId") ?? "",
    );
    const initialTimeframe = workspaceValue("timeframe") || "5m";
    const initialDataSource = "";
