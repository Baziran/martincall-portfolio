class CanvasManager {
      constructor() {
        this.layers = [];
      }

      register(layer) {
        if (!layer || typeof layer.draw !== "function") return this;
        this.layers.push({
          id: layer.id || `canvas-layer-${this.layers.length + 1}`,
          phase: layer.phase || "overlay",
          zIndex: Number.isFinite(Number(layer.zIndex)) ? Number(layer.zIndex) : canvasLayerZIndex(layer.phase),
          enabled: layer.enabled,
          draw: layer.draw,
        });
        return this;
      }

      draw(phase, env, options = {}) {
        const rows = this.layers
          .filter(layer => !phase || layer.phase === phase)
          .filter(layer => !layer.enabled || layer.enabled(env, options))
          .sort((left, right) => {
            const z = Number(left.zIndex || 0) - Number(right.zIndex || 0);
            return z || String(left.id || "").localeCompare(String(right.id || ""));
          });
        for (const layer of rows) {
          try {
            layer.draw(env, options);
          } catch (error) {
            console.error(`Canvas layer failed: ${layer.id || "unknown"}`, error);
            if (typeof debugStep === "function") debugStep("canvas layer failed", `${layer.id || "unknown"}: ${error?.message || error}`);
          }
        }
      }
    }

    function canvasLayerZIndex(phase) {
      return {
        grid: 0,
        background: 10,
        history: 20,
        indicators: 30,
        gex: 40,
        signals: 40,
        profile: 50,
        objects: 60,
        trading: 70,
        interaction: 80,
        tooltips: 90,
      }[String(phase || "").toLowerCase()] ?? 40;
    }

    let priceCanvasManager = null;

    function createPriceCanvasManager() {
      const manager = new CanvasManager();
      manager.register({
        id: "price-background-overlays",
        phase: "background",
        zIndex: canvasLayerZIndex("background"),
        draw: env => drawManagedPriceOverlays("background", env),
      });
      manager.register({
        id: "price-overlay",
        phase: "indicators",
        zIndex: canvasLayerZIndex("indicators"),
        draw: env => drawManagedPriceOverlays("overlay", env),
      });
      manager.register({
        id: "price-gex-levels",
        phase: "gex",
        zIndex: canvasLayerZIndex("gex"),
        draw: env => drawManagedPriceOverlays("gex", env),
      });
      manager.register({
        id: "price-profile",
        phase: "profile",
        zIndex: canvasLayerZIndex("profile"),
        draw: env => drawManagedPriceOverlays("profile", env),
      });
      manager.register({
        id: "price-objects",
        phase: "objects",
        zIndex: canvasLayerZIndex("objects"),
        draw: (env, options) => drawManagedObjectLayers("objects", env, options),
      });
      manager.register({
        id: "price-percent-scale",
        phase: "objects",
        zIndex: canvasLayerZIndex("objects") + 2,
        enabled: () => typeof drawHoveredPricePercentScale === "function",
        draw: env => drawHoveredPricePercentScale(env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.maxP, env.minP, env.width),
      });
      manager.register({
        id: "price-current-marker",
        phase: "objects",
        zIndex: canvasLayerZIndex("objects") + 5,
        draw: env => drawCurrentPriceMarker(env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.xStep, env.x, env.y, env.width),
      });
      manager.register({
        id: "price-time-axis",
        phase: "objects",
        zIndex: canvasLayerZIndex("objects") + 10,
        draw: env => drawTimeAxis(env.ctx, env.bars, env.pad, env.priceH, env.xStep),
      });
      manager.register({
        id: "price-trading",
        phase: "trading",
        zIndex: canvasLayerZIndex("trading"),
        draw: env => drawManagedObjectLayers("trading", env),
      });
      manager.register({
        id: "price-interaction-objects",
        phase: "interaction",
        zIndex: canvasLayerZIndex("interaction"),
        draw: env => drawManagedObjectLayers("interaction", env),
      });
      manager.register({
        id: "price-crosshair-price",
        phase: "interaction",
        zIndex: canvasLayerZIndex("interaction") + 5,
        draw: env => drawCrosshairPrice(env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.maxP, env.minP, env.width),
      });
      manager.register({
        id: "price-crosshair-time",
        phase: "interaction",
        zIndex: canvasLayerZIndex("interaction") + 10,
        draw: env => drawCrosshairTimeLabel(env.ctx, env.bars, env.pad, env.priceH, env.width),
      });
      return manager;
    }

    function priceCanvasManagerInstance() {
      if (!priceCanvasManager) priceCanvasManager = createPriceCanvasManager();
      return priceCanvasManager;
    }

    function drawManagedCanvasLayers(phase, env, options = {}) {
      priceCanvasManagerInstance().draw(phase, env, options);
    }
