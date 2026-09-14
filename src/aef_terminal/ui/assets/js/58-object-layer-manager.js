class ObjectLayerManager {
      constructor() {
        this.providers = [];
      }

      register(provider) {
        if (provider && (typeof provider.draw === "function" || typeof provider.hitTest === "function")) this.providers.push(provider);
        return this;
      }

      draw(phase, env, options = {}) {
        const providers = this.providers
          .filter(provider => !phase || provider.phase === phase)
          .filter(provider => !provider.enabled || provider.enabled(env, options))
          .sort((left, right) => Number(left.zIndex || 0) - Number(right.zIndex || 0));
        for (const provider of providers) {
          if (typeof provider.draw !== "function") continue;
          try {
            provider.draw(env, options);
          } catch (error) {
            console.error(`Object layer failed: ${provider.id || provider.kind || "unknown"}`, error);
            if (typeof debugStep === "function") debugStep("object layer failed", `${provider.id || provider.kind || "unknown"}: ${error?.message || error}`);
          }
        }
      }

      hitTest(kind, localX, localY, env = {}) {
        const providers = this.providers
          .filter(provider => !kind || provider.kind === kind || provider.kind === "all")
          .filter(provider => typeof provider.hitTest === "function")
          .filter(provider => !provider.enabled || provider.enabled(env, {}))
          .sort((left, right) => Number(right.zIndex || 0) - Number(left.zIndex || 0));
        for (const provider of providers) {
          try {
            const hit = provider.hitTest(localX, localY, env);
            if (hit) return { ...hit, source: hit.source || provider.id || provider.source };
          } catch (error) {
            console.error(`Object layer hit-test failed: ${provider.id || provider.kind || "unknown"}`, error);
            if (typeof debugStep === "function") debugStep("object hit-test failed", `${provider.id || provider.kind || "unknown"}: ${error?.message || error}`);
          }
        }
        return null;
      }
    }

    let managedObjectLayerManager = null;

    function objectLayerCall(name, args, required = false) {
      if (typeof window !== "undefined" && typeof window[name] === "function") return window[name](...args);
      if (typeof globalThis !== "undefined" && typeof globalThis[name] === "function") return globalThis[name](...args);
      if (typeof self !== "undefined" && typeof self[name] === "function") return self[name](...args);
      if (required) throw new Error(`Object layer function is not available: ${name}`);
      return null;
    }

    function createObjectLayerManager() {
      const manager = new ObjectLayerManager();
      manager.register({
        id: "price-alerts",
        kind: "alert",
        phase: "objects",
        zIndex: 10,
        enabled: env => !env.gexOnly,
        draw: (env, options) => objectLayerCall("drawPriceAlerts", [env.ctx, env.pad, env.width, env.priceH, env.y, { excludeIds: options.excludePriceAlertIds || [] }], true),
        hitTest: (localX, localY) => {
          const hit = objectLayerCall("hitTestPriceAlert", [localX, localY]);
          return hit ? { source: "price-alerts", kind: "alert", hit } : null;
        },
      });
      manager.register({
        id: "drawings",
        kind: "drawing",
        phase: "objects",
        zIndex: 20,
        enabled: env => !env.gexOnly,
        draw: (env, options) => objectLayerCall("drawDrawings", [env.ctx, env.visible, env.pad, env.priceH, env.xStep, env.x, env.y, env.width, {
          staticOnly: true,
          excludeIds: options.excludeDrawingIds || [],
        }], true),
        hitTest: (localX, localY) => {
          const handle = objectLayerCall("hitTestDrawingHandle", [localX, localY]);
          if (handle) return { source: "drawings", kind: "drawing-handle", handle };
          const id = objectLayerCall("hitTestDrawing", [localX, localY]);
          return id ? { source: "drawings", kind: "drawing", id } : null;
        },
      });
      manager.register({
        id: "option-targets",
        kind: "option-target",
        phase: "objects",
        zIndex: 30,
        enabled: env => !env.gexOnly,
        draw: env => objectLayerCall("drawOptionTargets", [env.ctx, env.visible, env.pad, env.priceH, env.xStep, env.x, env.y, env.width]),
        hitTest: (localX, localY) => {
          const id = objectLayerCall("hitTestOptionTarget", [localX, localY]);
          return id ? { source: "option-targets", kind: "option-target", id } : null;
        },
      });
      manager.register({
        id: "paper-orders",
        kind: "paper-order",
        phase: "trading",
        zIndex: 40,
        enabled: env => !env.gexOnly,
        draw: env => objectLayerCall("drawPaperTradeOrders", [env.ctx, env.pad, env.width, env.y, { actions: true, bars: env.bars, x: env.x }]),
        hitTest: (localX, localY) => {
          const hit = objectLayerCall("hitTestPaperOrderAction", [localX, localY]);
          return hit ? { source: "paper-orders", kind: "paper-order", hit } : null;
        },
      });
      manager.register({
        id: "dragged-price-alert",
        kind: "alert",
        phase: "interaction",
        zIndex: 50,
        enabled: env => !env.gexOnly && Boolean(state.alerts.drag),
        draw: env => objectLayerCall("drawPriceAlerts", [env.ctx, env.pad, env.width, env.priceH, env.y, { onlyIds: [state.alerts.drag], tooltips: false }], true),
      });
      manager.register({
        id: "drawing-interaction",
        kind: "drawing",
        phase: "interaction",
        zIndex: 60,
        enabled: env => !env.gexOnly,
        draw: env => objectLayerCall("drawDrawingInteraction", [env.ctx, env.visible, env.pad, env.priceH, env.xStep, env.x, env.y, env.width]),
      });
      return manager;
    }

    function drawManagedObjectLayers(phase, env, options = {}) {
      objectLayerManagerInstance().draw(phase, env, options);
    }

    function hitTestManagedObjectLayers(kind, localX, localY, env = {}) {
      return objectLayerManagerInstance().hitTest(kind, localX, localY, env);
    }

    function objectLayerManagerInstance() {
      if (!managedObjectLayerManager) managedObjectLayerManager = createObjectLayerManager();
      return managedObjectLayerManager;
    }
