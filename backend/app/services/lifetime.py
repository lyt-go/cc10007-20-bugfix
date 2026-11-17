"""LTV估算业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "lifetime"
REQUIRED_FIELDS = ["模型编号", "模型名称", "估算方法"]
STATUS_ORDER = ["草稿", "模型校验中", "已启用", "已废弃"]
ACTION_RULES = {"提交模型校验": "模型校验中", "启用模型": "已启用", "废弃模型": "已废弃"}
NEGATIVE_ACTIONS = ["废弃模型"]


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class LifetimeService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(store.rows(MODULE), key=lambda row: int(row.get("id", 0)))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模型编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["status_history"] = [{"时间": _now(), "状态": STATUS_ORDER[0], "动作": "登记LTV模型"}]
        entry["状态版本"] = 1
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"LTV模型 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于LTV估算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or "")
        if current == "已废弃" and target != "已废弃":
            return None, f"LTV模型 {entry.get('模型编号')} 已废弃，不能再回到{target}"
        if current == "已启用" and target == "模型校验中":
            return None, "已启用的LTV模型不能退回模型校验中，请先废弃再登记新模型"
        if current == target:
            return None, f"LTV模型已处于{target}，重复动作不改变状态"
        entry["status"] = target
        entry["状态版本"] = int(entry.get("状态版本") or 1) + 1
        entry.setdefault("status_history", []).append({
            "时间": _now(),
            "状态": target,
            "动作": action,
            "前一状态": current,
        })
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["状态生效时间"] = _now()
        return entry, f"LTV模型已{action}"
