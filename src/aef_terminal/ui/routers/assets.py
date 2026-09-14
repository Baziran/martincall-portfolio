import gzip
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response


@dataclass(frozen=True)
class AssetRouterDeps:
    html: Callable[[bool], Awaitable[str]]
    js: Callable[[], Awaitable[str]]
    css: Callable[[], Awaitable[str]]
    quote_worker: Callable[[], Awaitable[str]]
    js_gzip: Callable[[], Awaitable[bytes]] | None = None
    css_gzip: Callable[[], Awaitable[bytes]] | None = None
    quote_worker_gzip: Callable[[], Awaitable[bytes]] | None = None


def _no_cache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def _immutable_asset_headers() -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=31536000, immutable",
    }


def _accepts_gzip(request: Request) -> bool:
    accepted: dict[str, float] = {}
    for item in str(request.headers.get("accept-encoding") or "").lower().split(","):
        parts = [part.strip() for part in item.split(";") if part.strip()]
        if not parts:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if not parameter.startswith("q="):
                continue
            try:
                quality = max(0.0, min(float(parameter[2:]), 1.0))
            except ValueError:
                quality = 0.0
        accepted[parts[0]] = quality
    if "gzip" in accepted:
        return accepted["gzip"] > 0
    return accepted.get("*", 0.0) > 0


def _asset_response(
    body: str,
    *,
    media_type: str,
    request: Request,
    compressed_body: bytes | None = None,
) -> Response:
    headers = _immutable_asset_headers()
    headers["Vary"] = "Accept-Encoding"
    raw = body.encode("utf-8")
    if _accepts_gzip(request):
        compressed = compressed_body or gzip.compress(raw, compresslevel=9, mtime=0)
        if len(compressed) < len(raw):
            headers["Content-Encoding"] = "gzip"
            return Response(content=compressed, media_type=media_type, headers=headers)
    return Response(content=body, media_type=media_type, headers=headers)


def create_asset_router(deps: AssetRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def index(debug: bool = False) -> HTMLResponse:
        return HTMLResponse(await deps.html(debug), headers=_no_cache_headers())

    @router.get("/assets/martincall.js")
    async def martincall_js(request: Request) -> Response:
        return _asset_response(
            await deps.js(),
            media_type="application/javascript",
            request=request,
            compressed_body=await deps.js_gzip() if deps.js_gzip else None,
        )

    @router.get("/assets/martincall.css")
    async def martincall_css(request: Request) -> Response:
        return _asset_response(
            await deps.css(),
            media_type="text/css",
            request=request,
            compressed_body=await deps.css_gzip() if deps.css_gzip else None,
        )

    @router.get("/assets/quote-stream-worker.js")
    async def quote_stream_worker(request: Request) -> Response:
        return _asset_response(
            await deps.quote_worker(),
            media_type="application/javascript",
            request=request,
            compressed_body=(await deps.quote_worker_gzip() if deps.quote_worker_gzip else None),
        )

    return router
