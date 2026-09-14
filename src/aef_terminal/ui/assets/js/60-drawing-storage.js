    const DRAWING_HISTORY_LIMIT = 50;
    const drawingHistoryState = {
      scopeKey: "",
      undo: [],
      redo: [],
      applying: false,
    };

    function drawingHistoryScopeKey() {
      return scopedObjectSignature(
        state.instrumentId,
        state.timeframe,
        instrumentRouteFingerprint(),
      );
    }

    function cloneDrawingHistoryObjects(drawings = state.drawing.objects) {
      return JSON.parse(JSON.stringify(Array.isArray(drawings) ? drawings : []));
    }

    function ensureDrawingHistoryScope() {
      const scopeKey = drawingHistoryScopeKey();
      if (drawingHistoryState.scopeKey !== scopeKey) {
        drawingHistoryState.scopeKey = scopeKey;
        drawingHistoryState.undo = [];
        drawingHistoryState.redo = [];
        drawingHistoryState.applying = false;
      }
      return scopeKey;
    }

    function resetDrawingHistory() {
      drawingHistoryState.scopeKey = drawingHistoryScopeKey();
      drawingHistoryState.undo = [];
      drawingHistoryState.redo = [];
      drawingHistoryState.applying = false;
    }

    function captureDrawingHistoryState() {
      const scopeKey = ensureDrawingHistoryScope();
      return {
        scopeKey,
        objects: cloneDrawingHistoryObjects(),
        selectedId: state.drawing.selectedId || null,
      };
    }

    function drawingHistoryOwnsField(key) {
      const field = String(key || "");
      return ![
        "anchorResolution",
        "anchorProjection",
        "geometryIntegrity",
        "channelIntegrity",
        "channelCompromised",
      ].includes(field);
    }

    function drawingHistoryDefinitionSignature(drawings = state.drawing.objects) {
      const projected = cloneDrawingHistoryObjects(drawings).map(object => {
        if (!object || typeof object !== "object") return object;
        for (const key of Object.keys(object)) {
          if (!drawingHistoryOwnsField(key)) delete object[key];
        }
        return object;
      });
      return sharedObjectSignature(projected);
    }

    function drawingPatchIsHistoryEligible(patch) {
      const keys = patch && typeof patch === "object" ? Object.keys(patch) : [];
      return Boolean(keys.length && keys.every(drawingHistoryOwnsField));
    }

    function recordDrawingHistory(before, after, label = "Drawing change") {
      if (drawingHistoryState.applying || !before || before.scopeKey !== ensureDrawingHistoryScope()) return false;
      if (sharedObjectSignature(before.objects) === sharedObjectSignature(after.objects)) return false;
      drawingHistoryState.undo.push({
        scopeKey: before.scopeKey,
        label: String(label || "Drawing change"),
        before: {
          scopeKey: before.scopeKey,
          objects: cloneDrawingHistoryObjects(before.objects),
          selectedId: before.selectedId || null,
        },
        after: {
          scopeKey: after.scopeKey,
          objects: cloneDrawingHistoryObjects(after.objects),
          selectedId: after.selectedId || null,
        },
      });
      if (drawingHistoryState.undo.length > DRAWING_HISTORY_LIMIT) {
        drawingHistoryState.undo.splice(0, drawingHistoryState.undo.length - DRAWING_HISTORY_LIMIT);
      }
      drawingHistoryState.redo = [];
      return true;
    }

    function drawingHistoryRestoreObjects(targetObjects) {
      const currentById = new Map(
        (state.drawing.objects || [])
          .filter(item => item && typeof item === "object" && item.id)
          .map(item => [String(item.id), item]),
      );
      const restored = cloneDrawingHistoryObjects(targetObjects);
      for (const target of restored) {
        if (!target || typeof target !== "object") continue;
        const current = currentById.get(String(target.id || "")) || null;
        // A deleted drawing has no live counterpart. Existing drawings retain
        // the current generation-bound runtime projection while geometry rewinds.
        if (!current) continue;
        for (const key of Object.keys(target)) {
          if (!drawingHistoryOwnsField(key)) delete target[key];
        }
        for (const [key, value] of Object.entries(current)) {
          if (!drawingHistoryOwnsField(key)) target[key] = value;
        }
      }
      return restored;
    }

    function syncDrawingHistoryNonOwnedFields(drawings = state.drawing.objects) {
      const currentById = new Map(
        cloneDrawingHistoryObjects(drawings)
          .filter(item => item && typeof item === "object" && item.id)
          .map(item => [String(item.id), item]),
      );
      const syncSnapshot = snapshot => {
        if (!snapshot || !Array.isArray(snapshot.objects)) return;
        for (const target of snapshot.objects) {
          if (!target || typeof target !== "object") continue;
          const current = currentById.get(String(target.id || ""));
          if (!current) continue;
          for (const key of Object.keys(target)) {
            if (!drawingHistoryOwnsField(key)) delete target[key];
          }
          for (const [key, value] of Object.entries(current)) {
            if (!drawingHistoryOwnsField(key)) target[key] = value;
          }
        }
      };
      for (const entry of [...drawingHistoryState.undo, ...drawingHistoryState.redo]) {
        syncSnapshot(entry.before);
        syncSnapshot(entry.after);
      }
    }

    function restoreDrawingHistoryState(target) {
      if (!target || target.scopeKey !== ensureDrawingHistoryScope()) return false;
      if (state.drawing.drag || state.drawing.draft || state.alerts.selectedId || state.optionTargets.selectedId) return false;
      const previousObjects = state.drawing.objects;
      const previousSelectedId = state.drawing.selectedId;
      const previousDeletedIds = { ...(state.drawing.deletedIds || {}) };
      const nextObjects = drawingHistoryRestoreObjects(target.objects);
      const currentIds = new Set((previousObjects || []).map(item => String(item?.id || "")).filter(Boolean));
      const nextIds = new Set(nextObjects.map(item => String(item?.id || "")).filter(Boolean));
      const removedIds = [...currentIds].filter(id => !nextIds.has(id));
      const restoredIds = [...nextIds].filter(id => !currentIds.has(id));
      restoredIds.forEach(id => { delete state.drawing.deletedIds[id]; });
      removedIds.forEach(markDrawingDeleted);
      state.drawing.objects = nextObjects;
      state.drawing.selectedId = target.selectedId && nextIds.has(String(target.selectedId))
        ? String(target.selectedId)
        : null;
      state.drawing.hoverPoint = null;
      drawingHistoryState.applying = true;
      let saved = false;
      try {
        saved = saveDrawings({
          recordHistory: false,
          deletedIds: removedIds,
          allowMassDelete: previousObjects.length > 1 && nextObjects.length === 0,
        });
      } finally {
        drawingHistoryState.applying = false;
      }
      if (!saved) {
        state.drawing.objects = previousObjects;
        state.drawing.selectedId = previousSelectedId;
        state.drawing.deletedIds = previousDeletedIds;
        return false;
      }
      applyDrawingUi();
      requestObjectLayerRedraw("drawing history restored", { force: true });
      return true;
    }

    function undoDrawingMutation() {
      const scopeKey = ensureDrawingHistoryScope();
      const entry = drawingHistoryState.undo.pop();
      if (!entry) return false;
      if (entry.scopeKey !== scopeKey || !restoreDrawingHistoryState(entry.before)) {
        drawingHistoryState.undo.push(entry);
        return false;
      }
      drawingHistoryState.redo.push(entry);
      return true;
    }

    function redoDrawingMutation() {
      const scopeKey = ensureDrawingHistoryScope();
      const entry = drawingHistoryState.redo.pop();
      if (!entry) return false;
      if (entry.scopeKey !== scopeKey || !restoreDrawingHistoryState(entry.after)) {
        drawingHistoryState.redo.push(entry);
        return false;
      }
      drawingHistoryState.undo.push(entry);
      return true;
    }

    function suspiciousDrawingMassShrink(nextCount, options = {}) {
      if (options.allowMassDelete) return false;
      const baseline = Number(state.drawing.persistedCount);
      if (!Number.isFinite(baseline) || baseline <= 0) return false;
      const deletedIds = Array.isArray(options.deletedIds) ? options.deletedIds.filter(Boolean) : [];
      const next = Number(nextCount || 0);
      if (baseline > 1 && next === 0) return true;
      const shrink = baseline - next;
      return shrink > 0 && deletedIds.length < shrink;
    }

    function saveDrawings(options = {}) {
      const normalized = normalizeDrawingObjects(state.drawing.objects);
      if (suspiciousDrawingMassShrink(normalized.length, options)) {
        console.warn("blocked suspicious drawings mass deletion", {
          symbol: state.symbol,
          timeframe: state.timeframe,
          previous: state.drawing.persistedCount,
          next: normalized.length,
        });
        showBrowserToast("Drawing save blocked: too many objects would be removed at once");
        return false;
      }
      resolveDrawingAnchorsAgainstSnapshot(normalized, state.snapshot);
      const scopeKey = ensureDrawingHistoryScope();
      if (options.resetHistory) {
        resetDrawingHistory();
      } else if (options.recordHistory !== false && options.historyBefore) {
        recordDrawingHistory(
          options.historyBefore,
          {
            scopeKey,
            objects: normalized,
            selectedId: state.drawing.selectedId || null,
          },
          options.historyLabel,
        );
      } else if (options.recordHistory === false && !drawingHistoryState.applying) {
        syncDrawingHistoryNonOwnedFields(normalized);
      }
      state.drawing.localEditUntil = Date.now() + DRAWING_LOCAL_EDIT_GRACE_MS;
      state.drawing.objects = normalized;
      touchChartUserObjectsVersion();
      persistDrawingsToServer(options);
      return true;
    }

    function loadDrawingsForCurrent() {
      state.drawing.objects = [];
      state.drawing.draft = null;
      state.drawing.hoverPoint = null;
      state.drawing.selectedId = null;
      resetDrawingHistory();
      loadPriceAlertsForCurrent();
      applyDrawingUi();
      loadDrawingsFromServerForCurrent();
    }
