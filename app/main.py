"""FastAPI 路由（薄）：页面渲染 + 少量 JSON API，核心规则全部在纯函数模块。"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import acceptance, accept_store, batchio, report, schemaio, settings, store
from .schemaio import SchemaError
from .types import DECIMAL_RULES, matrix_table

app = FastAPI(title=settings.APP_NAME)
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(settings.BASE_DIR / "app" / "templates"))


def _ctx(request: Request, active: str, **extra) -> dict:
    ctx = {"app_name": settings.APP_NAME, "active": active, "matrix_version": settings.MATRIX_VERSION}
    ctx.update(extra)
    return ctx


def _index_stats(sample_count: int, history: list[dict]) -> dict:
    """总览页只读统计：全部从既有运行记录派生，不改动任何业务判定。"""
    steps = [int(r.get("summary", {}).get("sql_steps") or 0) for r in history]
    return {
        "sample_count": sample_count,
        "run_count": len(history),
        "full_count": sum(1 for r in history if r.get("summary", {}).get("level") == "full"),
        "backfill_count": sum(1 for r in history if int(r.get("summary", {}).get("backfill_count") or 0) > 0),
        "breaking_count": sum(1 for r in history if not r.get("summary", {}).get("can_execute", True)),
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0.0,
    }


def _resolve_inputs(payload: dict) -> tuple[dict, dict, dict, str]:
    if not isinstance(payload, dict):
        raise SchemaError("请求体必须是 JSON 对象")
    sample_key = str(payload.get("sample") or "").strip().upper()
    if sample_key in settings.SAMPLE_KEYS:
        sample = schemaio.load_sample(sample_key)
        return sample["old"], sample["new"], sample["consumers"], f"样例 {sample_key}"
    old = schemaio.parse_schema(payload.get("old"), "旧 schema")
    new = schemaio.parse_schema(payload.get("new"), "新 schema")
    consumers = schemaio.parse_consumers(payload.get("consumers"), "数据文件清单")
    return old, new, consumers, "自定义输入"


def _resolve_batch(payload: dict) -> tuple[dict, str, int | None]:
    if not isinstance(payload, dict):
        raise SchemaError("请求体必须是 JSON 对象")
    sample_key = str(payload.get("sample") or "").strip().upper()
    if sample_key in settings.BATCH_SAMPLE_KEYS:
        sample = batchio.load_batch_sample(sample_key)
        batch, source = sample["batch"], f"批次样例 {sample_key}"
    else:
        batch = batchio.parse_batch(payload.get("batch"), "批次定义")
        source = "自定义输入"
    simulate = payload.get("simulate_interrupt_after")
    if simulate is not None:
        if isinstance(simulate, bool) or not isinstance(simulate, int) or simulate < 0:
            raise SchemaError("simulate_interrupt_after 必须是非负整数（演示中断：执行 N 个任务后模拟进程崩溃）")
    return batch, source, simulate


def _acceptance_not_found(acceptance_id: str):
    return JSONResponse({"detail": f"验收记录 {acceptance_id} 不存在"}, status_code=404)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": "schemaevo", "matrix_version": settings.MATRIX_VERSION}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    samples = schemaio.load_sample_index()
    history = store.list_runs(limit=50)
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(
            request, "/",
            page="index",
            samples=samples,
            recent=history[:3],
            stats=_index_stats(len(samples), history),
        ),
    )


@app.get("/workbench", response_class=HTMLResponse)
async def workbench(request: Request):
    key = (request.query_params.get("sample") or "A").strip().upper()
    if key not in settings.SAMPLE_KEYS:
        key = "A"
    sample = schemaio.load_sample(key)
    # 右侧面板「预演」：复用既有纯函数算一遍所选样例，不落库、不改接口。
    preview = report.build_run(sample["old"], sample["new"], sample["consumers"], f"样例 {key}")
    return templates.TemplateResponse(
        request, "workbench.html",
        _ctx(
            request, "/workbench",
            page="workbench",
            samples=schemaio.load_sample_index(),
            selected=key,
            sample=sample,
            preview=preview,
            old_text=sample["raw_old"],
            new_text=sample["raw_new"],
            consumers_text=sample["raw_consumers"],
        ),
    )


@app.post("/api/compare")
async def api_compare(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        old, new, consumers, source = _resolve_inputs(payload)
        if old["table"] != new["table"]:
            raise SchemaError(f"新旧 schema 的表名不一致：{old['table']} vs {new['table']}")
        if consumers.get("table") and consumers["table"] != new["table"]:
            raise SchemaError(f"数据文件清单的表名 {consumers['table']} 与新 schema {new['table']} 不一致")
        run = report.build_run(old, new, consumers, source)
        run = store.save_run(run)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "run_id": run["run_id"],
        "summary": run["summary"],
        "redirect": f"/result/{run['run_id']}",
    }


@app.get("/api/samples/{key}")
async def api_sample(key: str):
    try:
        sample = schemaio.load_sample(key)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "key": sample["key"],
        "label": sample["label"],
        "description": sample["description"],
        "expected": sample["expected"],
        "old": sample["raw_old"],
        "new": sample["raw_new"],
        "consumers": sample["raw_consumers"],
    }


@app.get("/api/matrix")
async def api_matrix() -> dict:
    return {"version": settings.MATRIX_VERSION, "promotions": matrix_table(), "decimal_rules": DECIMAL_RULES}


@app.get("/api/runs")
async def api_runs() -> dict:
    rows = store.list_runs(limit=50)
    return {"count": len(rows), "runs": rows}


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return JSONResponse({"detail": f"运行记录 {run_id} 不存在"}, status_code=404)
    return run


@app.get("/api/runs/{run_id}/migration.sql", response_class=PlainTextResponse)
async def api_sql(run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return PlainTextResponse(f"-- 运行记录 {run_id} 不存在\n", status_code=404)
    return PlainTextResponse(
        run["migration"]["sql_text"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{report.sql_download_name(run)}"'},
    )


@app.get("/result/{run_id}", response_class=HTMLResponse)
async def result_page(request: Request, run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(
                request, "",
                page="not_found",
                run_id=run_id,
                max_runs=settings.MAX_RUNS_KEPT,
                error=f"运行记录 {run_id} 不存在（历史记录只保留最近 {settings.MAX_RUNS_KEPT} 次）",
            ),
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "result.html",
        _ctx(request, "", page="result", run=run, overview=report.overview(run)),
    )


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    runs = store.list_runs(limit=50)
    latest = store.load_run(runs[0]["run_id"]) if runs else None
    acceptances = accept_store.list_acceptances(limit=50)
    return templates.TemplateResponse(
        request, "review.html",
        _ctx(
            request, "/review",
            page="review",
            runs=runs,
            run_count=len(runs),
            latest=latest,
            acceptances=acceptances,
            promotions=matrix_table(),
            decimal_rules=DECIMAL_RULES,
            sample_index=schemaio.load_sample_index(),
            max_runs=settings.MAX_RUNS_KEPT,
        ),
    )


# ---- 批次验收：多表变更按业务批次整体验收 ----


@app.get("/batch", response_class=HTMLResponse)
async def batch_page(request: Request):
    key = (request.query_params.get("sample") or "D").strip().upper()
    if key not in settings.BATCH_SAMPLE_KEYS:
        key = "D"
    sample = batchio.load_batch_sample(key)
    return templates.TemplateResponse(
        request, "batch.html",
        _ctx(
            request, "/batch",
            page="batch",
            samples=batchio.load_batch_sample_index(),
            selected=key,
            sample=sample,
            batch_text=sample["raw"],
            acceptances=accept_store.list_acceptances(limit=20),
        ),
    )


@app.post("/api/acceptances")
async def api_acceptance_create(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        batch, source, simulate = _resolve_batch(payload)
        record = acceptance.create(batch, source, acceptance_id=accept_store.new_acceptance_id())
        accept_store.save_acceptance(record)
        record = acceptance.run(record, save=accept_store.save_acceptance,
                                simulate_interrupt_after=simulate)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "acceptance_id": record["acceptance_id"],
        "status": record["status"],
        "summary": acceptance.summarize(record),
        "redirect": f"/acceptance/{record['acceptance_id']}",
    }


@app.post("/api/acceptances/{acceptance_id}/resume")
async def api_acceptance_resume(acceptance_id: str):
    try:
        record = accept_store.load_acceptance(acceptance_id)
    except KeyError:
        return _acceptance_not_found(acceptance_id)
    if not any(t["status"] == "pending" for t in record["tasks"]):
        return JSONResponse({"detail": "没有待续跑的任务：该验收已全部执行完成"}, status_code=400)
    record = acceptance.run(record, save=accept_store.save_acceptance)
    return {
        "acceptance_id": record["acceptance_id"],
        "status": record["status"],
        "summary": acceptance.summarize(record),
        "redirect": f"/acceptance/{record['acceptance_id']}",
    }


@app.post("/api/acceptances/{acceptance_id}/retry")
async def api_acceptance_retry(acceptance_id: str):
    try:
        record = accept_store.load_acceptance(acceptance_id)
    except KeyError:
        return _acceptance_not_found(acceptance_id)
    # 重试 = 用同一份输入快照新建验收（新 id），从头执行；两次验收结果互不混用
    fresh = acceptance.create(record["input"], f"重试自 {acceptance_id}",
                              acceptance_id=accept_store.new_acceptance_id())
    accept_store.save_acceptance(fresh)
    fresh = acceptance.run(fresh, save=accept_store.save_acceptance)
    return {
        "acceptance_id": fresh["acceptance_id"],
        "status": fresh["status"],
        "summary": acceptance.summarize(fresh),
        "redirect": f"/acceptance/{fresh['acceptance_id']}",
    }


@app.get("/api/acceptances")
async def api_acceptances() -> dict:
    rows = accept_store.list_acceptances(limit=50)
    return {"count": len(rows), "acceptances": rows}


@app.get("/api/acceptances/{acceptance_id}")
async def api_acceptance(acceptance_id: str):
    try:
        return accept_store.load_acceptance(acceptance_id)
    except KeyError:
        return _acceptance_not_found(acceptance_id)


@app.get("/api/acceptances/{acceptance_id}/manifest")
async def api_acceptance_manifest(acceptance_id: str):
    try:
        record = accept_store.load_acceptance(acceptance_id)
    except KeyError:
        return _acceptance_not_found(acceptance_id)
    manifest = acceptance.build_manifest(record)
    return JSONResponse(
        manifest,
        headers={"Content-Disposition": f'attachment; filename="acceptance-{acceptance_id}.json"'},
    )


@app.get("/api/batch-samples/{key}")
async def api_batch_sample(key: str):
    try:
        sample = batchio.load_batch_sample(key)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "key": sample["key"],
        "label": sample["label"],
        "description": sample["description"],
        "expected": sample["expected"],
        "batch": sample["raw"],
    }


@app.get("/acceptance/{acceptance_id}", response_class=HTMLResponse)
async def acceptance_page(request: Request, acceptance_id: str):
    try:
        record = accept_store.load_acceptance(acceptance_id)
    except KeyError:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(
                request, "",
                page="not_found",
                run_id=acceptance_id,
                max_runs=settings.MAX_ACCEPTANCES_KEPT,
                error=f"验收记录 {acceptance_id} 不存在（验收记录只保留最近 {settings.MAX_ACCEPTANCES_KEPT} 次）",
            ),
            status_code=404,
        )
    related = [r for r in accept_store.list_acceptances(limit=100) if r["batch_id"] == record["batch_id"]]
    return templates.TemplateResponse(
        request, "acceptance.html",
        _ctx(
            request, "",
            page="acceptance",
            acc=record,
            summary=acceptance.summarize(record),
            diffs=acceptance.all_diffs(record),
            state=acceptance.batch_state(related),
            status_labels=acceptance.STATUS_LABELS,
        ),
    )
