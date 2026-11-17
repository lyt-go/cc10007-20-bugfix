"""LTV估算接口：维护LTV模型，覆盖提交模型校验、启用模型、废弃模型等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.allocation import allocation_service
from app.services.lifetime import LifetimeService

router = APIRouter(prefix="/api/lifetime", tags=["LTV估算"])

service = LifetimeService()

LIST_FIELDS = ["模型编号", "模型名称", "估算方法", "观察窗口天", "预测窗口天", "折现率", "客单价因子", "留存率因子"]
STATUSES = ["草稿", "模型校验中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="草稿、模型校验中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模型编号与状态过滤LTV估算列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    usages = allocation_service.model_usage_map([str(row.get("模型编号")) for row in items])
    enriched = [{**row, "预算引用": usages.get(str(row.get("模型编号")), [])} for row in items]
    return PageResult(items=enriched, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出LTV估算清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lifetime", "total": total, "items": items}


@router.get("/pending", response_model=PageResult[dict])
def list_pending() -> PageResult[dict]:
    """LTV估算待处理明细：渠道预算分配明细数据集投递后的复核队列。"""
    items = allocation_service.pending_items()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 20)


@router.post("/pending/{pending_id}/archive", response_model=ActionResult)
def archive_pending(pending_id: str) -> ActionResult:
    """来源失效的待处理方案停止批准并归档，保留原渠道额度与ROI偏差作为历史。"""
    item, message = allocation_service.archive_pending(pending_id)
    if item is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=item)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条LTV模型明细；不存在时给出可读的错误说明。"""
    entry = allocation_service.get_model_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"LTV模型 {entry_id} 不存在或已归档")
    payload = dict(entry)
    impact = allocation_service.model_impact(entry_id) or {}
    payload["用户价值记录"] = impact.get("价值记录") or []
    payload["预算引用"] = impact.get("预算方案") or []
    payload["待处理明细"] = impact.get("待处理明细") or []
    return payload


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条LTV模型，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="LTV模型已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """对单条LTV模型执行提交模型校验、启用模型、废弃模型；状态会同步到渠道额度。"""
    action = str(payload.values.get("action") or "").strip()
    model = allocation_service.get_model_by_id(entry_id)
    if model is None:
        return ActionResult(ok=False, message=f"LTV模型 {entry_id} 不存在或已归档")
    entry, message = service.run_action(int(model["id"]), action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    changed = allocation_service.on_model_state_changed(
        str(entry.get("模型编号")),
        str(entry.get("status")),
        action=action,
    )
    if changed:
        message += "；已同批落定渠道额度：" + "、".join(str(plan.get("方案编号")) for plan in changed)
    return ActionResult(ok=True, message=message, entry=entry)
