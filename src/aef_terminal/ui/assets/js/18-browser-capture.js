    const BROWSER_CAPTURE_PROTOCOL_PREFIX = "MCAP1:";
    const BROWSER_CAPTURE_SCOPES = new Set(["price", "terminal"]);
    const BROWSER_CAPTURE_REQUEST_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    const BROWSER_CAPTURE_MAX_PNG_BYTES = 9_200_000;
    const BROWSER_CAPTURE_MAX_PIXELS = 7_000_000;
    const BROWSER_CAPTURE_MAX_DIMENSION = 3200;
    const BROWSER_CAPTURE_MAX_DOM_NODES = 12_000;
    const BROWSER_CAPTURE_MAX_CANVAS_GROUPS = 12;
    const BROWSER_CAPTURE_MAX_EMBEDDED_PNG_BYTES = 9_200_000;
    const BROWSER_CAPTURE_MAX_SVG_CHARS = 18_000_000;
    const BROWSER_CAPTURE_TOTAL_TIMEOUT_MS = 12_000;
    const BROWSER_CAPTURE_SVG_RENDER_TIMEOUT_MS = 4_500;
    const BROWSER_CAPTURE_PNG_ENCODING_TIMEOUT_MS = 3_500;
    const BROWSER_CAPTURE_CANVAS_ENCODING_TIMEOUT_MS = 3_000;
    const BROWSER_CAPTURE_DATA_URL_TIMEOUT_MS = 2_000;
    const BROWSER_CAPTURE_STATE_INTERVAL_MS = 15_000;
    const BROWSER_CAPTURE_RECONNECT_MS = 2_000;
    const BROWSER_CAPTURE_TRANSIENT_CANVAS_IDS = new Set([
      "price-interaction",
      "volume-interaction",
    ]);
    const BROWSER_CAPTURE_XHTML_NS = "http://www.w3.org/1999/xhtml";
    const browserCaptureTextEncoder = new TextEncoder();
    let browserCaptureWork = Promise.resolve();

    function browserCaptureConnectionScope() {
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = exactIdentityText(instrumentRouteFingerprint());
      const timeframe = String(state.timeframe || "");
      if (
        state.instrumentSelectionStatus?.state !== "resolved"
        || !instrumentId
        || !routeFingerprint
        || !/^[1-9][0-9]{0,3}[mhdw]$/.test(timeframe)
      ) return null;
      return {
        instrumentId,
        routeFingerprint,
        timeframe,
        key: JSON.stringify([instrumentId, routeFingerprint, timeframe]),
      };
    }

    function browserCaptureRequestMatchesCurrent(message) {
      const scope = browserCaptureConnectionScope();
      return Boolean(
        scope
        && BROWSER_CAPTURE_SCOPES.has(message?.scope)
        && BROWSER_CAPTURE_REQUEST_ID_PATTERN.test(String(message?.request_id || ""))
        && exactIdentityText(message?.instrument_id) === scope.instrumentId
        && exactIdentityText(message?.route_fingerprint) === scope.routeFingerprint
        && String(message?.timeframe || "") === scope.timeframe
        && (!state.snapshot || snapshotMatchesCurrentRoute(state.snapshot))
      );
    }

    function browserCaptureTarget(scope) {
      if (scope === "price") return document.getElementById("price-frame");
      if (scope === "terminal") return document.querySelector("main");
      return null;
    }

    function browserCaptureRemainingMs(deadline, errorCode) {
      const remaining = Math.floor(Number(deadline) - performance.now());
      if (remaining <= 0) throw new Error(errorCode);
      return remaining;
    }

    function browserCaptureCanvasGroupKey(canvas, rect) {
      const parent = canvas.parentElement;
      if (!parent) return "";
      const parentRect = parent.getBoundingClientRect();
      const quantize = value => Math.round(Number(value) * 2) / 2;
      return [
        quantize(rect.left - parentRect.left),
        quantize(rect.top - parentRect.top),
        quantize(rect.width),
        quantize(rect.height),
      ].join(":");
    }

    function browserCaptureApplyCanvasStyle(source, target, zIndex) {
      const computed = window.getComputedStyle(source);
      const properties = [
        "position",
        "inset",
        "top",
        "right",
        "bottom",
        "left",
        "display",
        "width",
        "height",
        "box-sizing",
        "opacity",
        "transform",
        "transform-origin",
        "border-radius",
        "filter",
        "mix-blend-mode",
      ];
      for (const property of properties) {
        target.style.setProperty(property, computed.getPropertyValue(property));
      }
      target.style.setProperty("z-index", String(zIndex));
      target.style.setProperty("object-fit", "fill");
      target.style.setProperty("pointer-events", "none");
    }

    async function browserCaptureBlobDataUrl(blob, deadline) {
      return new Promise((resolve, reject) => {
        const timeoutMs = Math.min(
          browserCaptureRemainingMs(deadline, "BROWSER_CAPTURE_DATA_URL_TIMEOUT"),
          BROWSER_CAPTURE_DATA_URL_TIMEOUT_MS,
        );
        const reader = new FileReader();
        let settled = false;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timer);
          callback(value);
        };
        const timer = window.setTimeout(() => {
          try {
            reader.abort();
          } catch (_) {
            // The FileReader may already have completed between timer turns.
          }
          finish(reject, new Error("BROWSER_CAPTURE_DATA_URL_TIMEOUT"));
        }, timeoutMs);
        reader.onload = () => {
          const value = typeof reader.result === "string" ? reader.result : "";
          if (!value.startsWith("data:image/png;base64,")) {
            finish(reject, new Error("BROWSER_CAPTURE_DATA_URL_FAILED"));
            return;
          }
          finish(resolve, value);
        };
        reader.onerror = () => finish(
          reject,
          new Error("BROWSER_CAPTURE_DATA_URL_FAILED"),
        );
        reader.onabort = () => finish(
          reject,
          new Error("BROWSER_CAPTURE_DATA_URL_TIMEOUT"),
        );
        reader.readAsDataURL(blob);
      });
    }

    async function cloneBrowserCaptureNode(source, captureScale, deadline) {
      const sourceElements = [source, ...source.querySelectorAll("*")];
      if (sourceElements.length > BROWSER_CAPTURE_MAX_DOM_NODES) {
        throw new Error("BROWSER_CAPTURE_DOM_BOUNDS_EXCEEDED");
      }
      const clone = source.cloneNode(true);
      const cloneElements = [clone, ...clone.querySelectorAll("*")];
      if (sourceElements.length !== cloneElements.length) {
        throw new Error("BROWSER_CAPTURE_DOM_CLONE_FAILED");
      }

      const canvasGroupsByParent = new Map();
      const canvasActions = new Map();
      let sourceCanvasOrder = 0;
      for (const element of sourceElements) {
        if (!(element instanceof HTMLCanvasElement)) continue;
        sourceCanvasOrder += 1;
        if (BROWSER_CAPTURE_TRANSIENT_CANVAS_IDS.has(element.id)) {
          canvasActions.set(element, { type: "remove" });
          continue;
        }
        const computed = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        if (
          computed.display === "none"
          || computed.visibility === "hidden"
          || Number(computed.opacity) <= 0
          || rect.width < 1
          || rect.height < 1
          || element.width < 1
          || element.height < 1
        ) {
          canvasActions.set(element, { type: "remove" });
          continue;
        }
        const parent = element.parentElement;
        const key = browserCaptureCanvasGroupKey(element, rect);
        if (!parent || !key) throw new Error("BROWSER_CAPTURE_CANVAS_LAYOUT_INVALID");
        let parentGroups = canvasGroupsByParent.get(parent);
        if (!parentGroups) {
          parentGroups = new Map();
          canvasGroupsByParent.set(parent, parentGroups);
        }
        let group = parentGroups.get(key);
        if (!group) {
          group = { rect, members: [] };
          parentGroups.set(key, group);
        }
        const parsedZIndex = Number.parseFloat(computed.zIndex);
        group.members.push({
          source: element,
          computed,
          order: sourceCanvasOrder,
          zIndex: Number.isFinite(parsedZIndex) ? parsedZIndex : 0,
        });
      }

      const canvasGroups = [];
      for (const parentGroups of canvasGroupsByParent.values()) {
        canvasGroups.push(...parentGroups.values());
      }
      if (canvasGroups.length > BROWSER_CAPTURE_MAX_CANVAS_GROUPS) {
        throw new Error("BROWSER_CAPTURE_CANVAS_GROUP_BOUNDS_EXCEEDED");
      }

      let totalCanvasPixels = 0;
      const preparedGroups = canvasGroups.map(group => {
        const width = Math.max(1, Math.floor(group.rect.width * captureScale));
        const height = Math.max(1, Math.floor(group.rect.height * captureScale));
        if (
          width > BROWSER_CAPTURE_MAX_DIMENSION
          || height > BROWSER_CAPTURE_MAX_DIMENSION
          || width * height > BROWSER_CAPTURE_MAX_PIXELS
        ) throw new Error("BROWSER_CAPTURE_CANVAS_PIXEL_BOUNDS_EXCEEDED");
        totalCanvasPixels += width * height;
        if (totalCanvasPixels > BROWSER_CAPTURE_MAX_PIXELS) {
          throw new Error("BROWSER_CAPTURE_CANVAS_PIXEL_BOUNDS_EXCEEDED");
        }
        const composite = document.createElement("canvas");
        composite.width = width;
        composite.height = height;
        const context = composite.getContext("2d", { alpha: true });
        if (!context) throw new Error("BROWSER_CAPTURE_CANVAS_UNAVAILABLE");
        const paintOrder = [...group.members].sort(
          (left, right) => left.zIndex - right.zIndex || left.order - right.order,
        );
        try {
          for (const member of paintOrder) {
            context.globalAlpha = Math.min(
              Math.max(Number(member.computed.opacity) || 0, 0),
              1,
            );
            context.drawImage(member.source, 0, 0, width, height);
          }
          context.globalAlpha = 1;
        } catch (_) {
          throw new Error("BROWSER_CAPTURE_CANVAS_COMPOSITE_FAILED");
        }
        const leader = [...group.members].sort(
          (left, right) => left.order - right.order,
        )[0];
        return {
          composite,
          leader,
          maxZIndex: Math.max(...group.members.map(member => member.zIndex)),
          members: group.members,
        };
      });

      const encodedGroups = await Promise.all(preparedGroups.map(async group => {
        const blob = await browserCapturePngBlob(
          group.composite,
          deadline,
          "BROWSER_CAPTURE_CANVAS_ENCODING_TIMEOUT",
          BROWSER_CAPTURE_CANVAS_ENCODING_TIMEOUT_MS,
        );
        if (blob.size > BROWSER_CAPTURE_MAX_EMBEDDED_PNG_BYTES) {
          throw new Error("BROWSER_CAPTURE_CANVAS_PNG_TOO_LARGE");
        }
        return {
          ...group,
          blobSize: blob.size,
          dataUrl: await browserCaptureBlobDataUrl(blob, deadline),
        };
      }));
      const embeddedBytes = encodedGroups.reduce(
        (total, group) => total + group.blobSize,
        0,
      );
      if (embeddedBytes > BROWSER_CAPTURE_MAX_EMBEDDED_PNG_BYTES) {
        throw new Error("BROWSER_CAPTURE_CANVAS_PNG_TOO_LARGE");
      }
      for (const group of encodedGroups) {
        for (const member of group.members) {
          canvasActions.set(member.source, member === group.leader
            ? {
              type: "replace",
              dataUrl: group.dataUrl,
              zIndex: group.maxZIndex,
            }
            : { type: "remove" });
        }
      }

      for (let index = 0; index < sourceElements.length; index += 1) {
        const sourceElement = sourceElements[index];
        const cloneElement = cloneElements[index];
        if (sourceElement instanceof HTMLCanvasElement) {
          const action = canvasActions.get(sourceElement);
          if (!action || action.type === "remove") {
            cloneElement.remove();
            continue;
          }
          const replacement = document.createElementNS(
            BROWSER_CAPTURE_XHTML_NS,
            "img",
          );
          for (const attribute of cloneElement.attributes) {
            replacement.setAttribute(attribute.name, attribute.value);
          }
          replacement.setAttribute("src", action.dataUrl);
          replacement.setAttribute("data-browser-capture-canvas", "");
          replacement.setAttribute("aria-hidden", "true");
          replacement.setAttribute("draggable", "false");
          browserCaptureApplyCanvasStyle(
            sourceElement,
            replacement,
            action.zIndex,
          );
          cloneElement.replaceWith(replacement);
          continue;
        }
        if (sourceElement instanceof HTMLInputElement) {
          cloneElement.setAttribute("value", sourceElement.value);
          if (sourceElement.checked) cloneElement.setAttribute("checked", "");
          else cloneElement.removeAttribute("checked");
        } else if (sourceElement instanceof HTMLTextAreaElement) {
          cloneElement.textContent = sourceElement.value;
        } else if (sourceElement instanceof HTMLOptionElement) {
          if (sourceElement.selected) cloneElement.setAttribute("selected", "");
          else cloneElement.removeAttribute("selected");
        } else if (sourceElement instanceof HTMLDetailsElement) {
          if (sourceElement.open) cloneElement.setAttribute("open", "");
          else cloneElement.removeAttribute("open");
        } else if (
          sourceElement instanceof HTMLImageElement
          && !String(sourceElement.currentSrc || sourceElement.src || "").startsWith("data:")
        ) {
          cloneElement.remove();
          continue;
        }
        if (sourceElement.scrollLeft || sourceElement.scrollTop) {
          for (const child of cloneElement.children) {
            const currentTransform = child.style.transform === "none"
              ? ""
              : child.style.transform;
            child.style.setProperty(
              "transform",
              `translate(${-sourceElement.scrollLeft}px, ${-sourceElement.scrollTop}px) ${currentTransform}`.trim(),
            );
          }
        }
      }
      return clone;
    }

    function browserCaptureStylesheetText() {
      const chunks = [];
      for (const sheet of document.styleSheets) {
        try {
          chunks.push(Array.from(sheet.cssRules || []).map(rule => rule.cssText).join("\n"));
        } catch (_) {
          // The terminal assets are same-origin; ignore any operator-injected sheet.
        }
      }
      chunks.push(
        "*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;}",
        "[data-browser-capture-canvas]{display:block;object-fit:fill;pointer-events:none;user-select:none;}",
      );
      return chunks.join("\n");
    }

    function loadBrowserCaptureSvg(svg, deadline) {
      let source;
      try {
        source = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
      } catch (_) {
        return Promise.reject(new Error("BROWSER_CAPTURE_SVG_ENCODING_FAILED"));
      }
      return new Promise((resolve, reject) => {
        const timeoutMs = Math.min(
          browserCaptureRemainingMs(deadline, "BROWSER_CAPTURE_SVG_RENDER_TIMEOUT"),
          BROWSER_CAPTURE_SVG_RENDER_TIMEOUT_MS,
        );
        const image = new Image();
        let settled = false;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timer);
          callback(value);
        };
        const timer = window.setTimeout(() => {
          image.src = "";
          finish(reject, new Error("BROWSER_CAPTURE_SVG_RENDER_TIMEOUT"));
        }, timeoutMs);
        image.onload = () => {
          finish(resolve, image);
        };
        image.onerror = () => {
          finish(reject, new Error("BROWSER_CAPTURE_SVG_RENDER_FAILED"));
        };
        image.src = source;
      });
    }

    function browserCapturePngBlob(
      canvas,
      deadline,
      timeoutCode = "BROWSER_CAPTURE_PNG_ENCODING_TIMEOUT",
      phaseTimeoutMs = BROWSER_CAPTURE_PNG_ENCODING_TIMEOUT_MS,
    ) {
      return new Promise((resolve, reject) => {
        const timeoutMs = Math.min(
          browserCaptureRemainingMs(deadline, timeoutCode),
          phaseTimeoutMs,
        );
        let settled = false;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timer);
          callback(value);
        };
        const timer = window.setTimeout(
          () => finish(reject, new Error(timeoutCode)),
          timeoutMs,
        );
        try {
          canvas.toBlob(blob => {
            if (blob?.type === "image/png" && blob.size > 0) {
              finish(resolve, blob);
            } else {
              finish(reject, new Error("BROWSER_CAPTURE_PNG_ENCODING_FAILED"));
            }
          }, "image/png");
        } catch (_) {
          finish(reject, new Error("BROWSER_CAPTURE_PNG_ENCODING_FAILED"));
        }
      });
    }

    async function rasterizeBrowserCaptureTarget(
      target,
      deadline = performance.now() + BROWSER_CAPTURE_TOTAL_TIMEOUT_MS,
    ) {
      const rect = target?.getBoundingClientRect();
      if (
        !target
        || !rect
        || !Number.isFinite(rect.width)
        || !Number.isFinite(rect.height)
        || rect.width < 32
        || rect.height < 32
      ) throw new Error("BROWSER_CAPTURE_TARGET_UNAVAILABLE");

      const cssWidth = Math.max(1, Math.round(rect.width));
      const cssHeight = Math.max(1, Math.round(rect.height));
      const deviceScale = Math.min(Math.max(Number(window.devicePixelRatio) || 1, 1), 2);
      let scale = Math.min(
        deviceScale,
        BROWSER_CAPTURE_MAX_DIMENSION / cssWidth,
        BROWSER_CAPTURE_MAX_DIMENSION / cssHeight,
        Math.sqrt(BROWSER_CAPTURE_MAX_PIXELS / (cssWidth * cssHeight)),
        9_000 / (cssWidth + cssHeight),
      );
      scale = Math.max(
        Math.min(scale, 2),
        1 / Math.max(cssWidth, cssHeight),
      );

      await Promise.race([
        document.fonts?.ready || Promise.resolve(),
        new Promise(resolve => window.setTimeout(resolve, 250)),
      ]);
      const clone = await cloneBrowserCaptureNode(target, scale, deadline);
      if (!clone) throw new Error("BROWSER_CAPTURE_TARGET_UNAVAILABLE");
      clone.setAttribute("xmlns", BROWSER_CAPTURE_XHTML_NS);
      clone.style.setProperty("box-sizing", "border-box", "important");
      clone.style.setProperty("position", "relative", "important");
      clone.style.setProperty("inset", "auto", "important");
      clone.style.setProperty("transform", "none", "important");
      clone.style.setProperty("margin", "0", "important");
      clone.style.setProperty("width", `${cssWidth}px`, "important");
      clone.style.setProperty("height", `${cssHeight}px`, "important");

      const body = document.createElementNS(BROWSER_CAPTURE_XHTML_NS, "body");
      body.setAttribute("xmlns", BROWSER_CAPTURE_XHTML_NS);
      body.setAttribute("class", document.body.className);
      body.style.cssText = document.body.getAttribute("style") || "";
      body.style.setProperty("margin", "0");
      body.style.setProperty("width", `${cssWidth}px`);
      body.style.setProperty("height", `${cssHeight}px`);
      body.style.setProperty("overflow", "hidden");
      for (const property of document.documentElement.style) {
        if (!property.startsWith("--")) continue;
        body.style.setProperty(
          property,
          document.documentElement.style.getPropertyValue(property),
          document.documentElement.style.getPropertyPriority(property),
        );
      }
      const style = document.createElementNS(BROWSER_CAPTURE_XHTML_NS, "style");
      style.textContent = browserCaptureStylesheetText();
      body.append(style, clone);
      const serialized = new XMLSerializer().serializeToString(body);
      const svg = [
        `<svg xmlns="http://www.w3.org/2000/svg" width="${cssWidth}" height="${cssHeight}" viewBox="0 0 ${cssWidth} ${cssHeight}">`,
        `<foreignObject x="0" y="0" width="${cssWidth}" height="${cssHeight}">`,
        serialized,
        "</foreignObject></svg>",
      ].join("");
      if (svg.length > BROWSER_CAPTURE_MAX_SVG_CHARS) {
        throw new Error("BROWSER_CAPTURE_SVG_BOUNDS_EXCEEDED");
      }
      const image = await loadBrowserCaptureSvg(svg, deadline);

      for (let attempt = 0; attempt < 3; attempt += 1) {
        const width = Math.max(1, Math.floor(cssWidth * scale));
        const height = Math.max(1, Math.floor(cssHeight * scale));
        if (
          width > BROWSER_CAPTURE_MAX_DIMENSION
          || height > BROWSER_CAPTURE_MAX_DIMENSION
          || width * height > BROWSER_CAPTURE_MAX_PIXELS
        ) throw new Error("BROWSER_CAPTURE_OUTPUT_BOUNDS_EXCEEDED");
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d", { alpha: false });
        if (!context) throw new Error("BROWSER_CAPTURE_CANVAS_UNAVAILABLE");
        const targetBackground = window.getComputedStyle(target).backgroundColor;
        const bodyBackground = window.getComputedStyle(document.body).backgroundColor;
        context.fillStyle = ["", "transparent", "rgba(0, 0, 0, 0)"].includes(targetBackground)
          ? bodyBackground || "#ffffff"
          : targetBackground;
        context.fillRect(0, 0, width, height);
        context.drawImage(image, 0, 0, width, height);
        const blob = await browserCapturePngBlob(canvas, deadline);
        if (blob.size <= BROWSER_CAPTURE_MAX_PNG_BYTES) {
          return { blob, width, height };
        }
        scale *= 0.72;
      }
      throw new Error("BROWSER_CAPTURE_PNG_TOO_LARGE");
    }

    function sendBrowserCaptureState() {
      const capture = state.transport.browserCapture;
      const scope = browserCaptureConnectionScope();
      const socket = capture.socket;
      if (
        !scope
        || !socket
        || socket.readyState !== WebSocket.OPEN
        || capture.key !== scope.key
      ) return false;
      try {
        socket.send(JSON.stringify({
          type: "capture_state",
          visible: document.visibilityState === "visible",
          focused: typeof document.hasFocus === "function" && document.hasFocus(),
          timeframe: scope.timeframe,
        }));
      } catch (_) {
        if (capture.socket === socket) closeBrowserCaptureSocket();
        return false;
      }
      capture.lastStateAt = Date.now();
      return true;
    }

    function sendBrowserCaptureError(socket, requestId, error, elapsedMs) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const rawCode = String(error?.message || "BROWSER_CAPTURE_FAILED");
      const errorCode = /^BROWSER_CAPTURE_[A-Z0-9_]+$/.test(rawCode)
        ? rawCode
        : "BROWSER_CAPTURE_FAILED";
      try {
        socket.send(JSON.stringify({
          type: "capture_error",
          request_id: requestId,
          error_code: errorCode,
          elapsed_ms: Math.max(0, Math.round(Number(elapsedMs) || 0)),
        }));
      } catch (_) {
        if (state.transport.browserCapture.socket === socket) {
          closeBrowserCaptureSocket();
        }
      }
    }

    async function fulfillBrowserCaptureRequest(socket, message, startedAt = performance.now()) {
      const requestId = String(message?.request_id || "");
      const deadline = startedAt + BROWSER_CAPTURE_TOTAL_TIMEOUT_MS;
      try {
        browserCaptureRemainingMs(deadline, "BROWSER_CAPTURE_QUEUE_TIMEOUT");
        if (!browserCaptureRequestMatchesCurrent(message)) {
          throw new Error("BROWSER_CAPTURE_SCOPE_CHANGED");
        }
        if (message?.scope === "price" && !state.snapshot?.bars?.length) {
          throw new Error("BROWSER_CAPTURE_SNAPSHOT_UNAVAILABLE");
        }
        if (state.snapshot?.bars?.length && typeof renderCharts === "function") {
          renderCharts(state.snapshot, {
            immediate: true,
            overlayImmediate: true,
            force: true,
          });
        }
        const target = browserCaptureTarget(message.scope);
        const result = await rasterizeBrowserCaptureTarget(target, deadline);
        browserCaptureRemainingMs(deadline, "BROWSER_CAPTURE_TOTAL_TIMEOUT");
        if (
          !browserCaptureRequestMatchesCurrent(message)
          || socket !== state.transport.browserCapture.socket
          || socket.readyState !== WebSocket.OPEN
        ) throw new Error("BROWSER_CAPTURE_SCOPE_CHANGED");
        const header = browserCaptureTextEncoder.encode(
          `${BROWSER_CAPTURE_PROTOCOL_PREFIX}${requestId}:`,
        );
        socket.send(new Blob([header, result.blob], { type: "application/octet-stream" }));
      } catch (error) {
        sendBrowserCaptureError(
          socket,
          requestId,
          error,
          performance.now() - startedAt,
        );
      }
    }

    function queueBrowserCaptureRequest(socket, message) {
      const startedAt = performance.now();
      browserCaptureWork = browserCaptureWork
        .catch(() => undefined)
        .then(() => fulfillBrowserCaptureRequest(socket, message, startedAt));
    }

    function closeBrowserCaptureSocket() {
      const capture = state.transport.browserCapture;
      if (capture.retryTimer) {
        clearTimeout(capture.retryTimer);
        capture.retryTimer = null;
      }
      const socket = capture.socket;
      capture.socket = null;
      capture.key = "";
      capture.ready = false;
      capture.openedAt = 0;
      capture.lastStateAt = 0;
      if (socket) closeWebSocketQuietly(socket, "browser capture scope changed");
    }

    function connectBrowserCapture(force = false) {
      const capture = state.transport.browserCapture;
      const scope = browserCaptureConnectionScope();
      if (!scope) {
        closeBrowserCaptureSocket();
        return;
      }
      const current = capture.socket;
      if (
        !force
        && capture.key === scope.key
        && current
        && [WebSocket.CONNECTING, WebSocket.OPEN].includes(current.readyState)
      ) return;
      closeBrowserCaptureSocket();
      capture.key = scope.key;
      capture.epoch += 1;
      const epoch = capture.epoch;
      const params = new URLSearchParams({
        instrument_id: scope.instrumentId,
        expected_route_fingerprint: scope.routeFingerprint,
        timeframe: scope.timeframe,
        client_id: state.clientId,
      });
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(
        `${protocol}//${window.location.host}/ws/browser-capture?${params.toString()}`,
      );
      capture.socket = socket;
      socket.onopen = () => {
        if (capture.socket !== socket || capture.epoch !== epoch) return;
        capture.openedAt = Date.now();
      };
      socket.onmessage = event => {
        if (
          capture.socket !== socket
          || capture.epoch !== epoch
          || typeof event.data !== "string"
        ) return;
        let message;
        try {
          message = JSON.parse(event.data);
        } catch (_) {
          closeBrowserCaptureSocket();
          return;
        }
        if (message?.type === "capture_request" && capture.ready) {
          queueBrowserCaptureRequest(socket, message);
          return;
        }
        const ready = Boolean(
          message?.type === "capture_ready"
          && exactIdentityText(message.instrument_id) === scope.instrumentId
          && exactIdentityText(message.route_fingerprint) === scope.routeFingerprint
          && String(message.timeframe || "") === scope.timeframe
        );
        if (!ready) {
          closeBrowserCaptureSocket();
          return;
        }
        capture.ready = true;
        sendBrowserCaptureState();
      };
      socket.onclose = () => {
        if (capture.socket !== socket || capture.epoch !== epoch) return;
        capture.socket = null;
        capture.ready = false;
        capture.openedAt = 0;
        capture.lastStateAt = 0;
        capture.retryTimer = window.setTimeout(() => {
          capture.retryTimer = null;
          connectBrowserCapture(false);
        }, BROWSER_CAPTURE_RECONNECT_MS);
      };
      socket.onerror = () => {
        if (capture.socket === socket) {
          closeBrowserCaptureSocket();
        }
      };
    }

    function maintainBrowserCaptureConnection(now = Date.now()) {
      const scope = browserCaptureConnectionScope();
      const capture = state.transport.browserCapture;
      if (capture.retryTimer) return;
      if (!scope || capture.key !== scope.key || !capture.socket) {
        connectBrowserCapture(false);
        return;
      }
      if (
        capture.ready
        && capture.socket.readyState === WebSocket.OPEN
        && now - Number(capture.lastStateAt || 0)
        >= BROWSER_CAPTURE_STATE_INTERVAL_MS
      ) sendBrowserCaptureState();
    }

    function setupBrowserCapture() {
      connectBrowserCapture(false);
      document.addEventListener("visibilitychange", () => {
        if (!sendBrowserCaptureState()) connectBrowserCapture(false);
      });
      window.addEventListener("focus", sendBrowserCaptureState);
      window.addEventListener("blur", sendBrowserCaptureState);
      window.addEventListener("pagehide", closeBrowserCaptureSocket);
    }
