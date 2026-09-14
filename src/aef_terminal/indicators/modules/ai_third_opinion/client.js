    const aiThirdOpinionDiscussionState = {
      key: "",
      activeRequestKey: "",
      loading: false,
      messages: [],
      draft: "",
      error: "",
      usage: null,
      providerTest: { loading: false, result: null, error: "" },
    };

    const AI_THIRD_OPINION_MODELS_BY_PROVIDER = Object.freeze({
      openai: ["gpt-5.5"],
      deepseek: ["deepseek-v4-flash", "deepseek-v4-pro"],
      gemini: ["gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
      ollama: ["llama3.2", "qwen2.5", "gemma3", "deepseek-r1", "qwen2.5-coder:14b", "deepseek-r1:14b", "deepseek-r1:8b"],
      codex: ["gpt-5.6-terra", "gpt-5.6-sol"],
    });

    function aiThirdOpinionModelsForProvider(provider) {
      return AI_THIRD_OPINION_MODELS_BY_PROVIDER[String(provider || "").toLowerCase()]
        || AI_THIRD_OPINION_MODELS_BY_PROVIDER.openai;
    }

    function syncAiThirdOpinionModelOptions({ resetInvalid = false } = {}) {
      const providerNode = document.getElementById("ai-third-opinion-provider");
      const modelNode = document.getElementById("ai-third-opinion-model");
      if (!providerNode || !modelNode) return false;
      const allowed = new Set(["default", ...aiThirdOpinionModelsForProvider(providerNode.value)]);
      for (const option of modelNode.options) {
        const visible = allowed.has(option.value);
        option.hidden = !visible;
        option.disabled = !visible;
      }
      if (allowed.has(modelNode.value)) return false;
      modelNode.value = "default";
      if (resetInvalid && state.indicators?.aiThirdOpinion) {
        state.indicators.aiThirdOpinion.model = "default";
      }
      return true;
    }

    function aiThirdOpinionDiscussionKey(snapshot) {
      const meta = snapshot?.meta || {};
      return JSON.stringify([
        exactIdentityText(meta.route_fingerprint || instrumentRouteFingerprint()),
        String(meta.timeframe || state.timeframe || ""),
      ]);
    }

    function aiThirdOpinionDiscussionSnapshot(snapshot) {
      try {
        const copy = typeof structuredClone === "function"
          ? structuredClone(snapshot)
          : JSON.parse(JSON.stringify(snapshot));
        return typeof compactMarketSnapshot === "function" ? compactMarketSnapshot(copy) : copy;
      } catch (_) {
        return snapshot;
      }
    }

    function aiThirdOpinionDiscussionUsageText(usage) {
      if (!usage || typeof usage !== "object") return "";
      const total = Number(usage.total_tokens);
      if (!Number.isFinite(total)) return "";
      const input = usage.input_tokens ?? "-";
      const output = usage.output_tokens ?? "-";
      return `${total} tok · ${input}/${output}`;
    }

    function aiThirdOpinionConnector(snapshot = state.snapshot) {
      const latest = snapshot?.indicators?.ai_third_opinion?.latest || {};
      const connector = latest.connector && typeof latest.connector === "object" ? latest.connector : {};
      return {
        provider: String(document.getElementById("ai-third-opinion-provider")?.value || connector.provider || latest.source || "openai"),
        model: String(document.getElementById("ai-third-opinion-model")?.value || connector.model || "default"),
      };
    }

    let aiThirdOpinionGateDesiredOpen = false;
    let aiThirdOpinionGateAppliedOpen = null;
    let aiThirdOpinionGateSyncPromise = null;
    let aiThirdOpinionGateNotifyFailure = false;

    function syncAiThirdOpinionModuleLifecycle(options = {}) {
      const calcEnabled = typeof indicatorCalcForId === "function"
        ? indicatorCalcForId("ai_third_opinion")
        : false;
      const connector = aiThirdOpinionConnector();
      aiThirdOpinionGateDesiredOpen = Boolean(calcEnabled && connector.provider === "codex");
      if (options.force) aiThirdOpinionGateAppliedOpen = null;
      if (options.notify) aiThirdOpinionGateNotifyFailure = true;
      if (typeof applySideTabs === "function") applySideTabs();
      if (aiThirdOpinionGateSyncPromise || aiThirdOpinionGateAppliedOpen === aiThirdOpinionGateDesiredOpen) {
        return aiThirdOpinionGateSyncPromise || Promise.resolve(null);
      }
      aiThirdOpinionGateSyncPromise = (async () => {
        let result = null;
        while (aiThirdOpinionGateAppliedOpen !== aiThirdOpinionGateDesiredOpen) {
          const targetOpen = aiThirdOpinionGateDesiredOpen;
          try {
            result = await fetchJson("/api/ai-third-opinion/gate", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                enabled: targetOpen,
                provider: targetOpen ? "codex" : connector.provider,
              }),
              timeoutMs: 7000,
              dedupe: false,
            });
          } catch (error) {
            result = {
              ok: false,
              status: "bridge_unavailable",
              message: requestErrorMessage(error, "Codex gate synchronization failed"),
            };
          }
          if (!result?.ok) {
            if (aiThirdOpinionGateNotifyFailure) {
              showBrowserToast(result?.message || result?.status || "Codex gate synchronization failed");
            }
            break;
          }
          aiThirdOpinionGateAppliedOpen = result.gate === "open";
        }
        aiThirdOpinionGateNotifyFailure = false;
        aiThirdOpinionGateSyncPromise = null;
        return result;
      })();
      return aiThirdOpinionGateSyncPromise;
    }

    function aiThirdOpinionDiscussionStatus(snapshot) {
      const latest = snapshot?.indicators?.ai_third_opinion?.latest || {};
      const connector = latest.connector && typeof latest.connector === "object" ? latest.connector : {};
      const source = latest.source || connector.status || "local";
      const view = latest.market_view ? String(latest.market_view).toUpperCase() : "WAIT";
      const phase = String(latest.phase || latest.state || "context").replace(/_/g, " ");
      const age = Number.isInteger(connector.response_bars_ago) ? `${connector.response_bars_ago} bars` : "local";
      return `${view} · ${phase} · ${source} · ${age}`;
    }

    function renderAiThirdOpinionDiscussionMessages(messages) {
      if (!messages.length) {
        return `<div class="ai-third-opinion-discuss-empty">Ask why the current view is long, short, or wait.</div>`;
      }
      return messages.map(item => {
        const role = item.role === "assistant" ? "ai" : "you";
        const meta = role === "ai"
          ? [item.provider, item.model, aiThirdOpinionDiscussionUsageText(item.usage)].filter(Boolean).join(" · ")
          : "";
        const suggestions = role === "ai" && Array.isArray(item.objectSuggestions)
          ? item.objectSuggestions
          : [];
        const suggestionCards = suggestions.map(suggestion => {
          const overlay = suggestion?.overlay && typeof suggestion.overlay === "object" ? suggestion.overlay : {};
          const level = suggestion?.kind === "target_zone"
            ? `${fmt(overlay.bottom)} – ${fmt(overlay.top)}`
            : fmt(overlay.y1 ?? overlay.price);
          const confidence = Number(suggestion?.confidence);
          const stale = String(suggestion?.context_bar_ts || "") !== String(
            aiThirdOpinionLatestConfirmedBarTs(state.snapshot),
          );
          return `
            <div class="ai-third-opinion-object-card" data-ai-object-id="${escapeHtml(suggestion.id || "")}">
              <div class="ai-third-opinion-object-head">
                <b>${escapeHtml(suggestion.label || "AI object")}</b>
                <span>${escapeHtml(level)}${Number.isFinite(confidence) ? ` · ${Math.round(confidence * 100)}%` : ""}</span>
              </div>
              ${suggestion.rationale ? `<p>${escapeHtml(suggestion.rationale)}</p>` : ""}
              ${stale ? `<small>Based on an earlier confirmed bar; review before adding.</small>` : ""}
              <div class="ai-third-opinion-object-actions">
                <button type="button" data-ai-object-action="add" data-ai-object-id="${escapeHtml(suggestion.id || "")}">Add</button>
                <button type="button" data-ai-object-action="edit" data-ai-object-id="${escapeHtml(suggestion.id || "")}">Edit</button>
                <button type="button" data-ai-object-action="reject" data-ai-object-id="${escapeHtml(suggestion.id || "")}">Reject</button>
              </div>
            </div>
          `;
        }).join("");
        return `
          <div class="ai-third-opinion-discuss-msg ${role}">
            <span>${role === "ai" ? `AI${meta ? ` · ${escapeHtml(meta)}` : ""}` : "You"}</span>
            <p>${escapeHtml(item.content || "")}</p>
            ${suggestionCards}
          </div>
        `;
      }).join("");
    }

    function aiThirdOpinionLatestConfirmedBarTs(snapshot = state.snapshot) {
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      for (let index = bars.length - 1; index >= 0; index -= 1) {
        const bar = bars[index];
        if (
          bar?.closed === true
          && (!bar.state || bar.state === "confirmed")
          && bar.authoritative !== false
          && !bar.missing
          && !bar.data_gap
        ) return String(bar.ts || "");
      }
      return "";
    }

    function aiThirdOpinionDiscussionObjectSuggestions() {
      const messages = Array.isArray(aiThirdOpinionDiscussionState.messages)
        ? aiThirdOpinionDiscussionState.messages
        : [];
      return messages.flatMap(message => (
        message?.role === "assistant" && Array.isArray(message.objectSuggestions)
          ? message.objectSuggestions
          : []
      ));
    }

    function aiThirdOpinionDiscussionPreviewOverlays() {
      return aiThirdOpinionDiscussionObjectSuggestions()
        .filter(suggestion => aiThirdOpinionObjectScopeMatches(suggestion))
        .map(suggestion => suggestion?.overlay)
        .filter(overlay => overlay && typeof overlay === "object");
    }

    function aiThirdOpinionDiscussionPreviewKey() {
      return aiThirdOpinionDiscussionObjectSuggestions()
        .map(suggestion => [
          suggestion?.id || "",
          suggestion?.context_bar_ts || "",
          suggestion?.overlay?.top ?? "",
          suggestion?.overlay?.bottom ?? "",
          suggestion?.overlay?.y1 ?? suggestion?.overlay?.price ?? "",
        ].join(":"))
        .join("|");
    }

    function aiThirdOpinionObjectScopeMatches(suggestion) {
      const scope = suggestion?.scope && typeof suggestion.scope === "object" ? suggestion.scope : {};
      return exactIdentityText(scope.instrument_id) === exactIdentityText(state.instrumentId)
        && exactIdentityText(scope.route_fingerprint) === instrumentRouteFingerprint()
        && String(scope.timeframe || "") === String(state.timeframe || "");
    }

    function removeAiThirdOpinionObjectSuggestion(suggestionId) {
      const id = String(suggestionId || "");
      if (!id) return false;
      let removed = false;
      for (const message of aiThirdOpinionDiscussionState.messages || []) {
        if (!Array.isArray(message?.objectSuggestions)) continue;
        const next = message.objectSuggestions.filter(suggestion => String(suggestion?.id || "") !== id);
        if (next.length !== message.objectSuggestions.length) {
          message.objectSuggestions = next;
          removed = true;
        }
      }
      return removed;
    }

    function acceptAiThirdOpinionObjectSuggestion(suggestionId, { edit = false } = {}) {
      const suggestion = aiThirdOpinionDiscussionObjectSuggestions().find(
        item => String(item?.id || "") === String(suggestionId || ""),
      );
      if (!suggestion || !aiThirdOpinionObjectScopeMatches(suggestion)) {
        showBrowserToast("AI object belongs to a different instrument or timeframe");
        return false;
      }
      const drawing = suggestion.drawing && typeof suggestion.drawing === "object"
        ? JSON.parse(JSON.stringify(suggestion.drawing))
        : null;
      if (!drawing || !Array.isArray(drawing.points) || typeof addDrawingObject !== "function") {
        showBrowserToast("AI object geometry is unavailable");
        return false;
      }
      const saved = addDrawingObject(drawing);
      if (!saved) return false;
      removeAiThirdOpinionObjectSuggestion(suggestion.id);
      if (edit && typeof openDrawingManager === "function") {
        openDrawingManager({ focusSelectedType: true });
      }
      renderAiThirdOpinionDiscussionActivity(state.snapshot);
      showBrowserToast(edit ? "AI object added and selected for editing" : "AI object added to chart");
      return true;
    }

    function aiThirdOpinionDiscussionActionCells() {
      const discussion = aiThirdOpinionDiscussionState;
      if (discussion.loading) return ["ASK", "...", "waiting"];
      if (discussion.error) return ["ASK", "ERR", "fail"];
      const last = Array.isArray(discussion.messages) ? discussion.messages[discussion.messages.length - 1] : null;
      if (last?.role === "assistant") return ["ASK", "OK", "answer"];
      return ["ASK", "SEND", "context"];
    }

    registerIndicatorTableAction("ai-third-opinion-discuss-send", {
      cells: aiThirdOpinionDiscussionActionCells,
      click: () => {
        if (typeof indicatorCalcForId === "function" && !indicatorCalcForId("ai_third_opinion")) return;
        document.getElementById("side-tab-ai")?.click();
        sendAiThirdOpinionMarketContext(state.snapshot);
      },
    });

    function renderAiThirdOpinionDiscussionActivity(snapshot = state.snapshot) {
      if (typeof renderAiThirdOpinionDiscussion === "function") renderAiThirdOpinionDiscussion(snapshot);
      if (snapshot && typeof renderPriceOverlay === "function") renderPriceOverlay(snapshot);
    }

    async function askAiThirdOpinionDiscussion(snapshot, question) {
      const discussion = aiThirdOpinionDiscussionState;
      const requestKey = aiThirdOpinionDiscussionKey(snapshot);
      discussion.key = requestKey;
      discussion.activeRequestKey = requestKey;
      discussion.loading = true;
      discussion.error = "";
      discussion.messages.push({ role: "user", content: question });
      renderAiThirdOpinionDiscussionActivity(snapshot);
      try {
        const payload = await fetchJson("/api/ai-third-opinion/discuss", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question,
            history: discussion.messages.slice(-8),
            snapshot: aiThirdOpinionDiscussionSnapshot(snapshot),
          }),
          timeoutMs: 45000,
          dedupe: false,
        });
        if (discussion.activeRequestKey !== requestKey || discussion.key !== requestKey) return;
        if (!payload?.ok) {
          discussion.error = payload?.message || payload?.status || "AI discussion failed";
          return;
        }
        discussion.messages.push({
          role: "assistant",
          content: String(payload.answer || "").trim(),
          provider: payload.provider || "",
          model: payload.model || "",
          usage: payload.usage || null,
          objectSuggestions: Array.isArray(payload.object_suggestions)
            ? payload.object_suggestions
            : [],
        });
        discussion.usage = payload.usage || null;
      } catch (error) {
        if (discussion.activeRequestKey !== requestKey || discussion.key !== requestKey) return;
        discussion.error = requestErrorMessage(error, "AI discussion failed");
      } finally {
        if (discussion.activeRequestKey === requestKey && discussion.key === requestKey) {
          discussion.activeRequestKey = "";
          discussion.loading = false;
          renderAiThirdOpinionDiscussionActivity(state.snapshot);
        }
      }
    }

    function sendAiThirdOpinionMarketContext(snapshot = state.snapshot) {
      const discussion = aiThirdOpinionDiscussionState;
      if (!snapshot || discussion.loading) return;
      const question = [
        "Проверь текущий рыночный контекст.",
        "Смотри только подтвержденные свечи, compact candle summary и мои каналы.",
        "Дай phase, long/short/wait, где может быть ложный пробой или liquidity sweep, и что изменит взгляд.",
      ].join(" ");
      askAiThirdOpinionDiscussion(snapshot, question);
    }

    async function testAiThirdOpinionProvider(snapshot = state.snapshot) {
      const discussion = aiThirdOpinionDiscussionState;
      if (discussion.loading || discussion.providerTest?.loading) return;
      const requestKey = aiThirdOpinionDiscussionKey(snapshot);
      const connector = aiThirdOpinionConnector(snapshot);
      discussion.providerTest = { key: requestKey, loading: true, result: null, error: "" };
      renderAiThirdOpinionDiscussionActivity(snapshot);
      try {
        const payload = await fetchJson("/api/ai-third-opinion/provider-test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(connector),
          timeoutMs: 45000,
          dedupe: false,
        });
        if (discussion.key !== requestKey || discussion.providerTest?.key !== requestKey) return;
        discussion.providerTest = payload?.ok
          ? { key: requestKey, loading: false, result: payload, error: "" }
          : { key: requestKey, loading: false, result: payload || null, error: payload?.message || payload?.status || "Provider test failed" };
      } catch (error) {
        if (discussion.key !== requestKey || discussion.providerTest?.key !== requestKey) return;
        discussion.providerTest = { key: requestKey, loading: false, result: null, error: requestErrorMessage(error, "Provider test failed") };
      } finally {
        if (discussion.key === requestKey && discussion.providerTest?.key === requestKey) {
          renderAiThirdOpinionDiscussionActivity(state.snapshot);
        }
      }
    }

    function aiThirdOpinionDiscussionWaiting() {
      return Boolean(aiThirdOpinionDiscussionState.loading);
    }

    registerIndicatorTableThinkingRef("ai_third_opinion_discussion", aiThirdOpinionDiscussionWaiting);

    function renderAiThirdOpinionDiscussion(snapshot = state.snapshot) {
      const node = document.getElementById("ai-third-opinion-discuss");
      if (!node) return;
      const indicator = snapshot?.indicators?.ai_third_opinion;
      const calcEnabled = typeof indicatorCalcForId === "function" ? indicatorCalcForId("ai_third_opinion") : true;
      const contextAvailable = Boolean(calcEnabled && indicator && typeof indicator === "object");
      const discussion = aiThirdOpinionDiscussionState;
      const key = aiThirdOpinionDiscussionKey(snapshot);
      if (discussion.key && discussion.key !== key) {
        discussion.activeRequestKey = "";
        discussion.loading = false;
        discussion.messages = [];
        discussion.draft = "";
        discussion.error = "";
        discussion.usage = null;
        discussion.providerTest = { loading: false, result: null, error: "" };
      }
      discussion.key = key;
      node.classList.add("ai-third-opinion-discuss");
      node.setAttribute("aria-live", "polite");
      node.classList.remove("hidden");
      const previousInput = document.getElementById("ai-third-opinion-discuss-input");
      const restoreInputFocus = document.activeElement === previousInput;
      const selectionStart = restoreInputFocus ? previousInput.selectionStart : null;
      const selectionEnd = restoreInputFocus ? previousInput.selectionEnd : null;
      const usageText = aiThirdOpinionDiscussionUsageText(discussion.usage);
      const providerTest = discussion.providerTest || {};
      const tested = providerTest.result && typeof providerTest.result === "object" ? providerTest.result : null;
      const testStatus = providerTest.loading
        ? "test..."
        : providerTest.error
          ? `test error · ${providerTest.error}`
          : tested
            ? `test ${tested.health?.label || tested.status || "-"} · ${tested.provider || ""} ${tested.model || ""}${tested.usage?.total_tokens ? ` · ${tested.usage.total_tokens} tok` : ""}`
            : "";
      const status = discussion.loading
        ? "thinking"
        : discussion.error
          ? "error"
          : usageText || testStatus || (contextAvailable ? "ready" : calcEnabled ? "waiting for context" : "calculation off");
      node.innerHTML = `
        <div class="ai-third-opinion-discuss-head">
          <div>
            <b>AI Discuss</b>
            <span>${escapeHtml(aiThirdOpinionDiscussionStatus(snapshot))}</span>
          </div>
          <div class="ai-third-opinion-discuss-actions">
            <button id="ai-third-opinion-discuss-test" type="button" title="Test selected AI provider connection" ${providerTest.loading || discussion.loading ? "disabled" : ""}>${providerTest.loading ? "..." : "Test"}</button>
            <button id="ai-third-opinion-discuss-clear" type="button" title="Clear AI discussion">Clear</button>
          </div>
        </div>
        <div class="ai-third-opinion-discuss-log">${renderAiThirdOpinionDiscussionMessages(discussion.messages)}</div>
        ${discussion.error ? `<div class="ai-third-opinion-discuss-error">${escapeHtml(discussion.error)}</div>` : ""}
        ${providerTest.error ? `<div class="ai-third-opinion-discuss-error">${escapeHtml(providerTest.error)}</div>` : ""}
        <form id="ai-third-opinion-discuss-form" class="ai-third-opinion-discuss-form">
          <textarea id="ai-third-opinion-discuss-input" rows="3" maxlength="800" placeholder="${contextAvailable ? "Ask about this market context" : calcEnabled ? "Waiting for typed market context" : "Enable AI Third Opinion calculation to ask about market context"}" ${contextAvailable ? "" : "disabled"}></textarea>
          <button type="submit" ${discussion.loading || !contextAvailable ? "disabled" : ""}>${discussion.loading ? "..." : "Ask"}</button>
        </form>
        <div class="ai-third-opinion-discuss-foot">${escapeHtml(status)}</div>
      `;
      const form = document.getElementById("ai-third-opinion-discuss-form");
      const input = document.getElementById("ai-third-opinion-discuss-input");
      const log = node.querySelector(".ai-third-opinion-discuss-log");
      const clear = document.getElementById("ai-third-opinion-discuss-clear");
      const test = document.getElementById("ai-third-opinion-discuss-test");
      if (input) input.value = String(discussion.draft || "");
      if (restoreInputFocus && input && !input.disabled) {
        requestAnimationFrame(() => {
          input.focus();
          if (Number.isInteger(selectionStart) && Number.isInteger(selectionEnd)) {
            input.setSelectionRange(selectionStart, selectionEnd);
          }
        });
      }
      if (log && (discussion.loading || discussion.messages.length)) {
        requestAnimationFrame(() => {
          log.scrollTop = log.scrollHeight;
        });
      }
      clear?.addEventListener("click", () => {
        discussion.activeRequestKey = "";
        discussion.loading = false;
        discussion.messages = [];
        discussion.draft = "";
        discussion.error = "";
        discussion.usage = null;
        discussion.providerTest = { loading: false, result: null, error: "" };
        renderAiThirdOpinionDiscussionActivity(snapshot);
      });
      test?.addEventListener("click", () => testAiThirdOpinionProvider(snapshot));
      input?.addEventListener("input", () => {
        discussion.draft = input.value;
      });
      node.querySelectorAll("[data-ai-object-action]").forEach(button => {
        button.addEventListener("click", () => {
          const action = String(button.dataset.aiObjectAction || "");
          const suggestionId = String(button.dataset.aiObjectId || "");
          if (action === "reject") {
            if (removeAiThirdOpinionObjectSuggestion(suggestionId)) {
              renderAiThirdOpinionDiscussionActivity(state.snapshot);
            }
            return;
          }
          acceptAiThirdOpinionObjectSuggestion(suggestionId, { edit: action === "edit" });
        });
      });
      form?.addEventListener("submit", event => {
        event.preventDefault();
        const question = String(input?.value || "").trim();
        if (!question || discussion.loading) return;
        discussion.draft = "";
        askAiThirdOpinionDiscussion(snapshot, question);
      });
      input?.addEventListener("keydown", event => {
        if (event.key !== "Enter" || event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) return;
        event.preventDefault();
        if (typeof form?.requestSubmit === "function") form.requestSubmit();
        else form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      });
    }

    registerIndicatorLifecycleSync("ai_third_opinion", syncAiThirdOpinionModuleLifecycle);
    registerIndicatorSettingsSync("ai_third_opinion", syncAiThirdOpinionModelOptions);
    registerIndicatorProcessEffect("ai_third_opinion_gate", () => {
      void syncAiThirdOpinionModuleLifecycle({ force: true, notify: true });
      return false;
    });
    registerIndicatorControlEffect("ai_third_opinion_model_options", ({ spec, control, group, saveControlValue }) => {
      const reset = syncAiThirdOpinionModelOptions({ resetInvalid: true });
      if (reset && group && control?.key === "provider") {
        const modelControl = (spec?.controls || []).find(item => item.key === "model");
        if (modelControl) saveControlValue(modelControl, "default");
      }
      void syncAiThirdOpinionModuleLifecycle({ force: control?.key === "provider", notify: true });
      return false;
    });
    registerIndicatorSidebarRenderer("ai", renderAiThirdOpinionDiscussion);
    registerIndicatorOverlayContribution("ai_third_opinion", {
      layer: "zones",
      enabled: () => indicatorVisibleForId("ai_third_opinion") !== false,
      collect: aiThirdOpinionDiscussionPreviewOverlays,
      stateKey: aiThirdOpinionDiscussionPreviewKey,
    });
