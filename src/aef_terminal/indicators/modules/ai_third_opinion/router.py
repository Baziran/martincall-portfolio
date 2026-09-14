from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from aef_terminal.runtime.async_tasks import run_physical_thread_call

from .providers import set_ai_third_opinion_gate
from .service import discuss_ai_third_opinion, test_ai_third_opinion_provider


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/ai-third-opinion/discuss")
    async def discuss(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        snapshot = payload.get("snapshot")
        question = str(payload.get("question") or "")
        history = payload.get("history")
        if not isinstance(snapshot, dict):
            return {
                "ok": False,
                "status": "invalid_payload",
                "message": "snapshot object is required.",
            }
        return await run_physical_thread_call(
            discuss_ai_third_opinion,
            snapshot,
            question,
            history,
        )

    @router.post("/api/ai-third-opinion/provider-test")
    async def provider_test(
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        return await run_physical_thread_call(
            test_ai_third_opinion_provider,
            payload.get("provider"),
            payload.get("model"),
        )

    @router.post("/api/ai-third-opinion/gate")
    async def gate(
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        return await run_physical_thread_call(
            set_ai_third_opinion_gate,
            payload.get("enabled"),
            payload.get("provider"),
        )

    return router
