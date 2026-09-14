    const nativeLocalStorageSetItem = localStorage.setItem.bind(localStorage);
    const SERVER_SETTING_RETRY_MS = 2000;
    const SERVER_SETTING_MUTATION_HIGH_WATER_PREFIX = "martincall:settings-mutation-high-water-ms:v1:";
    const SERVER_SETTING_SNAPSHOT_RECONCILE_MS = 60000;
    function requireClientSettingsKeyContract() {
      const contract = window.CLIENT_SETTINGS_CONTRACT_MANIFEST;
      const expectedKeys = [
        "browser_scalar_keys",
        "current_scalar_keys",
        "indicator_keys",
        "indicator_modes",
        "instrument_scalar_names",
        "version",
        "workspace_scalar_keys",
        "workspace_slots",
        "workspace_timeframes",
        "workspace_tuple_prefixes",
      ];
      if (
        !contract
        || typeof contract !== "object"
        || Array.isArray(contract)
        || Object.keys(contract).sort().join(",") !== expectedKeys.sort().join(",")
        || contract.version !== 2
        || !contract.indicator_keys
        || typeof contract.indicator_keys !== "object"
        || Array.isArray(contract.indicator_keys)
        || Object.keys(contract.indicator_keys).sort().join(",") !== "global,instrument"
      ) throw new TypeError("CLIENT_SETTINGS_CONTRACT_MANIFEST_INVALID");
      const stringSet = value => {
        if (
          !Array.isArray(value)
          || value.some(item => typeof item !== "string" || !item)
          || new Set(value).size !== value.length
        ) throw new TypeError("CLIENT_SETTINGS_CONTRACT_MANIFEST_INVALID");
        return new Set(value);
      };
      const parsed = {
        currentScalarKeys: stringSet(contract.current_scalar_keys),
        browserScalarKeys: stringSet(contract.browser_scalar_keys),
        workspaceSlots: stringSet(contract.workspace_slots),
        workspaceScalarKeys: stringSet(contract.workspace_scalar_keys),
        workspaceTuplePrefixes: stringSet(contract.workspace_tuple_prefixes),
        workspaceTimeframes: stringSet(contract.workspace_timeframes),
        instrumentScalarNames: stringSet(contract.instrument_scalar_names),
        indicatorModes: stringSet(contract.indicator_modes),
        indicatorKeys: {
          global: stringSet(contract.indicator_keys.global),
          instrument: stringSet(contract.indicator_keys.instrument),
        },
      };
      if (
        Array.from(parsed.browserScalarKeys).some(key => !parsed.currentScalarKeys.has(key))
      ) throw new TypeError("CLIENT_SETTINGS_CONTRACT_MANIFEST_INVALID");
      return parsed;
    }
    const clientSettingsKeyContract = requireClientSettingsKeyContract();
    if (!("BroadcastChannel" in window)) {
      throw new Error("SETTINGS_MUTATION_BROADCAST_REQUIRED");
    }
    const serverSettingWriterId = createBrowserUuidV4();
    if (!SERVER_SETTING_WRITER_ID_PATTERN.test(serverSettingWriterId)) {
      throw new TypeError("SETTINGS_MUTATION_WRITER_INVALID");
    }
    let serverSettingWriterSequence = 0;
    let serverSettingChangedAtMs = serverSettingMutationHighWater();
    const serverSettingWriterHighWaterKey = `${SERVER_SETTING_MUTATION_HIGH_WATER_PREFIX}${serverSettingWriterId}`;
    const serverSettings = {};
    const serverSettingQueue = new Map();
    const serverSettingLatestMutations = new Map();
    const serverSettingAuthoritativeOrders = new Map();
    const serverSettingFailures = new Map();
    const serverSettingSyncChannel = new BroadcastChannel("martincall-settings-sync-v1");
    let serverSettingFlushTimer = null;
    const serverSettingFlushRequests = new Set();
    let serverSettingSnapshotReconcilePromise = null;
    let serverSettingAuthoritativeRevision = 0;
    let serverSettingCompleteSnapshotRevision = 0;
    let serverSettingCompleteSnapshotKeys = new Set();
    const serverSettingsSaveState = {
      phase: "saved",
      pending: 0,
      error: "",
    };
    let serverStorageReady = false;
    let serverDrawingSignature = "";
    let serverDrawingRuntimeSignature = "";
    const serverPersistedDrawingSignatures = new Map();
    const serverPendingDrawingWrites = new Map();
    const serverQueuedDrawingWrites = new Map();
    const serverDirtyDrawingScopes = new Set();
    const serverAppliedAlertSignatures = new Map();
    const serverPendingAlertWrites = new Map();
    const serverAlertCommandChains = new Map();
    const serverAlertCommandGenerations = new Map();
    const serverAlertCommandTargets = new Map();
    const serverAlertRuntimeFeedbackReadyScopes = new Set();
    let sharedObjectSyncInFlight = null;

    function setServerSettingsSaveState(phase, error = "") {
      let nextPhase = ["saving", "saved", "error"].includes(phase) ? phase : "saved";
      let nextError = String(error || "");
      if (nextPhase === "saved" && serverSettingFailures.size) {
        nextPhase = "error";
        nextError = String(Array.from(serverSettingFailures.values()).at(-1) || "settings save failed");
      }
      serverSettingsSaveState.phase = nextPhase;
      serverSettingsSaveState.pending = serverSettingQueue.size
        + serverSettingFlushRequests.size;
      serverSettingsSaveState.error = nextError;
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("martincall:settings-save-state", {
          detail: { ...serverSettingsSaveState },
        }));
      }
    }

    function isStorageQuotaError(error) {
      return error?.name === "QuotaExceededError" || error?.code === 22 || error?.code === 1014;
    }

    function localStorageKeys() {
      const keys = [];
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (key) keys.push(key);
      }
      return keys;
    }

    function serverSettingMutationHighWater() {
      let highWater = 0;
      for (const key of localStorageKeys()) {
        if (!key.startsWith(SERVER_SETTING_MUTATION_HIGH_WATER_PREFIX)) continue;
        const writerId = key.slice(SERVER_SETTING_MUTATION_HIGH_WATER_PREFIX.length);
        const value = Number(localStorage.getItem(key));
        if (
          !SERVER_SETTING_WRITER_ID_PATTERN.test(writerId)
          || !Number.isSafeInteger(value)
          || value < 0
        ) {
          throw new TypeError("SETTINGS_MUTATION_HIGH_WATER_INVALID");
        }
        highWater = Math.max(highWater, value);
      }
      return highWater;
    }

    function isPrunableLocalStorageKey(key) {
      const value = String(key || "");
      return value.startsWith("aef:sharedFetch:")
        || value.startsWith("aef:sharedStream:");
    }

    function canonicalClientSettingScope(value, length) {
      if (typeof value !== "string") return null;
      let scope = null;
      try {
        scope = JSON.parse(value);
      } catch (_) {
        return null;
      }
      if (
        !Array.isArray(scope)
        || scope.length !== length
        || scope.some(item => typeof item !== "string" || !item)
        || JSON.stringify(scope) !== value
      ) return null;
      return scope;
    }

    function isCurrentClientSettingKey(key) {
      if (typeof key !== "string") return false;
      if (clientSettingsKeyContract.currentScalarKeys.has(key)) return true;
      if (key.startsWith("aef:workspace:")) {
        const workspace = key.slice("aef:workspace:".length);
        const slotSeparator = workspace.indexOf(":");
        if (slotSeparator < 1) return false;
        const slot = workspace.slice(0, slotSeparator);
        const settingName = workspace.slice(slotSeparator + 1);
        if (!clientSettingsKeyContract.workspaceSlots.has(slot)) return false;
        if (clientSettingsKeyContract.workspaceScalarKeys.has(settingName)) return true;
        const settingSeparator = settingName.indexOf(":");
        if (settingSeparator < 1) return false;
        const prefix = settingName.slice(0, settingSeparator);
        const scope = canonicalClientSettingScope(settingName.slice(settingSeparator + 1), 2);
        return clientSettingsKeyContract.workspaceTuplePrefixes.has(prefix)
          && scope !== null
          && clientSettingsKeyContract.workspaceTimeframes.has(scope[1]);
      }
      if (key.startsWith("aef:instrument:")) {
        const instrumentSetting = key.slice("aef:instrument:".length);
        for (const settingName of clientSettingsKeyContract.instrumentScalarNames) {
          const suffix = `:${settingName}`;
          if (
            instrumentSetting.endsWith(suffix)
            && canonicalClientSettingScope(instrumentSetting.slice(0, -suffix.length), 1)
          ) return true;
        }
        const indicatorSeparator = instrumentSetting.lastIndexOf(":indicator:");
        if (indicatorSeparator < 1) return false;
        const scope = canonicalClientSettingScope(
          instrumentSetting.slice(0, indicatorSeparator),
          2,
        );
        const indicatorKey = instrumentSetting.slice(
          indicatorSeparator + ":indicator:".length,
        );
        return scope !== null
          && clientSettingsKeyContract.indicatorModes.has(scope[1])
          && clientSettingsKeyContract.indicatorKeys.instrument.has(indicatorKey);
      }
      if (key.startsWith("aef:indicator:global:")) {
        return clientSettingsKeyContract.indicatorKeys.global.has(
          key.slice("aef:indicator:global:".length),
        );
      }
      return false;
    }

    function isBrowserWritableClientSettingKey(key) {
      if (typeof key !== "string") return false;
      if (clientSettingsKeyContract.currentScalarKeys.has(key)) {
        return clientSettingsKeyContract.browserScalarKeys.has(key);
      }
      return isCurrentClientSettingKey(key);
    }

    function serverSettingValue(key) {
      if (!isCurrentClientSettingKey(key)) return null;
      const value = serverSettings[key];
      return value === undefined ? null : value;
    }

    function setServerSettingValue(key, value) {
      const storageKey = String(key || "");
      if (!isBrowserWritableClientSettingKey(storageKey)) return false;
      const normalized = String(value);
      if (
        serverSettings[storageKey] === normalized
        && serverSettingQueue.get(storageKey)?.value === normalized
      ) return true;
      if (serverSettings[storageKey] === normalized && !serverSettingQueue.has(storageKey)) {
        return true;
      }
      return queueServerSetting(storageKey, normalized) !== null;
    }

    function pruneLocalStorageForQuota() {
      let removed = 0;
      for (const key of localStorageKeys()) {
        if (!isPrunableLocalStorageKey(key)) continue;
        try {
          localStorage.removeItem(key);
          removed += 1;
        } catch (_) {
          // Keep saving resilient if storage becomes unavailable mid-cleanup.
        }
      }
      return removed;
    }

    function rawLocalStorageSetItem(key, value) {
      try {
        nativeLocalStorageSetItem(key, value);
        return true;
      } catch (error) {
        if (!isStorageQuotaError(error)) throw error;
        const removed = pruneLocalStorageForQuota();
        try {
          nativeLocalStorageSetItem(key, value);
          if (window.mcDebugStep) window.mcDebugStep("storage quota", `pruned ${removed} cache keys`);
          return true;
        } catch (retryError) {
          if (isPrunableLocalStorageKey(key)) return false;
          console.warn("localStorage save skipped after quota cleanup", retryError);
          if (typeof showRuntimeError === "function") {
            showRuntimeError(new Error("Storage quota is full; pruned transient cache but the last setting could not be saved."), "storage");
          }
          return false;
        }
      }
    }

    function observeServerSettingMutationChangedAtMs(value) {
      const observed = Number(value);
      if (!Number.isSafeInteger(observed) || observed < 0) {
        throw new TypeError("SETTINGS_MUTATION_HIGH_WATER_INVALID");
      }
      serverSettingChangedAtMs = Math.max(serverSettingChangedAtMs, observed);
      if (!rawLocalStorageSetItem(
        serverSettingWriterHighWaterKey,
        String(serverSettingChangedAtMs),
      )) {
        throw new Error("SETTINGS_MUTATION_HIGH_WATER_UNAVAILABLE");
      }
      for (const key of localStorageKeys()) {
        if (
          key === serverSettingWriterHighWaterKey
          || !key.startsWith(SERVER_SETTING_MUTATION_HIGH_WATER_PREFIX)
        ) continue;
        const candidate = Number(localStorage.getItem(key));
        if (Number.isSafeInteger(candidate) && candidate <= observed) {
          try {
            localStorage.removeItem(key);
          } catch (_) {
            // Retaining an acknowledged high-water ticket is safe.
          }
        }
      }
      return serverSettingChangedAtMs;
    }

    function applyServerSettingReconciliation(plan) {
      for (const record of plan.authoritativeRecords) {
        serverSettingAuthoritativeOrders.set(record.key, record);
      }
      for (const key of plan.deletedAuthoritativeKeys) {
        serverSettingAuthoritativeOrders.delete(key);
      }
      for (const record of plan.appliedRecords) {
        serverSettings[record.key] = record.value;
        serverSettingQueue.delete(record.key);
        serverSettingLatestMutations.delete(record.key);
        serverSettingFailures.delete(record.key);
        if (/^aef:workspace:[1-4]:/.test(record.key)) {
          sessionStorage.setItem(record.key, record.value);
          sessionStorage.removeItem(`${record.key}:dirty`);
        }
      }
      for (const key of plan.deletedSettingKeys) {
        delete serverSettings[key];
        serverSettingLatestMutations.delete(key);
        if (/^aef:workspace:[1-4]:/.test(key)) {
          sessionStorage.removeItem(key);
          sessionStorage.removeItem(`${key}:dirty`);
        }
      }
      return plan.changed;
    }

    function hydrateServerSettingMutationSnapshot(
      settings,
      mutations,
      changedAtMs,
      settingsRevision,
      options = {},
    ) {
      if (
        !settings
        || typeof settings !== "object"
        || Array.isArray(settings)
        || !Array.isArray(mutations)
        || !Number.isSafeInteger(changedAtMs)
        || changedAtMs < 0
        || !Number.isSafeInteger(settingsRevision)
        || settingsRevision < 0
      ) throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
      const settingKeys = Object.keys(settings);
      if (mutations.length !== settingKeys.length) {
        throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
      }
      const seen = new Set();
      const normalizedSettings = {};
      const normalizedRecords = [];
      let observedHighWater = 0;
      for (const rawRecord of mutations) {
        const record = normalizeServerSettingMutationRecord(rawRecord);
        const value = settings[record.key];
        if (
          !Object.prototype.hasOwnProperty.call(settings, record.key)
          || seen.has(record.key)
          || !isCurrentClientSettingKey(record.key)
          || (record.writer_id !== null && typeof value !== "string")
        ) {
          throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
        }
        seen.add(record.key);
        observedHighWater = Math.max(observedHighWater, record.changed_at_ms);
        normalizedSettings[record.key] = value;
        normalizedRecords.push({ ...record, value });
      }
      if (seen.size !== settingKeys.length || observedHighWater !== changedAtMs) {
        throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
      }
      const reconcile = options.reconcile === true;
      const priorAuthoritativeRevision = serverSettingAuthoritativeRevision;
      const snapshotCanDelete = settingsRevision >= priorAuthoritativeRevision;
      const snapshotKeys = new Set(settingKeys);
      if (
        settingsRevision > 0
        && settingsRevision === serverSettingCompleteSnapshotRevision
        && (
          snapshotKeys.size !== serverSettingCompleteSnapshotKeys.size
          || Array.from(snapshotKeys).some(key => !serverSettingCompleteSnapshotKeys.has(key))
        )
      ) throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
      if (settingsRevision < serverSettingCompleteSnapshotRevision) {
        return {
          changed: false,
          stale: true,
          settings: normalizedSettings,
          mutations: normalizedRecords.map(({ value: _value, ...record }) => record),
          settings_mutation_changed_at_ms: changedAtMs,
          settings_revision: settingsRevision,
        };
      }
      for (const record of normalizedRecords) {
        const currentAuthoritative = serverSettingAuthoritativeOrders.get(record.key);
        if (
          currentAuthoritative?.settings_revision === settingsRevision
          && (
            currentAuthoritative.changed_at_ms !== record.changed_at_ms
            || currentAuthoritative.writer_id !== record.writer_id
            || currentAuthoritative.sequence !== record.sequence
            || JSON.stringify(currentAuthoritative.value) !== JSON.stringify(record.value)
          )
        ) throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
        const latest = serverSettingLatestMutations.get(record.key);
        if (latest) {
          const latestOrder = {
            changed_at_ms: latest.changed_at_ms,
            writer_id: serverSettingWriterId,
            sequence: latest.sequence,
          };
          if (
            !serverSettingMutationOrderIsNewer(latestOrder, record)
            && !serverSettingMutationOrderIsNewer(record, latestOrder)
            && latest.value !== record.value
          ) throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
        }
      }
      for (const [key, currentAuthoritative] of serverSettingAuthoritativeOrders) {
        if (
          currentAuthoritative?.settings_revision === settingsRevision
          && !snapshotKeys.has(key)
        ) throw new TypeError("SETTINGS_MUTATION_SNAPSHOT_INVALID");
      }
      observeServerSettingMutationChangedAtMs(changedAtMs);

      const reconciliation = reduceServerSettingReconciliation({
        records: normalizedRecords,
        settings: serverSettings,
        authoritativeOrders: serverSettingAuthoritativeOrders,
        latestMutations: serverSettingLatestMutations,
        queuedKeys: new Set(serverSettingQueue.keys()),
        serverSettingKeys: new Set(
          Object.keys(serverSettings).filter(isCurrentClientSettingKey),
        ),
        settingsRevision,
        writerId: serverSettingWriterId,
        snapshotKeys: snapshotCanDelete ? snapshotKeys : null,
        invalidCode: "SETTINGS_MUTATION_SNAPSHOT_INVALID",
      });
      const reconciled = applyServerSettingReconciliation(reconciliation);
      serverSettingAuthoritativeRevision = Math.max(
        priorAuthoritativeRevision,
        settingsRevision,
      );
      if (settingsRevision > serverSettingCompleteSnapshotRevision) {
        serverSettingCompleteSnapshotRevision = settingsRevision;
        serverSettingCompleteSnapshotKeys = new Set(snapshotKeys);
      }
      if (reconcile && reconciled && typeof applyServerSettings === "function") {
        applyServerSettings(serverSettings, {
          preserveNavigation: true,
          applyVisuals: true,
        });
      }
      return {
        changed: reconciled,
        stale: false,
        settings: normalizedSettings,
        mutations: normalizedRecords.map(({ value: _value, ...record }) => record),
        settings_mutation_changed_at_ms: changedAtMs,
        settings_revision: settingsRevision,
      };
    }

    function applyServerSettingMutationBroadcast(mutations, settingsRevision) {
      if (
        !Array.isArray(mutations)
        || !mutations.length
        || !Number.isSafeInteger(settingsRevision)
        || settingsRevision <= 0
      ) {
        throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
      }
      const seen = new Set();
      const normalizedRecords = [];
      for (const rawRecord of mutations) {
        const record = normalizeServerSettingMutationRecord(rawRecord, { includeValue: true });
        if (
          seen.has(record.key)
          || record.writer_id === null
          || !isCurrentClientSettingKey(record.key)
        ) {
          throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
        }
        seen.add(record.key);
        normalizedRecords.push(record);
      }
      if (settingsRevision < serverSettingCompleteSnapshotRevision) return false;
      if (
        settingsRevision === serverSettingCompleteSnapshotRevision
        && normalizedRecords.some(record => !serverSettingCompleteSnapshotKeys.has(record.key))
      ) throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
      for (const record of normalizedRecords) {
        const currentAuthoritative = serverSettingAuthoritativeOrders.get(record.key);
        if (
          currentAuthoritative?.settings_revision === settingsRevision
          && (
            currentAuthoritative.changed_at_ms !== record.changed_at_ms
            || currentAuthoritative.writer_id !== record.writer_id
            || currentAuthoritative.sequence !== record.sequence
            || currentAuthoritative.value !== record.value
          )
        ) throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
        if (
          currentAuthoritative
          && !serverSettingMutationOrderIsNewer(record, currentAuthoritative)
          && !serverSettingMutationOrderIsNewer(currentAuthoritative, record)
          && currentAuthoritative.value !== record.value
        ) throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
      }

      observeServerSettingMutationChangedAtMs(Math.max(
        ...normalizedRecords.map(record => record.changed_at_ms),
      ));
      const reconciliation = reduceServerSettingReconciliation({
        records: normalizedRecords,
        settings: serverSettings,
        authoritativeOrders: serverSettingAuthoritativeOrders,
        latestMutations: serverSettingLatestMutations,
        queuedKeys: new Set(serverSettingQueue.keys()),
        serverSettingKeys: new Set(),
        settingsRevision,
        writerId: serverSettingWriterId,
        snapshotKeys: null,
        invalidCode: "SETTINGS_MUTATION_BROADCAST_INVALID",
      });
      const reconciled = applyServerSettingReconciliation(reconciliation);
      serverSettingAuthoritativeRevision = Math.max(
        serverSettingAuthoritativeRevision,
        settingsRevision,
      );
      if (reconciled && typeof applyServerSettings === "function") {
        applyServerSettings(serverSettings, {
          preserveNavigation: true,
          applyVisuals: true,
        });
      }
      return reconciled;
    }

    function publishServerSettingMutations(mutations, options = {}) {
      if (options.snapshot === true) {
        serverSettingSyncChannel.postMessage({
          type: "settings_snapshot_reconciled",
          settings: options.settings,
          settings_mutations: mutations,
          settings_mutation_changed_at_ms: options.settingsMutationChangedAtMs,
          settings_revision: options.settingsRevision,
        });
        return;
      }
      serverSettingSyncChannel.postMessage({
        type: "settings_mutations_committed",
        settings_revision: options.settingsRevision,
        mutations: mutations.map(record => ({
          key: record.key,
          value: record.value,
          changed_at_ms: record.changed_at_ms,
          writer_id: record.writer_id,
          sequence: record.sequence,
        })),
      });
    }

    serverSettingSyncChannel.onmessage = event => {
      try {
        const message = event?.data;
        if (message?.type === "settings_mutations_committed") {
          if (
            !message
            || typeof message !== "object"
            || Array.isArray(message)
            || Object.keys(message).sort().join(",") !== "mutations,settings_revision,type"
          ) throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
          applyServerSettingMutationBroadcast(message.mutations, message.settings_revision);
        } else if (message?.type === "settings_snapshot_reconciled") {
          if (
            !message
            || typeof message !== "object"
            || Array.isArray(message)
            || Object.keys(message).sort().join(",")
              !== "settings,settings_mutation_changed_at_ms,settings_mutations,settings_revision,type"
          ) throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
          hydrateServerSettingMutationSnapshot(
            message.settings,
            message.settings_mutations,
            message.settings_mutation_changed_at_ms,
            message.settings_revision,
            { reconcile: true },
          );
        } else {
          throw new TypeError("SETTINGS_MUTATION_BROADCAST_INVALID");
        }
      } catch (error) {
        setServerSettingsSaveState(
          "error",
          requestErrorMessage(error, "settings mutation broadcast failed"),
        );
      }
    };

    function flushServerSettings() {
      window.clearTimeout(serverSettingFlushTimer);
      serverSettingFlushTimer = null;
      if (!serverSettingQueue.size) {
        if (!serverSettingFlushRequests.size) setServerSettingsSaveState("saved");
        return Promise.all(Array.from(serverSettingFlushRequests)).then(
          results => results.length ? results[results.length - 1] : { ok: true },
        );
      }
      const mutations = Array.from(
        serverSettingQueue.entries(),
        ([key, mutation]) => ({ key, ...mutation }),
      );
      serverSettingQueue.clear();
      setServerSettingsSaveState("saving");
      if (window.mcTelemetryInc) window.mcTelemetryInc("settings.flush");
      if (window.mcTelemetrySet) {
        window.mcTelemetrySet(
          "settings.last_flush_keys",
          mutations.map(mutation => mutation.key).sort().join(","),
        );
      }
      let succeeded = false;
      let retryDelay = SERVER_SETTING_RETRY_MS;
      const request = fetchJson("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          writer_id: serverSettingWriterId,
          mutations,
        }),
        keepalive: true,
        telemetryReason: "settings_flush",
      })
        .then(payload => {
          if (payload?.ok === false) {
            const error = new Error(apiErrorMessage(payload, "server settings save failed"));
            error.payload = normalizeErrorPayload(payload) || payload;
            throw error;
          }
          const responseMutations = payload?.mutations;
          if (
            !payload
            || typeof payload !== "object"
            || Array.isArray(payload)
            || Object.keys(payload).sort().join(",")
              !== "count,mutations,ok,settings_revision"
            || payload.ok !== true
            || !Number.isSafeInteger(payload?.count)
            || payload.count < 0
            || !Number.isSafeInteger(payload?.settings_revision)
            || payload.settings_revision <= 0
            || !Array.isArray(responseMutations)
            || responseMutations.length !== mutations.length
          ) {
            const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
            error.payload = { error: { retryable: false } };
            throw error;
          }
          const sentByKey = new Map(mutations.map(mutation => [mutation.key, mutation]));
          const acknowledgedKeys = new Set();
          const admittedRecords = [];
          let appliedCount = 0;
          for (const result of responseMutations) {
            const resultKeys = result && typeof result === "object"
              ? Object.keys(result).sort().join(",")
              : "";
            const key = result?.key;
            const sent = sentByKey.get(key);
            if (
              resultKeys !== "changed_at_ms,key,outcome,sequence,value,writer_id"
              || typeof key !== "string"
              || !sent
              || acknowledgedKeys.has(key)
              || !["applied", "replayed", "rejected"].includes(result.outcome)
              || typeof result.value !== "string"
              || !Number.isSafeInteger(result.changed_at_ms)
              || result.changed_at_ms <= 0
              || !Number.isSafeInteger(result.sequence)
              || result.sequence <= 0
              || !SERVER_SETTING_WRITER_ID_PATTERN.test(String(result.writer_id || ""))
            ) {
              const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
              error.payload = { error: { retryable: false } };
              throw error;
            }
            acknowledgedKeys.add(key);
            const authoritativeRecord = {
              key,
              value: result.value,
              changed_at_ms: result.changed_at_ms,
              writer_id: result.writer_id,
              sequence: result.sequence,
            };
            const submittedOrder = {
              changed_at_ms: sent.changed_at_ms,
              writer_id: serverSettingWriterId,
              sequence: sent.sequence,
            };
            if (["applied", "replayed"].includes(result.outcome)) {
              if (result.outcome === "applied") appliedCount += 1;
              if (
                authoritativeRecord.changed_at_ms !== submittedOrder.changed_at_ms
                || authoritativeRecord.writer_id !== submittedOrder.writer_id
                || authoritativeRecord.sequence !== submittedOrder.sequence
                || authoritativeRecord.value !== sent.value
              ) {
                const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
                error.payload = { error: { retryable: false } };
                throw error;
              }
            } else if (!serverSettingMutationOrderIsNewer(authoritativeRecord, submittedOrder)) {
              const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
              error.payload = { error: { retryable: false } };
              throw error;
            }
            const currentAuthoritative = serverSettingAuthoritativeOrders.get(key);
            if (
              currentAuthoritative?.settings_revision === payload.settings_revision
              && (
                currentAuthoritative.changed_at_ms !== authoritativeRecord.changed_at_ms
                || currentAuthoritative.writer_id !== authoritativeRecord.writer_id
                || currentAuthoritative.sequence !== authoritativeRecord.sequence
                || currentAuthoritative.value !== authoritativeRecord.value
              )
            ) {
              const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
              error.payload = { error: { retryable: false } };
              throw error;
            }
            if (
              currentAuthoritative
              && !serverSettingMutationOrderIsNewer(authoritativeRecord, currentAuthoritative)
              && !serverSettingMutationOrderIsNewer(currentAuthoritative, authoritativeRecord)
              && currentAuthoritative.value !== authoritativeRecord.value
            ) {
              const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
              error.payload = { error: { retryable: false } };
              throw error;
            }
            const effectiveAuthoritative = currentAuthoritative
              && currentAuthoritative.settings_revision > payload.settings_revision
              ? currentAuthoritative
              : authoritativeRecord;
            if (
              result.outcome === "rejected"
              && typeof effectiveAuthoritative.value !== "string"
            ) {
              const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
              error.payload = { error: { retryable: false } };
              throw error;
            }
            admittedRecords.push({ result, sent, authoritativeRecord, effectiveAuthoritative });
          }
          if (acknowledgedKeys.size !== mutations.length || appliedCount !== payload.count) {
            const error = new Error("SETTINGS_MUTATION_ACK_INVALID");
            error.payload = { error: { retryable: false } };
            throw error;
          }
          observeServerSettingMutationChangedAtMs(Math.max(
            ...admittedRecords.map(({ authoritativeRecord }) => (
              authoritativeRecord.changed_at_ms
            )),
          ));

          let reconciled = false;
          for (const {
            result,
            sent,
            authoritativeRecord,
            effectiveAuthoritative,
          } of admittedRecords) {
            const key = authoritativeRecord.key;
            const currentAuthoritative = serverSettingAuthoritativeOrders.get(key);
            if (
              !currentAuthoritative
              || Number(currentAuthoritative.settings_revision || 0)
                <= payload.settings_revision
            ) {
              serverSettingAuthoritativeOrders.set(key, {
                ...authoritativeRecord,
                settings_revision: payload.settings_revision,
              });
            }
            const latest = serverSettingLatestMutations.get(key);
            const latestMatchesSent = latest?.changed_at_ms === sent.changed_at_ms
              && latest?.sequence === sent.sequence
              && latest?.value === sent.value;
            if (!latestMatchesSent) continue;
            serverSettingFailures.delete(key);
            serverSettingLatestMutations.delete(key);
            if (result.outcome === "rejected") {
              if (serverSettings[key] !== effectiveAuthoritative.value) {
                serverSettings[key] = effectiveAuthoritative.value;
                reconciled = true;
              }
              if (/^aef:workspace:[1-4]:/.test(key)) {
                sessionStorage.setItem(key, effectiveAuthoritative.value);
                sessionStorage.removeItem(`${key}:dirty`);
              }
            }
          }
          serverSettingAuthoritativeRevision = Math.max(
            serverSettingAuthoritativeRevision,
            payload.settings_revision,
          );
          publishServerSettingMutations(responseMutations, {
            settingsRevision: payload.settings_revision,
          });
          if (reconciled && typeof applyServerSettings === "function") {
            applyServerSettings(serverSettings, {
              applyVisuals: true,
            });
          }
          succeeded = true;
          return payload;
        })
        .catch(error => {
          const errorPayload = normalizeErrorPayload(error?.payload);
          const retryable = errorPayload?.error?.retryable !== false;
          let hasRetryWork = false;
          let reconciled = false;
          const message = requestErrorMessage(error, "server settings save failed");
          if (retryable) {
            for (const mutation of mutations) {
              if (serverSettingQueue.has(mutation.key)) {
                hasRetryWork = true;
              } else {
                const latest = serverSettingLatestMutations.get(mutation.key);
                if (
                  latest?.changed_at_ms !== mutation.changed_at_ms
                  || latest?.sequence !== mutation.sequence
                  || latest?.value !== mutation.value
                ) continue;
                const { key, ...queuedMutation } = mutation;
                serverSettingQueue.set(key, queuedMutation);
                hasRetryWork = true;
              }
            }
          } else {
            retryDelay = 0;
            for (const mutation of mutations) {
              const latest = serverSettingLatestMutations.get(mutation.key);
              if (
                latest?.changed_at_ms === mutation.changed_at_ms
                && latest?.sequence === mutation.sequence
                && latest?.value === mutation.value
              ) {
                serverSettingFailures.set(mutation.key, message);
                serverSettingLatestMutations.delete(mutation.key);
                const authoritative = serverSettingAuthoritativeOrders.get(mutation.key);
                if (typeof authoritative?.value === "string") {
                  if (serverSettings[mutation.key] !== authoritative.value) {
                    serverSettings[mutation.key] = authoritative.value;
                    reconciled = true;
                  }
                } else if (Object.prototype.hasOwnProperty.call(serverSettings, mutation.key)) {
                  delete serverSettings[mutation.key];
                  reconciled = true;
                }
              }
            }
          }
          if (retryable && !hasRetryWork) {
            succeeded = true;
            return { ok: true, superseded: true };
          }
          setServerSettingsSaveState("error", message);
          if (reconciled && typeof applyServerSettings === "function") {
            applyServerSettings(serverSettings, { applyVisuals: true });
          }
          console.warn("server settings save failed", error);
          return { ok: false, error: message };
        })
        .finally(() => {
          serverSettingFlushRequests.delete(request);
          if (!serverSettingQueue.size) {
            if (succeeded && !serverSettingFlushRequests.size) {
              setServerSettingsSaveState("saved");
            } else {
              setServerSettingsSaveState(
                serverSettingsSaveState.phase,
                serverSettingsSaveState.error,
              );
            }
            return;
          }
          if (succeeded) setServerSettingsSaveState("saving");
          window.clearTimeout(serverSettingFlushTimer);
          serverSettingFlushTimer = window.setTimeout(
            flushServerSettings,
            succeeded ? 0 : retryDelay,
          );
        });
      serverSettingFlushRequests.add(request);
      setServerSettingsSaveState("saving");
      return request;
    }

    function queueServerSetting(key, value) {
      if (!isBrowserWritableClientSettingKey(key)) return null;
      const storageKey = String(key);
      const normalized = String(value);
      if (
        serverSettings[storageKey] === normalized
        && serverSettingQueue.get(storageKey)?.value === normalized
      ) return serverSettingQueue.get(storageKey);
      serverSettingFailures.delete(storageKey);
      try {
        const sharedHighWater = serverSettingMutationHighWater();
        serverSettingWriterSequence += 1;
        serverSettingChangedAtMs = Math.max(
          serverSettingChangedAtMs,
          sharedHighWater,
          Math.trunc(performance.timeOrigin + performance.now()),
        ) + 1;
        if (
          !Number.isSafeInteger(serverSettingWriterSequence)
          || !Number.isSafeInteger(serverSettingChangedAtMs)
        ) {
          throw new RangeError("SETTINGS_MUTATION_ORDER_EXHAUSTED");
        }
        if (!rawLocalStorageSetItem(
          serverSettingWriterHighWaterKey,
          String(serverSettingChangedAtMs),
        )) {
          throw new Error("SETTINGS_MUTATION_HIGH_WATER_UNAVAILABLE");
        }
        const mutation = {
          value: normalized,
          changed_at_ms: serverSettingChangedAtMs,
          sequence: serverSettingWriterSequence,
        };
        serverSettings[storageKey] = normalized;
        serverSettingLatestMutations.set(storageKey, mutation);
        serverSettingQueue.set(storageKey, mutation);
        window.clearTimeout(serverSettingFlushTimer);
        serverSettingFlushTimer = window.setTimeout(flushServerSettings, 350);
        setServerSettingsSaveState("saving");
        return mutation;
      } catch (error) {
        const message = requestErrorMessage(error, "settings mutation ordering failed");
        serverSettingFailures.set(storageKey, message);
        setServerSettingsSaveState("error", message);
        return null;
      }
    }
