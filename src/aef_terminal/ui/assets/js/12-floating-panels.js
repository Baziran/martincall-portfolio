    const floatingPanelControllers = new Map();
    const FLOATING_PANEL_MARGIN = 8;
    const FLOATING_PANEL_INTERACTIVE_SELECTOR = "button, input, select, textarea, a, [contenteditable='true'], [role='button']";

    function floatingPanelPosition(surface, left, top, margin = FLOATING_PANEL_MARGIN) {
      const width = surface.offsetWidth || surface.getBoundingClientRect().width || 0;
      const height = surface.offsetHeight || surface.getBoundingClientRect().height || 0;
      const maxLeft = Math.max(margin, window.innerWidth - width - margin);
      const maxTop = Math.max(margin, window.innerHeight - height - margin);
      return {
        left: Math.max(margin, Math.min(maxLeft, left)),
        top: Math.max(margin, Math.min(maxTop, top)),
      };
    }

    function clampFloatingPanelToViewport(key) {
      const controller = floatingPanelControllers.get(key);
      if (!controller || controller.surface.dataset.floatingPositioned !== "1") return;
      const rect = controller.surface.getBoundingClientRect();
      const position = floatingPanelPosition(controller.surface, rect.left, rect.top, controller.margin);
      controller.surface.style.left = `${position.left}px`;
      controller.surface.style.top = `${position.top}px`;
    }

    function setupFloatingPanelDrag(key, options = {}) {
      if (floatingPanelControllers.has(key)) return floatingPanelControllers.get(key);
      const root = options.root || document.getElementById(options.rootId || key);
      const surface = options.surface || root?.querySelector(options.surfaceSelector || ".modal-card") || root;
      const handle = options.handle || root?.querySelector(options.handleSelector || ".modal-head");
      if (!root || !surface || !handle) return null;

      const controller = {
        root,
        surface,
        handle,
        margin: Math.max(0, Number(options.margin) || FLOATING_PANEL_MARGIN),
        drag: null,
      };
      floatingPanelControllers.set(key, controller);

      const finishDrag = event => {
        const drag = controller.drag;
        if (!drag || (event?.pointerId != null && event.pointerId !== drag.pointerId)) return;
        controller.drag = null;
        surface.classList.remove("floating-panel-dragging");
        if (handle.hasPointerCapture?.(drag.pointerId)) handle.releasePointerCapture(drag.pointerId);
      };

      handle.addEventListener("pointerdown", event => {
        if (event.button !== 0 || event.isPrimary === false) return;
        if (event.target?.closest?.(FLOATING_PANEL_INTERACTIVE_SELECTOR)) return;
        const rect = surface.getBoundingClientRect();
        surface.style.position = "fixed";
        surface.style.left = `${rect.left}px`;
        surface.style.top = `${rect.top}px`;
        surface.style.right = "auto";
        surface.style.bottom = "auto";
        surface.dataset.floatingPositioned = "1";
        controller.drag = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          startLeft: rect.left,
          startTop: rect.top,
        };
        surface.classList.add("floating-panel-dragging");
        handle.setPointerCapture?.(event.pointerId);
        event.preventDefault();
      });

      handle.addEventListener("pointermove", event => {
        const drag = controller.drag;
        if (!drag || event.pointerId !== drag.pointerId) return;
        const position = floatingPanelPosition(
          surface,
          drag.startLeft + event.clientX - drag.startX,
          drag.startTop + event.clientY - drag.startY,
          controller.margin,
        );
        surface.style.left = `${position.left}px`;
        surface.style.top = `${position.top}px`;
      });
      handle.addEventListener("pointerup", finishDrag);
      handle.addEventListener("pointercancel", finishDrag);
      handle.addEventListener("lostpointercapture", finishDrag);

      const reclamp = () => {
        if (root.classList.contains(options.openClass || "open")) clampFloatingPanelToViewport(key);
      };
      window.addEventListener("resize", reclamp);
      if (typeof ResizeObserver === "function") new ResizeObserver(reclamp).observe(surface);
      return controller;
    }
