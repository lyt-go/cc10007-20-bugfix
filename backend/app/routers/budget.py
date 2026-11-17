"""预算分配接口：维护预算方案，覆盖方案评审、批准、冻结与额度重算、数据集接纳。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.allocation import allocation_service
from app.services.budget import BudgetService

router = APIRouter(prefix="/api/budget", tags=["预算分配"])

service = BudgetService()

LIST_FIELDS = ["方案编号", "预算总额", "渠道分配比例", "人群分配比例", "时段分配策略", "消耗速率上限", "调整周期", "ROI目标"]
STATUSES = ["草稿", "方案评审中", "已批准", "已冻结"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    status: str | None = Query(default=None, description="草稿、方案评审中、已批准、已冻结"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按方案编号与状态过滤预算分配列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出预算分配清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "budget", "total": total, "items": items}


@router.get("/pending", response_model=PageResult[dict])
def list_pending() -> PageResult[dict]:
    """LTV估算待处理明细：由渠道预算分配明细数据集投递而来。"""
    items = allocation_service.pending_items()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 20)


@router.get("/datasets/{dataset_no}", response_model=dict)
def get_dataset(dataset_no: str) -> dict:
    """读取单个渠道预算分配明细数据集，历史数据集按统一口径补齐。"""
    from app.store import store

    for dataset in store.rows("budget_dataset"):
        if dataset.get("数据集编号") == dataset_no:
            return allocation_service.normalize_dataset(dataset)
    raise HTTPException(status_code=404, detail=f"数据集 {dataset_no} 不存在")


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条预算方案明细；不存在时给出可读的错误说明。"""
    entry = allocation_service.get_plan(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预算方案 {entry_id} 不存在或已归档")
    payload = dict(entry)
    payload["当前渠道额度"] = allocation_service.plan_allocations(entry)
    payload["数据集"] = [allocation_service.normalize_dataset(row) for row in allocation_service.plan_datasets(entry)]
    payload["业务版本材料"] = allocation_service.business_material(entry)
    payload["价值链路"] = allocation_service.value_chain(entry)
    payload["计算预览"] = allocation_service.preview(entry)
    return payload


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预算方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预算方案已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """对单条预算方案执行提交方案评审、批准方案、冻结方案；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    remark = str(payload.values.get("remark") or payload.remark or "")
    if action == "批准方案":
        entry, message = allocation_service.approve(entry_id)
    elif action == "冻结方案":
        entry, message = allocation_service.freeze(entry_id, reason=remark)
    elif action == "提交方案评审":
        entry, message = allocation_service.reopen(entry_id)
    else:
        entry, message = service.run_action(int(entry_id) if str(entry_id).isdigit() else 0, action)
        if entry is None:
            return ActionResult(ok=False, message=message)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/recalculate", response_model=ActionResult)
def recalculate(entry_id: str, payload: EntryPayload | None = None) -> ActionResult:
    """按故障链路上的LTV模型状态重算渠道额度，生成版本化的渠道预算分配明细数据集。"""
    values = (payload.values if payload else {}) or {}
    dataset, message = allocation_service.recalculate(
        entry_id,
        business_version=str(values.get("业务版本") or "") or None,
        request_id=str(values.get("请求编号") or "") or None,
    )
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.post("/{entry_id}/datasets", response_model=ActionResult)
def accept_dataset(entry_id: str, payload: EntryPayload) -> ActionResult:
    """接纳上传的渠道预算分配明细数据集：来源失效与重复结果都会被拒绝。"""
    dataset, message = allocation_service.accept_dataset(entry_id, payload.values)
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.post("/{entry_id}/deliver", response_model=ActionResult)
def deliver(entry_id: str, payload: EntryPayload | None = None) -> ActionResult:
    """把渠道预算分配明细数据集投递到LTV估算待处理明细；来源失效的方案停止批准。"""
    values = (payload.values if payload else {}) or {}
    item, message = allocation_service.deliver(
        entry_id,
        dataset_no=str(values.get("数据集编号") or "") or None,
    )
    if item is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=item)


@router.post("/{entry_id}/value-version", response_model=ActionResult)
def replace_value_version(entry_id: str, payload: EntryPayload) -> ActionResult:
    """把方案替换为新的LTV价值版本并重算；已批准方案保持原预算时点状态。"""
    plan, message = allocation_service.replace_value_version(entry_id, payload.values)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)


@router.post("/{entry_id}/business-version", response_model=ActionResult)
def confirm_business_version(entry_id: str, payload: EntryPayload | None = None) -> ActionResult:
    """确认业务版本材料，结果版本只能取该材料确认的版本。"""
    values = (payload.values if payload else {}) or {}
    material, message = allocation_service.confirm_business_version(
        entry_id,
        version=str(values.get("业务版本") or "") or None,
    )
    if material is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=material)


@router.post("/{entry_id}/ltv-receipt", response_model=ActionResult)
def ltv_receipt(entry_id: str) -> ActionResult:
    """LTV下线回执：与预算批准请求竞争同一方案时只允许一个处理终点。"""
    plan, message = allocation_service.ltv_receipt(entry_id)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)


@router.post("/{entry_id}/package", response_model=ActionResult)
def package(entry_id: str) -> ActionResult:
    """打包本轮渠道预算分配明细数据集，形成可交付的接收文件。"""
    record, message = allocation_service.package(entry_id)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.post("/{entry_id}/receive", response_model=ActionResult)
def receive(entry_id: str, payload: EntryPayload) -> ActionResult:
    """文件接收：只接收摘要一致、来源有效的交付包，接收后批准结果才落定。"""
    package, message = allocation_service.receive_file(entry_id, payload.values)
    if package is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=package)


@router.get("/{entry_id}/packages", response_model=PageResult[dict])
def list_packages(entry_id: str) -> PageResult[dict]:
    """查看方案的打包与接收记录。"""
    from app.store import store

    items = [row for row in store.rows("budget_package") if row.get("方案编号") == entry_id]
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 20)


@router.get("/{entry_id}/audit", response_model=dict)
def audit(entry_id: str, chain_version: str | None = Query(default=None)) -> dict:
    """按遗留调用链版本核对断点：校验中模型参与、废弃模型残留、重复批准与失败回退。"""
    result = allocation_service.audit(entry_id, chain_version=chain_version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"预算方案 {entry_id} 不存在或已归档")
    return result
