"""仅绑定回环地址的本地只读 Web 查看器。"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import DEFAULT_RUNS_DIR
from .errors import ErrorCode, FunnelCheckError
from .ledger import list_comparisons, list_runs
from .pipeline import comparison_dir, load_comparison, load_run_report, run_dir, runs_root
from .render_markdown import QUALITY_LABELS, REASON_LABELS

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.2f}%"


def _signed_pct(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{number:+.2f}%"


def _friendly_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


_TEMPLATES.env.filters["pct"] = _pct
_TEMPLATES.env.filters["signed_pct"] = _signed_pct
_TEMPLATES.env.filters["friendly_time"] = _friendly_time


def validate_loopback_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized == "localhost":
        return "localhost"
    try:
        ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise FunnelCheckError(
            ErrorCode.HOST_NOT_ALLOWED,
            f"拒绝绑定非回环地址：{host}",
            hint="仅允许 127.0.0.1、::1 或 localhost。",
        ) from exc
    if normalized not in {"127.0.0.1", "::1"}:
        raise FunnelCheckError(
            ErrorCode.HOST_NOT_ALLOWED,
            f"拒绝绑定非回环地址：{host}",
            hint="仅允许 127.0.0.1、::1 或 localhost。",
        )
    return host


def _http_status(exc: FunnelCheckError) -> int:
    if exc.code in (ErrorCode.RUN_NOT_FOUND, ErrorCode.ARTIFACT_MISSING):
        return 404
    if exc.code == ErrorCode.HOST_NOT_ALLOWED:
        return 403
    return 400


def create_app(root: str | Path | None = None) -> FastAPI:
    root_path = runs_root(root if root is not None else DEFAULT_RUNS_DIR)
    ledger_path = root_path / "ledger.sqlite"
    app = FastAPI(
        title="漏斗验算台",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(FunnelCheckError)
    async def handle_domain_error(request: Request, exc: FunnelCheckError) -> Any:
        status = _http_status(exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse(exc.as_dict(), status_code=status)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="error.html",
            context={"error": exc.as_dict()},
            status_code=status,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "runs": list_runs(ledger_path),
                "comparisons": list_comparisons(ledger_path),
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str) -> Any:
        report = load_run_report(root_path, run_id)
        funnel_max = max(
            [step.get("attempts", 0) for step in report.get("funnel", {}).get("steps", [])]
            or [0]
        )
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="run.html",
            context={
                "report": report,
                "funnel_max": funnel_max,
                "quality_labels": QUALITY_LABELS,
                "reason_labels": REASON_LABELS,
            },
        )

    @app.get("/runs/{run_id}/report.html", response_class=FileResponse)
    def run_report_html(run_id: str) -> FileResponse:
        path = run_dir(root_path, run_id) / "report.html"
        if not path.is_file():
            raise FunnelCheckError(ErrorCode.ARTIFACT_MISSING, f"找不到 {run_id} 的 HTML 报告")
        return FileResponse(path, media_type="text/html; charset=utf-8")

    @app.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
    def run_report_markdown(run_id: str) -> FileResponse:
        path = run_dir(root_path, run_id) / "report.md"
        if not path.is_file():
            raise FunnelCheckError(ErrorCode.ARTIFACT_MISSING, f"找不到 {run_id} 的 Markdown 报告")
        return FileResponse(path, media_type="text/markdown; charset=utf-8")

    @app.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
    def comparison_detail(request: Request, comparison_id: str) -> Any:
        comparison = load_comparison(root_path, comparison_id)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="compare.html",
            context={"comparison": comparison, "quality_labels": QUALITY_LABELS},
        )

    @app.get("/comparisons/{comparison_id}/compare.html", response_class=FileResponse)
    def comparison_html(comparison_id: str) -> FileResponse:
        path = comparison_dir(root_path, comparison_id) / "compare.html"
        if not path.is_file():
            raise FunnelCheckError(
                ErrorCode.ARTIFACT_MISSING, f"找不到 {comparison_id} 的 HTML 报告"
            )
        return FileResponse(path, media_type="text/html; charset=utf-8")

    @app.get("/comparisons/{comparison_id}/compare.md", response_class=PlainTextResponse)
    def comparison_markdown(comparison_id: str) -> FileResponse:
        path = comparison_dir(root_path, comparison_id) / "compare.md"
        if not path.is_file():
            raise FunnelCheckError(
                ErrorCode.ARTIFACT_MISSING, f"找不到 {comparison_id} 的 Markdown 报告"
            )
        return FileResponse(path, media_type="text/markdown; charset=utf-8")

    @app.get("/api/runs")
    def api_runs() -> dict[str, Any]:
        return {"runs": list_runs(ledger_path)}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str) -> dict[str, Any]:
        return load_run_report(root_path, run_id)

    @app.get("/api/comparisons")
    def api_comparisons() -> dict[str, Any]:
        return {"comparisons": list_comparisons(ledger_path)}

    @app.get("/api/comparisons/{comparison_id}")
    def api_comparison(comparison_id: str) -> dict[str, Any]:
        return load_comparison(root_path, comparison_id)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "read_only": True,
            "ledger_present": ledger_path.is_file(),
            "bind": "loopback-only",
        }

    return app


def serve(
    root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    log_level: str = "warning",
) -> None:
    validate_loopback_host(host)
    if not 1 <= port <= 65535:
        raise FunnelCheckError(ErrorCode.USAGE, f"端口无效：{port}")
    import uvicorn

    uvicorn.run(
        create_app(root),
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
    )
