"""使用效果追踪接口：维护使用记录，覆盖汇总数据、分析效果、归档记录等动作。

列表、调用统计汇总、效果分析、统计周期快照共用同一套过滤参数
（产品名称 product / 调用方名称 caller / 统计周期 period），保证口径一致。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.usage import usage_service

router = APIRouter(prefix="/api/usage", tags=["使用效果追踪"])

service = usage_service

LIST_FIELDS = ["记录编号", "产品名称", "调用方名称", "调用次数", "平均耗时", "错误次数", "统计周期", "记录状态"]
STATUSES = ["采集中", "已汇总", "已分析", "已归档"]


def _scope_param(request: Request, *names: str) -> str | None:
    """兼容英文参数名与中文字段名两种传参（前端筛选框直接用中文字段名）。"""
    for name in names:
        value = request.query_params.get(name)
        if value and value.strip():
            return value.strip()
    return None


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="采集中、已汇总、已分析、已归档"),
    product: str | None = Query(default=None, description="按产品名称/产品编号过滤"),
    caller: str | None = Query(default=None, description="按调用方名称过滤"),
    period: str | None = Query(default=None, description="按统计周期过滤，例如 2026-06"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按产品、调用方、统计周期等条件过滤使用记录；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    product = product or _scope_param(request, "产品名称")
    caller = caller or _scope_param(request, "调用方名称")
    period = period or _scope_param(request, "统计周期")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        product=product,
        caller=caller,
        period=period,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


def _read_scope(payload: EntryPayload | None) -> tuple[str, str, str]:
    values = payload.values if payload is not None else {}
    product = str(values.get("product") or values.get("产品名称") or "").strip()
    caller = str(values.get("caller") or values.get("调用方名称") or "").strip()
    period = str(values.get("period") or values.get("统计周期") or "").strip()
    return product, caller, period


@router.get("/summary")
def get_summary(
    request: Request,
    product: str | None = None,
    caller: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """调用统计汇总：按当前过滤范围对明细重算（明细聚合优先于既有周期汇总）。"""
    product = product or _scope_param(request, "产品名称")
    caller = caller or _scope_param(request, "调用方名称")
    period = period or _scope_param(request, "统计周期")
    if not (product and caller and period):
        raise HTTPException(status_code=400, detail="汇总需要同时提供产品名称、调用方名称和统计周期")
    snapshot, message, ok = service.summarize(product=product, caller=caller, period=period)
    return {"ok": ok, "message": message, "snapshot": snapshot}


@router.post("/summary")
def run_summary(payload: EntryPayload) -> dict[str, Any]:
    """触发一次周期汇总；重复过滤/汇总复用同一份快照，失败时沿用上一份完整快照。"""
    product, caller, period = _read_scope(payload)
    fail = bool(payload.values.get("fail"))
    if not (product and caller and period):
        raise HTTPException(status_code=400, detail="汇总需要同时提供产品名称、调用方名称和统计周期")
    snapshot, message, ok = service.summarize(
        product=product, caller=caller, period=period, fail=fail
    )
    return {"ok": ok, "message": message, "snapshot": snapshot}


@router.get("/analysis")
def get_analysis(
    request: Request,
    product: str | None = None,
    caller: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """效果分析：分析结果只从当前范围的统计周期快照读取，不另起口径。"""
    product = product or _scope_param(request, "产品名称")
    caller = caller or _scope_param(request, "调用方名称")
    period = period or _scope_param(request, "统计周期")
    if not (product and caller and period):
        raise HTTPException(status_code=400, detail="分析需要同时提供产品名称、调用方名称和统计周期")
    snapshot, message, ok = service.analyze(product=product, caller=caller, period=period)
    if not ok or snapshot is None:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message, "snapshot": snapshot, "analysis": snapshot.get("分析结果")}


@router.get("/snapshots/current")
def get_current_snapshot(
    request: Request,
    product: str | None = None,
    caller: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """只读当前过滤范围的统计周期快照，不触发重算。"""
    product = product or _scope_param(request, "产品名称")
    caller = caller or _scope_param(request, "调用方名称")
    period = period or _scope_param(request, "统计周期")
    if not (product and caller and period):
        raise HTTPException(status_code=400, detail="读取快照需要同时提供产品名称、调用方名称和统计周期")
    snapshot, message = service.get_snapshot(product=product, caller=caller, period=period)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "snapshot": snapshot}


@router.get("/export")
def export_entries(
    request: Request,
    product: str | None = None,
    caller: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """导出使用效果追踪清单：返回当前过滤条件下的全量数据。"""
    product = product or _scope_param(request, "产品名称")
    caller = caller or _scope_param(request, "调用方名称")
    period = period or _scope_param(request, "统计周期")
    items, total = service.list_entries(
        product=product, caller=caller, period=period, page=1, size=10000
    )
    return {"module": "usage", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条使用记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"使用记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条使用记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="使用记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条使用记录执行汇总数据、分析效果、归档记录；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
