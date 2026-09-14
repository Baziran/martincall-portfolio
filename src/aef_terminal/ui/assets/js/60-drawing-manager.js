    const DRAWING_MANAGER_TYPES = [
      ["all", "All"],
      ["channel", "Channel"],
      ["line", "Line"],
      ["zone", "Zone"],
      ["fib", "Fib"],
      ["ellipse", "Ellipse"],
      ["text", "Text"],
      ["path", "Plan"],
    ];
    const DRAWING_MANAGER_TYPE_ORDER = new Map(DRAWING_MANAGER_TYPES.map(([type], index) => [type, index]));
    const drawingManagerCheckedIds = new Set();

    function drawingTypeName(type) {
      return type === "line" ? "Line" : type === "zone" ? "Zone" : type === "channel" ? "Channel" : type === "fib" ? "Fib" : type === "text" ? "Text" : type === "path" ? "Plan" : type === "ellipse" ? "Ellipse" : type;
    }

    function drawingManagerType() {
      const current = String(state.drawing.managerType || "all");
      return DRAWING_MANAGER_TYPE_ORDER.has(current) ? current : "all";
    }

    function drawingManagerTypeCount(type) {
      if (type === "all") return state.drawing.objects.length;
      return state.drawing.objects.filter(object => object.type === type).length;
    }

    function drawingManagerSortedObjects() {
      const indexed = state.drawing.objects.map((object, index) => ({ object, index }));
      const activeType = drawingManagerType();
      return indexed
        .filter(item => activeType === "all" || item.object.type === activeType)
        .sort((a, b) => {
          const aRank = DRAWING_MANAGER_TYPE_ORDER.get(a.object.type) ?? 99;
          const bRank = DRAWING_MANAGER_TYPE_ORDER.get(b.object.type) ?? 99;
          return aRank - bRank || a.index - b.index;
        });
    }

    function drawingObjectMeta(object) {
      if (!object) return "";
      const objectReasons = typeof drawingObjectIntegrityReasons === "function" ? drawingObjectIntegrityReasons(object) : [];
      if (objectReasons.length) {
        const label = object.type === "channel" ? "CHECK BAR ANCHORS" : "CHECK OBJECT";
        return `${label} · ${objectReasons.join(", ")}`;
      }
      if (object.type === "channel") {
        const guides = object.showGuides === false ? "edges" : "quarters";
        const color = object.color ? object.color.toUpperCase() : "theme";
        return `${guides} · ghosts ${clamp(Number(object.ghostCopies ?? CHANNEL_GHOST_COPIES_DEFAULT), 0, CHANNEL_GHOST_COPIES_MAX)} · ${color}`;
      }
      if (object.type === "text") return object.text || "Text";
      if (object.type === "path") return `${object.points?.length || 0} pts · ${object.style || "solid"} · ${Number(object.width) || 2}px`;
      if (object.type === "zone") return `${object.label || "Price zone"} · ${object.extendRight ? "extend" : "fixed"}`;
      if (object.type === "fib") return `${object.extendRight === false ? "fixed" : "extend"} · ${object.style || "solid"} · ${Number(object.width) || 1}px`;
      if (object.type === "line" && object.lineVariant === "ruler") {
        return `ruler · fixed · ${object.style || "solid"} · ${Number(object.width) || 1}px`;
      }
      return `${object.extendRight ? "extend" : "fixed"} · ${object.style || "solid"} · ${Number(object.width) || 1}px`;
    }

    function renderDrawingManagerTabs() {
      const tabs = document.getElementById("drawing-object-tabs");
      if (!tabs) return;
      const activeType = drawingManagerType();
      tabs.innerHTML = DRAWING_MANAGER_TYPES.map(([type, label]) => {
        const count = drawingManagerTypeCount(type);
        const active = type === activeType ? "active" : "";
        return `<button class="object-tab ${active}" type="button" data-object-type="${type}">${escapeHtml(label)} <span>${count}</span></button>`;
      }).join("");
      tabs.querySelectorAll("[data-object-type]").forEach(button => {
        button.addEventListener("click", () => {
          state.drawing.managerType = button.dataset.objectType || "all";
          renderDrawingManager();
        });
      });
    }

    function renderDrawingManager() {
      const dialog = document.getElementById("drawing-dialog");
      if (!dialog) return;
      dialog.classList.toggle("open", state.drawing.managerOpen);
      renderDrawingManagerTabs();
      const list = document.getElementById("drawing-object-list");
      if (!list) return;
      const objectIds = new Set(state.drawing.objects.map(object => object.id));
      [...drawingManagerCheckedIds].forEach(id => {
        if (!objectIds.has(id)) drawingManagerCheckedIds.delete(id);
      });
      if (!state.drawing.objects.length) {
        list.innerHTML = `<div class="muted object-empty">No drawing objects on this symbol/timeframe.</div>`;
        return;
      }
      const rows = drawingManagerSortedObjects();
      if (!rows.length) {
        list.innerHTML = `<div class="muted object-empty">No ${escapeHtml(drawingTypeName(drawingManagerType()).toLowerCase())} objects on this symbol/timeframe.</div>`;
        return;
      }
      list.innerHTML = rows.map(({ object, index }) => {
        const active = object.id === state.drawing.selectedId ? "active" : "";
        const compromised = typeof drawingObjectIntegrityReasons === "function" && drawingObjectIntegrityReasons(object).length > 0;
        const rowClass = `object-row ${active}${compromised ? " compromised" : ""}`.trim();
        const objectId = escapeHtml(object.id);
        const objectName = `${index + 1}. ${drawingTypeName(object.type)}`;
        return `<div class="${rowClass}" data-object-id="${objectId}">
          <input type="checkbox" class="object-check" name="drawing-object-${objectId}" value="${objectId}" aria-label="Mark ${escapeHtml(objectName)} for deletion"${drawingManagerCheckedIds.has(object.id) ? " checked" : ""}>
          <button type="button" class="object-select" data-object-select="${objectId}" aria-pressed="${active ? "true" : "false"}" aria-label="${active ? "Deselect" : "Select"} ${escapeHtml(objectName)}">
            <span class="object-name">${escapeHtml(objectName)}</span>
            <span class="object-meta">${escapeHtml(drawingObjectMeta(object))}</span>
          </button>
        </div>`;
      }).join("");
      list.querySelectorAll(".object-check").forEach(input => {
        input.addEventListener("change", () => {
          if (input.checked) drawingManagerCheckedIds.add(input.value);
          else drawingManagerCheckedIds.delete(input.value);
        });
      });
      list.querySelectorAll("[data-object-select]").forEach(button => {
        button.addEventListener("click", () => {
          if (state.drawing.draft?.type === "text") {
            state.drawing.draft = null;
            state.drawing.hoverPoint = null;
          }
          const objectId = button.dataset.objectSelect;
          const nextId = state.drawing.selectedId === objectId ? null : objectId;
          setChartObjectSelection(nextId ? "drawing" : "", nextId);
          applyDrawingUi();
          renderDrawingOverlay();
        });
      });
    }

    function openDrawingManager(options = {}) {
      if (!state.drawing.managerOpen) drawingManagerCheckedIds.clear();
      state.drawing.managerOpen = true;
      const selected = selectedDrawing();
      const textDraft = state.drawing.draft?.type === "text" ? state.drawing.draft : null;
      const focusedObject = textDraft || selected;
      if (options.focusSelectedType && focusedObject?.type && DRAWING_MANAGER_TYPE_ORDER.has(focusedObject.type)) {
        state.drawing.managerType = focusedObject.type;
      } else {
        state.drawing.managerType = drawingManagerType();
      }
      renderDrawingManager();
      applyDrawingUi();
      requestAnimationFrame(() => {
        clampFloatingPanelToViewport("drawing-dialog");
        if (focusedObject?.type !== "text") return;
        const input = document.getElementById("object-text-input");
        input?.focus();
        if (textDraft) input?.select();
      });
    }

    function closeDrawingManager() {
      const dialog = document.getElementById("drawing-dialog");
      if (dialog?.contains(document.activeElement)) document.activeElement?.blur?.();
      const discardedTextDraft = state.drawing.draft?.type === "text";
      if (discardedTextDraft) {
        state.drawing.draft = null;
        state.drawing.hoverPoint = null;
      }
      state.drawing.managerOpen = false;
      drawingManagerCheckedIds.clear();
      renderDrawingManager();
      if (discardedTextDraft) {
        applyDrawingUi();
        renderInteractionOverlay({ immediate: true });
      }
    }

    function deleteCheckedDrawings() {
      const checked = [...drawingManagerCheckedIds];
      if (!checked.length) return;
      const historyBefore = captureDrawingHistoryState();
      const remove = new Set(checked);
      checked.forEach(id => {
        if (typeof markDrawingDeleted === "function") markDrawingDeleted(id);
        clearOptionTargetRuntime(id);
      });
      state.drawing.objects = state.drawing.objects.filter(object => !remove.has(object.id));
      if (remove.has(state.drawing.selectedId)) state.drawing.selectedId = null;
      state.drawing.draft = null;
      state.drawing.hoverPoint = null;
      drawingManagerCheckedIds.clear();
      saveDrawings({
        historyBefore,
        historyLabel: "Delete drawings",
        deletedIds: checked,
      });
      applyDrawingUi();
      renderDrawingOverlay();
    }

    function deleteAllDrawings() {
      if (!state.drawing.objects.length && !state.drawing.draft) return;
      if (!window.confirm("Delete all drawing objects for this symbol and timeframe?")) return;
      const historyBefore = captureDrawingHistoryState();
      state.drawing.objects.forEach(object => {
        if (typeof markDrawingDeleted === "function") markDrawingDeleted(object.id);
        clearOptionTargetRuntime(object.id);
      });
      state.drawing.objects = [];
      state.drawing.draft = null;
      state.drawing.hoverPoint = null;
      state.drawing.selectedId = null;
      drawingManagerCheckedIds.clear();
      saveDrawings({
        historyBefore,
        historyLabel: "Delete all drawings",
        allowMassDelete: true,
      });
      applyDrawingUi();
      requestObjectLayerRedraw("drawings cleared", { force: true });
    }
