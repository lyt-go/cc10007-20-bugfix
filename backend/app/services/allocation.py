"""渠道预算分配与LTV价值来源的联动规则。

这一层负责四件事：

1. 从故障链路上的LTV模型状态与预算分配记录读出用户价值记录；
2. 只让“已启用”的LTV模型参与渠道额度重算，输出带版本的渠道预算分配明细数据集；
3. 用同一把记录级锁裁决批准/冻结/投递等并发请求，保证同一业务记录最终只有一个有效结果；
4. 提供遗留调用链版本的断点核对口径，历史数据集与现行数据集可同口径复查。

管理判断全部收在这里，路由层只做参数解析与错误包装。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from app.store import store

VALID_MODEL_STATUS = "已启用"
STATUS_DRAFT = "草稿"
STATUS_REVIEW = "方案评审中"
STATUS_APPROVED = "已批准"
STATUS_FROZEN = "已冻结"

DATASET_PENDING = "待复核"
DATASET_VALIDATED = "已校验"
DATASET_EFFECTIVE = "已生效"
DATASET_VOID = "已作废"

TERMINAL_APPROVE = "预算批准"
TERMINAL_FREEZE = "冻结待复核"
TERMINAL_RECEIPT = "LTV下线回执冻结"

PENDING_OPEN = "待处理"
PENDING_ARCHIVED = "来源失效归档"

LEGACY_EVIDENCE = "遗留口径（按渠道额度回算）"
CURRENT_EVIDENCE = "现行口径（按数据集条目核对）"

# 让“回执”和“批准”落在同一方案时只有一个赢家，避免出现两个处理终点。
_decision_lock = threading.RLock()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _money(value: Any) -> float:
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("元", "")
        if value.endswith("%"):
            value = value[:-1]
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _add_row(module: str, id_field: str, prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    """统一补 id 与业务编号，编号与 id 始终一一对应，避免列表与详情错位。"""
    rows = store.rows(module)
    new_id = _next_id(rows)
    row = {"id": new_id, id_field: f"{prefix}-{new_id:04d}"}
    row.update(values)
    rows.append(row)
    return row


def pct(new: float, old: float) -> str:
    if old <= 0:
        return "0.00%" if new <= 0 else "新增"
    return f"{(new - old) / old * 100:+.2f}%"


class AllocationService:
    """渠道预算分配重算、数据集接纳与价值来源校验。"""

    # ---------- 读取 ----------

    def get_plan(self, entry_id: int | str) -> dict[str, Any] | None:
        for row in store.rows("budget"):
            if str(row.get("id")) == str(entry_id) or row.get("方案编号") == str(entry_id):
                return row
        return None

    def get_model(self, model_no: str) -> dict[str, Any] | None:
        for row in store.rows("lifetime"):
            if row.get("模型编号") == model_no:
                return row
        return None

    def get_model_by_id(self, entry_id: int | str) -> dict[str, Any] | None:
        for row in store.rows("lifetime"):
            if str(row.get("id")) == str(entry_id) or row.get("模型编号") == str(entry_id):
                return row
        return None

    def model_status(self, model_no: str) -> str:
        model = self.get_model(model_no)
        return str(model.get("status")) if model else "已归档"

    def value_records(self, models: list[str]) -> list[dict[str, Any]]:
        wanted = set(models)
        return [row for row in store.rows("ltv_value") if row.get("模型编号") in wanted]

    def plan_allocations(self, plan: dict[str, Any], *, current_only: bool = True) -> list[dict[str, Any]]:
        rows = [row for row in store.rows("channel_alloc") if row.get("方案编号") == plan.get("方案编号")]
        if current_only:
            rows = [row for row in rows if not row.get("历史标记")]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def plan_datasets(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        rows = [row for row in store.rows("budget_dataset") if row.get("方案编号") == plan.get("方案编号")]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def current_dataset(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        dataset_no = plan.get("数据集编号")
        for row in store.rows("budget_dataset"):
            if row.get("数据集编号") == dataset_no:
                return row
        return None

    def business_material(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        for row in store.rows("business_version"):
            if row.get("方案编号") == plan.get("方案编号"):
                return row
        return None

    def confirm_business_version(
        self,
        entry_id: int | str,
        *,
        version: str | None = None,
        operator: str = "运营负责人",
    ) -> tuple[dict[str, Any] | None, str]:
        """业务版本材料确认：结果版本只能取材料确认的业务版本。"""
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            material = self.business_material(plan)
            if material is None:
                return None, f"预算方案 {plan.get('方案编号')} 没有业务版本材料，无法确认结果版本"
            target = str(version or material.get("确认版本") or plan.get("复核版本") or "")
            if not target:
                return None, "业务版本材料没有给出可确认的版本"
            material["材料状态"] = "已确认"
            material["确认版本"] = target
            material["确认人"] = operator
            material["确认时间"] = _now()
            plan["复核版本"] = target
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "业务版本材料确认",
                "数据集编号": plan.get("数据集编号"),
                "数据集版本": plan.get("数据集版本"),
                "操作人": operator,
                "说明": f"材料 {material['材料编号']} 确认版本 {target}",
            })
            return material, f"业务版本材料 {material['材料编号']} 已确认版本 {target}"

    def confirm_version(self, plan: dict[str, Any]) -> str:
        material = self.business_material(plan)
        return str((material or {}).get("确认版本") or plan.get("复核版本") or "")

    def chain_models(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """按遗留调用链顺序列出与方案相关的LTV模型及其当前状态。

        只从“当前”渠道分配记录补齐模型，已归档的历史记录不参与重算范围，
        否则替换价值版本后旧模型仍会被拉回计算。
        """
        order: list[str] = []
        for model_no in list(plan.get("原始关联模型") or []) + list(plan.get("关联LTV模型") or []):
            if model_no not in order and self.get_model(model_no) is not None:
                order.append(model_no)
        for row in self.plan_allocations(plan):
            model_no = str(row.get("模型编号") or "")
            if model_no and model_no not in order:
                order.append(model_no)
        models: list[dict[str, Any]] = []
        for model_no in order:
            model = self.get_model(model_no)
            status = str((model or {}).get("status") or "已归档")
            records = [r for r in self.value_records([model_no]) if r.get("状态") == "有效"]
            usable = status == VALID_MODEL_STATUS
            models.append({
                "模型编号": model_no,
                "模型名称": (model or {}).get("模型名称") or "—",
                "状态": status,
                "状态版本": (model or {}).get("状态版本"),
                "关联渠道": (model or {}).get("关联渠道"),
                "价值记录数": len(records),
                "用户价值合计": round(sum(_money(r.get("用户价值")) for r in records), 2),
                "参与重算": usable,
                "排除原因": "" if usable else f"模型状态为{status}，不能支撑新的预算批准",
            })
        return models

    def value_chain(self, plan: dict[str, Any]) -> dict[str, Any]:
        """从进入故障链路的LTV模型状态与预算分配记录中读出用户价值记录。"""
        models = self.chain_models(plan)
        model_nos = [item["模型编号"] for item in models]
        records = []
        for row in self.value_records(model_nos):
            status = self.model_status(str(row.get("模型编号")))
            records.append({
                **row,
                "当前模型状态": status,
                "计入重算": status == VALID_MODEL_STATUS and row.get("状态") == "有效",
            })
        allocations = []
        for row in self.plan_allocations(plan, current_only=False):
            status = self.model_status(str(row.get("模型编号")))
            allocations.append({
                **row,
                "当前模型状态": status,
                "来源失效": status != VALID_MODEL_STATUS and _money(row.get("渠道额度")) > 0,
            })
        return {
            "方案编号": plan.get("方案编号"),
            "业务版本": plan.get("业务版本"),
            "调用链版本": plan.get("调用链版本"),
            "关联LTV模型": model_nos,
            "模型": models,
            "用户价值记录": records,
            "渠道分配记录": allocations,
            "断点": self._breakpoints(plan, models),
        }

    def _breakpoints(self, plan: dict[str, Any], models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        breakpoints: list[dict[str, Any]] = []
        for item in models:
            if item["参与重算"]:
                continue
            for row in self.plan_allocations(plan, current_only=False):
                if row.get("模型编号") != item["模型编号"] or _money(row.get("渠道额度")) <= 0:
                    continue
                kind = "校验中模型参与额度" if item["状态"] == "模型校验中" else "废弃模型残留额度"
                breakpoints.append({
                    "数据编号": row.get("分配编号"),
                    "数据集编号": row.get("数据集编号"),
                    "断点类型": kind,
                    "模型编号": item["模型编号"],
                    "模型状态": item["状态"],
                    "渠道": row.get("渠道"),
                    "渠道额度": _money(row.get("渠道额度")),
                    "说明": (
                        f"{row.get('分配编号')} 的{row.get('渠道')}额度 "
                        f"{_money(row.get('渠道额度')):.2f} 元仍引用状态为{item['状态']}的{item['模型编号']}"
                    ),
                })
        return breakpoints

    # ---------- 计算 ----------

    def preview(self, plan: dict[str, Any]) -> dict[str, Any]:
        total = _money(plan.get("预算总额"))
        models = self.chain_models(plan)
        valid_models = [item["模型编号"] for item in models if item["参与重算"]]
        records = [
            r for r in self.value_records([m["模型编号"] for m in models])
            if r.get("模型编号") in valid_models and r.get("状态") == "有效"
        ]
        value_by_channel: dict[str, float] = {}
        record_by_channel: dict[str, dict[str, Any]] = {}
        for record in records:
            channel = str(record.get("渠道") or "未分渠道")
            value_by_channel[channel] = value_by_channel.get(channel, 0.0) + _money(record.get("用户价值"))
            record_by_channel[channel] = record

        baseline: dict[str, dict[str, Any]] = {}
        for row in self.plan_allocations(plan):
            channel = str(row.get("渠道") or "未分渠道")
            baseline[channel] = {
                "原渠道额度": _money(row.get("渠道额度")),
                "原分配编号": row.get("分配编号"),
                "原数据集版本": row.get("数据集版本"),
                "原模型编号": row.get("模型编号"),
                "原ROI偏差": row.get("ROI偏差") or "0.00%",
            }

        excluded: list[dict[str, Any]] = []
        for item in models:
            if item["参与重算"]:
                continue
            for record in self.value_records([item["模型编号"]]):
                if record.get("状态") != "有效":
                    continue
                channel = str(record.get("渠道") or "未分渠道")
                excluded.append({
                    "模型编号": item["模型编号"],
                    "模型状态": item["状态"],
                    "渠道": channel,
                    "用户分群": record.get("用户分群"),
                    "用户价值": _money(record.get("用户价值")),
                    "原渠道额度": baseline.get(channel, {}).get("原渠道额度", 0.0),
                    "原因": item["排除原因"],
                })

        channels = sorted(set(baseline) | set(value_by_channel))
        value_total = sum(value_by_channel.values())
        usable_channels = [ch for ch in channels if value_total > 0 and ch in value_by_channel]
        allocations: list[dict[str, Any]] = []
        allocated = 0.0
        for channel in channels:
            origin = baseline.get(channel, {})
            original = _money(origin.get("原渠道额度"))
            quota = 0.0
            if channel in usable_channels:
                if channel == usable_channels[-1]:
                    quota = round(total - allocated, 2)
                else:
                    quota = round(total * value_by_channel[channel] / value_total, 2)
                quota = max(quota, 0.0)
                allocated += quota
            model_no = str((record_by_channel.get(channel) or {}).get("模型编号") or "")
            allocations.append({
                "渠道": channel,
                "模型编号": model_no,
                "用户价值": round(value_by_channel.get(channel, 0.0), 2),
                "渠道额度": quota,
                "原渠道额度": original,
                "分配比例": round(quota / total * 100, 2) if total > 0 else 0.0,
                "ROI偏差": pct(quota, original),
                "来源状态": "有效" if quota > 0 else "失效",
                "原分配编号": origin.get("原分配编号"),
                "原数据集版本": origin.get("原数据集版本"),
                "原模型编号": origin.get("原模型编号"),
                "原ROI偏差": origin.get("原ROI偏差") or "0.00%",
            })

        invalid_models = [item for item in models if not item["参与重算"]]
        # 修正后的口径：所有拿到额度的渠道都由“已启用”模型支撑，且额度合计回到预算总额，
        # 才认为本轮来源有效；被排除的模型写入数据集排除清单并保留原额度作为历史。
        source_valid = (
            total > 0
            and allocated > 0
            and abs(allocated - total) <= 0.01
            and all(item["渠道"] in usable_channels for item in allocations if item["渠道额度"] > 0)
        )
        return {
            "方案编号": plan.get("方案编号"),
            "预算总额": total,
            "业务版本": self.confirm_version(plan),
            "调用链版本": plan.get("调用链版本"),
            "渠道": allocations,
            "排除模型": [
                {"模型编号": item["模型编号"], "模型状态": item["状态"], "原因": item["排除原因"]}
                for item in invalid_models
            ],
            "排除明细": excluded,
            "计入模型": valid_models,
            "渠道额度合计": round(allocated, 2),
            "来源有效": source_valid,
            "来源提示": self._source_notice(invalid_models) if invalid_models else "",
        }

    def _source_notice(self, invalid_models: list[dict[str, Any]]) -> str:
        if not invalid_models:
            return ""
        parts = [f"{item['模型编号']}（{item['状态']}）" for item in invalid_models]
        return f"LTV模型 {'、'.join(parts)} 无法支撑新的预算批准，相关渠道额度已剔除并保留原额度与ROI偏差作为历史"

    # ---------- 写入 ----------

    def _next_dataset_version(self, plan: dict[str, Any], business_version: str) -> tuple[str, str]:
        seq = len(self.plan_datasets(plan)) + 1
        existing = {str(row.get("数据集编号")) for row in store.rows("budget_dataset")}
        new_id = _next_id(store.rows("budget_dataset"))
        while f"DSET-{new_id:04d}" in existing:
            new_id += 1
        return f"{plan['方案编号']}-{business_version}-V{seq}", f"DSET-{new_id:04d}"

    def _supersede(self, plan: dict[str, Any], keep: str | None = None) -> None:
        for dataset in self.plan_datasets(plan):
            if dataset.get("数据集编号") == keep:
                continue
            if dataset.get("状态") in (DATASET_PENDING, DATASET_VALIDATED):
                dataset["状态"] = DATASET_VOID
                dataset["作废原因"] = dataset.get("作废原因") or "被同一业务记录的新结果取代"
                dataset["作废时间"] = _now()

    def _archive_current(self, plan: dict[str, Any], reason: str, *, state: str = "已归档") -> None:
        for row in self.plan_allocations(plan):
            row["历史标记"] = True
            row["生效状态"] = state
            row["归档原因"] = reason
            row["归档时间"] = _now()

    def _write_allocations(
        self,
        plan: dict[str, Any],
        dataset: dict[str, Any],
        allocations: list[dict[str, Any]],
        *,
        effective: bool,
    ) -> list[dict[str, Any]]:
        written: list[dict[str, Any]] = []
        for item in allocations:
            valid = item["来源状态"] == "有效"
            model_no = item.get("模型编号") if valid else item.get("原模型编号")
            record = _add_row("channel_alloc", "分配编号", "CHAN", {
                "数据集编号": dataset["数据集编号"],
                "数据集版本": dataset["数据集版本"],
                "方案编号": plan["方案编号"],
                "渠道": item["渠道"],
                "模型编号": model_no or "",
                "模型名称": (self.get_model(str(model_no or "")) or {}).get("模型名称") or "—",
                "模型状态": self.model_status(str(model_no)) if model_no else "—",
                "用户价值": item["用户价值"],
                "渠道额度": item["渠道额度"],
                "分配比例": item["分配比例"],
                "原渠道额度": item["原渠道额度"],
                "ROI偏差": item["ROI偏差"],
                "来源状态": item["来源状态"],
                "历史标记": not valid,
                "生效状态": ("已生效" if effective else "待复核") if valid else "已失效",
                "计算时间": _now(),
                "生效时间": _now() if effective and valid else None,
                "历史说明": "" if valid else "保留原渠道额度与ROI偏差作为历史",
                "原数据集版本": item.get("原数据集版本"),
                "原分配编号": item.get("原分配编号"),
                "原ROI偏差": item.get("原ROI偏差"),
            })
            written.append(record)
        return written

    def recalculate(
        self,
        entry_id: int | str,
        *,
        business_version: str | None = None,
        operator: str = "值班管理员",
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            material = self.business_material(plan)
            confirmed = self.confirm_version(plan)
            if business_version and confirmed and business_version != confirmed:
                return None, (
                    f"业务版本材料 {material.get('材料编号')} 确认的是 {confirmed}，"
                    f"与提交的 {business_version} 不一致"
                )
            if not confirmed:
                return None, "缺少业务版本材料确认的版本，结果版本无法落定"
            if material and material.get("材料状态") != "已确认":
                return None, f"业务版本材料 {material.get('材料编号')} 尚未确认，结果版本不能落定"
            if plan.get("status") == STATUS_APPROVED:
                return None, "方案已批准，已批准的历史分配保持原预算时点状态，不能重算"

            plan["原始关联模型"] = list(plan.get("关联LTV模型") or [])
            preview = self.preview(plan)
            self._supersede(plan)
            self._archive_current(plan, "渠道预算额度重算")

            dataset_version, dataset_no = self._next_dataset_version(plan, confirmed)
            source_valid = preview["来源有效"]
            content = {
                "方案编号": plan["方案编号"],
                "业务版本": confirmed,
                "业务版本材料": (material or {}).get("材料编号"),
                "调用链版本": plan.get("调用链版本"),
                "渠道": [
                    {"渠道": item["渠道"], "渠道额度": item["渠道额度"], "模型编号": item.get("模型编号")}
                    for item in preview["渠道"]
                ],
                "排除模型": [item["模型编号"] for item in preview["排除模型"]],
            }
            digest = _digest(content)
            dataset = _add_row("budget_dataset", "数据集编号", "DSET", {
                "方案编号": plan["方案编号"],
                "业务版本": confirmed,
                "业务版本材料": (material or {}).get("材料编号"),
                "数据集版本": dataset_version,
                "状态": DATASET_VALIDATED if source_valid else DATASET_PENDING,
                "来源状态": "有效" if source_valid else "失效",
                "条目数": len([item for item in preview["渠道"] if item["来源状态"] == "有效"]),
                "渠道额度合计": preview["渠道额度合计"],
                "模型范围": preview["计入模型"],
                "排除模型": [item["模型编号"] for item in preview["排除模型"]],
                "摘要": digest,
                "幂等键": f"{plan['方案编号']}|{confirmed}|{digest}",
                "请求编号": request_id or f"REQ-{plan['方案编号']}-R{int(plan.get('重算次数') or 0) + 1}",
                "创建时间": _now(),
                "生效时间": None,
                "调用链版本": plan.get("调用链版本"),
                "说明": "遗留调用链重算结果" if preview["排除模型"] else "按已启用模型重算",
            })
            dataset["数据集编号"] = dataset_no
            self._write_allocations(plan, dataset, preview["渠道"], effective=False)

            plan.update({
                "业务版本": confirmed,
                "数据集编号": dataset_no,
                "数据集版本": dataset_version,
                "渠道额度": preview["渠道额度合计"],
                "渠道分配比例": " / ".join(
                    f"{item['渠道']} {item['分配比例']:.2f}%" for item in preview["渠道"]
                ),
                "ROI偏差": "；".join(f"{item['渠道']} {item['ROI偏差']}" for item in preview["渠道"]),
                "来源有效": source_valid,
                "来源提示": preview["来源提示"],
                "复核状态": "待复核",
                "处理终点": None,
                "复核版本": confirmed,
                "重算次数": int(plan.get("重算次数") or 0) + 1,
                "重算时间": _now(),
                "pending": True,
                "status": STATUS_REVIEW,
                "冻结时间": None if source_valid else _now(),
                "停批原因": "" if source_valid else "价值来源失效，停止批准",
            })
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "重算渠道额度",
                "数据集编号": dataset_no,
                "数据集版本": dataset_version,
                "操作人": operator,
                "说明": (
                    ("排除模型 " + "、".join(d["模型编号"] for d in preview["排除模型"]) + "；")
                    if preview["排除模型"] else "全部模型已启用；"
                ) + "原渠道额度 " + "、".join(
                    f"{item['渠道']} {item['原渠道额度']:.2f}" for item in preview["渠道"]
                ),
                "来源失效渠道": sorted({item["渠道"] for item in preview["排除明细"]}),
            })
            message = (
                f"已生成渠道预算分配明细数据集 {dataset_no}（{dataset_version}），"
                f"渠道额度合计 {preview['渠道额度合计']:.2f}"
            )
            if preview["排除模型"]:
                message += "；排除模型 " + "、".join(d["模型编号"] for d in preview["排除模型"])
            return dataset, message

    def accept_dataset(
        self,
        entry_id: int | str,
        values: dict[str, Any],
        *,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        """接纳上传的渠道预算分配明细数据集；只有来源有效的数据集能成为本轮有效结果。"""
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            if plan.get("status") in (STATUS_APPROVED, STATUS_FROZEN):
                return None, f"方案当前为{plan.get('status')}，不能接纳新的渠道预算分配明细数据集"

            confirmed = self.confirm_version(plan)
            business_version = str(values.get("业务版本") or confirmed)
            if confirmed and business_version != confirmed:
                return None, f"业务版本材料确认的是 {confirmed}，不能用 {business_version} 落定结果版本"

            items = values.get("渠道") or values.get("items") or []
            if not isinstance(items, list) or not items:
                return None, "数据集缺少渠道条目，不能接纳"
            total = _money(plan.get("预算总额"))
            quota_sum = 0.0
            model_range: list[str] = []
            problems: list[str] = []
            normalized_items: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    problems.append("渠道条目格式不正确")
                    continue
                model_no = str(item.get("模型编号") or "").strip()
                channel = str(item.get("渠道") or "").strip()
                quota = _money(item.get("渠道额度"))
                if not channel:
                    problems.append("渠道名称缺失")
                if not model_no:
                    problems.append(f"{channel or '未知渠道'}缺少LTV模型编号")
                else:
                    status = self.model_status(model_no)
                    if status != VALID_MODEL_STATUS:
                        problems.append(f"{channel}引用的{model_no}当前为{status}，来源失效")
                    model_range.append(model_no)
                if quota < 0:
                    problems.append(f"{channel}渠道额度不能为负")
                quota_sum += quota
                normalized_items.append({
                    "渠道": channel,
                    "模型编号": model_no,
                    "渠道额度": quota,
                    "用户价值": _money(item.get("用户价值")),
                    "原渠道额度": _money(item.get("原渠道额度")),
                })
            if abs(quota_sum - total) > 0.01:
                problems.append(f"渠道额度合计 {quota_sum:.2f} 与预算总额 {total:.2f} 不一致")
            if problems:
                return None, "；".join(problems)

            digest = _digest({
                "方案编号": plan["方案编号"],
                "业务版本": business_version,
                "条目": normalized_items,
            })
            idempotency_key = str(values.get("幂等键") or f"{plan['方案编号']}|{business_version}|{digest}")
            for dataset in self.plan_datasets(plan):
                if dataset.get("幂等键") == idempotency_key:
                    return dataset, f"幂等命中，本轮有效结果仍为 {dataset.get('数据集编号')}"

            current = self.current_dataset(plan)
            if (
                current
                and current.get("状态") in (DATASET_VALIDATED, DATASET_EFFECTIVE)
                and current.get("来源状态") == "有效"
                and plan.get("来源有效")
            ):
                return None, (
                    f"同一业务记录已存在有效结果 {current.get('数据集编号')}，"
                    "并发请求只接纳一个有效数据集，请先复核该版本"
                )

            self._supersede(plan)
            self._archive_current(plan, "接纳上传数据集")
            dataset_version, dataset_no = self._next_dataset_version(plan, business_version)
            dataset = _add_row("budget_dataset", "数据集编号", "DSET", {
                "方案编号": plan["方案编号"],
                "业务版本": business_version,
                "业务版本材料": (self.business_material(plan) or {}).get("材料编号"),
                "数据集版本": dataset_version,
                "状态": DATASET_VALIDATED,
                "来源状态": "有效",
                "条目数": len(normalized_items),
                "渠道额度合计": round(quota_sum, 2),
                "模型范围": sorted(set(model_range)),
                "排除模型": [],
                "摘要": digest,
                "幂等键": idempotency_key,
                "请求编号": str(values.get("请求编号") or f"REQ-{plan['方案编号']}-U{_next_id(store.rows('budget_dataset'))}"),
                "创建时间": _now(),
                "生效时间": None,
                "调用链版本": plan.get("调用链版本"),
                "说明": "上传并经来源校验的渠道预算分配明细数据集",
            })
            dataset["数据集编号"] = dataset_no
            for item in normalized_items:
                _add_row("channel_alloc", "分配编号", "CHAN", {
                    "数据集编号": dataset_no,
                    "数据集版本": dataset_version,
                    "方案编号": plan["方案编号"],
                    "渠道": item["渠道"],
                    "模型编号": item["模型编号"],
                    "模型名称": (self.get_model(item["模型编号"]) or {}).get("模型名称") or "—",
                    "模型状态": self.model_status(item["模型编号"]),
                    "用户价值": item["用户价值"],
                    "渠道额度": item["渠道额度"],
                    "分配比例": round(item["渠道额度"] / total * 100, 2) if total else 0.0,
                    "原渠道额度": item["原渠道额度"],
                    "ROI偏差": pct(item["渠道额度"], item["原渠道额度"]),
                    "来源状态": "有效",
                    "历史标记": False,
                    "生效状态": "待复核",
                    "计算时间": _now(),
                    "生效时间": None,
                })
            plan.update({
                "数据集编号": dataset_no,
                "数据集版本": dataset_version,
                "业务版本": business_version,
                "渠道额度": round(quota_sum, 2),
                "复核状态": "待复核",
                "来源有效": True,
                "来源提示": "",
                "处理终点": None,
                "pending": True,
                "status": STATUS_REVIEW,
                "停批原因": "",
            })
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "接纳渠道预算分配明细数据集",
                "数据集编号": dataset_no,
                "数据集版本": dataset_version,
                "操作人": operator,
                "说明": "仅接纳来源有效的数据集",
            })
            return dataset, f"数据集 {dataset_no}（{dataset_version}）已接纳为本轮有效结果"

    # ---------- 终态裁决 ----------

    def _record_conflict(self, plan: dict[str, Any], request: str, result: str, note: str) -> None:
        plan.setdefault("冲突处理", []).append({
            "时间": _now(),
            "请求": request,
            "结果": result,
            "说明": note,
        })
        plan.setdefault("历史", []).append({
            "时间": _now(),
            "动作": request,
            "数据集编号": plan.get("数据集编号"),
            "数据集版本": plan.get("数据集版本"),
            "说明": f"{result}：{note}",
        })

    def approve(self, entry_id: int | str, *, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            return self._approve_locked(plan, operator=operator)

    def _approve_locked(self, plan: dict[str, Any], *, operator: str) -> tuple[dict[str, Any] | None, str]:
        if plan.get("处理终点") == TERMINAL_APPROVE:
            self._record_conflict(plan, "预算批准请求", "未生效", "方案已批准，重复批准不生效")
            return None, "方案已批准，重复批准不生效"
        if plan.get("处理终点"):
            self._record_conflict(plan, "预算批准请求", "未生效", f"本轮已由「{plan['处理终点']}」落定")
            return None, f"本轮已由「{plan['处理终点']}」落定，批准请求不生效"
        if plan.get("status") == STATUS_FROZEN:
            return None, "方案已冻结待复核，不能批准"
        if plan.get("status") not in (STATUS_REVIEW, STATUS_DRAFT):
            return None, f"方案当前为{plan.get('status')}，不在可批准状态"

        self._reconcile(plan)
        dataset = self.current_dataset(plan)
        if dataset is None:
            return None, "方案缺少渠道预算分配明细数据集，不能批准"
        if dataset.get("状态") not in (DATASET_VALIDATED, DATASET_EFFECTIVE):
            return None, f"数据集 {dataset.get('数据集编号')} 状态为{dataset.get('状态')}，不能批准"
        if not plan.get("来源有效") or dataset.get("来源状态") != "有效":
            return None, f"价值来源失效，停止批准：{plan.get('来源提示') or '渠道额度引用了不可用模型'}"
        invalid_models = sorted({
            str(row.get("模型编号"))
            for row in self.plan_allocations(plan)
            if self.model_status(str(row.get("模型编号"))) != VALID_MODEL_STATUS
        })
        if invalid_models:
            return None, f"渠道额度仍引用不可用模型 {'、'.join(invalid_models)}，停止批准"
        if not self.plan_allocations(plan):
            return None, "方案没有生效的渠道额度，不能批准"

        plan.update({
            "status": STATUS_APPROVED,
            "处理终点": TERMINAL_APPROVE,
            "复核状态": "已批准",
            "pending": False,
            "批准次数": int(plan.get("批准次数") or 0) + 1,
            "批准时点": _now(),
            "生效时间": _now(),
            "停批原因": "",
        })
        dataset["状态"] = DATASET_EFFECTIVE
        dataset["生效时间"] = _now()
        for row in self.plan_allocations(plan):
            row["生效状态"] = "已生效"
            row["生效时间"] = _now()
        plan.setdefault("历史", []).append({
            "时间": _now(),
            "动作": "预算批准",
            "数据集编号": dataset.get("数据集编号"),
            "数据集版本": dataset.get("数据集版本"),
            "操作人": operator,
            "说明": f"渠道额度 {_money(plan.get('渠道额度')):.2f} 元",
        })
        return plan, f"方案已批准，生效数据集 {dataset.get('数据集编号')}"

    def freeze(
        self,
        entry_id: int | str,
        *,
        reason: str = "",
        source: str = TERMINAL_FREEZE,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            if plan.get("status") == STATUS_APPROVED:
                self._record_conflict(plan, source, "未生效", "方案已批准，已批准的历史分配保持原预算时点状态")
                return None, "方案已批准，冻结请求不生效；原预算时点状态保持不变"
            if plan.get("处理终点"):
                self._record_conflict(plan, source, "未生效", f"本轮已由「{plan['处理终点']}」落定")
                return None, f"本轮已由「{plan['处理终点']}」落定，冻结请求不生效"
            plan.update({
                "status": STATUS_FROZEN,
                "处理终点": source,
                "复核状态": "待复核",
                "pending": True,
                "冻结时间": _now(),
                "停批原因": reason or "预算待复核，暂停批准",
            })
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": source,
                "数据集编号": plan.get("数据集编号"),
                "数据集版本": plan.get("数据集版本"),
                "操作人": operator,
                "说明": reason or "冻结预算待复核",
            })
            return plan, f"方案已冻结待复核（处理终点：{source}）"

    def reopen(self, entry_id: int | str, *, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            if plan.get("status") == STATUS_APPROVED:
                return None, "方案已批准，已批准的历史分配保持原预算时点状态，不能重新评审"
            plan.update({
                "status": STATUS_REVIEW,
                "处理终点": None,
                "复核状态": "待复核",
                "pending": True,
                "冻结时间": None,
            })
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "提交方案评审",
                "数据集编号": plan.get("数据集编号"),
                "数据集版本": plan.get("数据集版本"),
                "操作人": operator,
                "说明": "开启新一轮复核，历史结果保留",
            })
            return plan, "方案已进入新一轮评审"

    def ltv_receipt(self, entry_id: int | str, *, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        """LTV下线回执：与预算批准请求竞争同一方案时，先落定者决定本轮处理终点。"""
        return self.freeze(
            entry_id,
            reason="收到LTV下线回执，预算方案先冻结待复核",
            source=TERMINAL_RECEIPT,
            operator=operator,
        )

    def replace_value_version(
        self,
        entry_id: int | str,
        values: dict[str, Any],
        *,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        """把方案替换到新的LTV价值版本并重算；已批准方案保持原预算时点状态，只生成新版本。"""
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            model_no = str(values.get("模型编号") or "").strip()
            if not model_no:
                return None, "替换价值版本需要指定新的LTV模型编号"
            model = self.get_model(model_no)
            if model is None:
                return None, f"LTV模型 {model_no} 不存在或已归档"
            if model.get("status") != VALID_MODEL_STATUS:
                return None, f"LTV模型 {model_no} 当前为{model.get('status')}，不能作为新的价值版本"
            if plan.get("status") == STATUS_APPROVED:
                self._record_conflict(plan, "替换为新价值版本", "未生效", "方案已批准，原预算时点状态保持不变")
                return None, "方案已批准，已批准的历史分配保持原预算时点状态，替换请求不生效"

            old_models = [m for m in (plan.get("关联LTV模型") or [])]
            current_version = str(plan.get("业务版本") or "")
            business_version = str(values.get("业务版本") or current_version)
            if current_version and business_version != current_version:
                return None, f"方案业务版本为 {current_version}，替换价值版本不能改业务版本"

            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "替换为新价值版本",
                "数据集编号": plan.get("数据集编号"),
                "数据集版本": plan.get("数据集版本"),
                "操作人": operator,
                "说明": f"价值来源 {('、'.join(old_models)) or '—'} → {model_no}",
            })
            # 新模型替换失效来源；其他仍然可用的渠道模型保留，避免误伤已复核的额度。
            keep_models = [
                str(row.get("模型编号"))
                for row in self.plan_allocations(plan)
                if row.get("模型编号")
                and str(row.get("模型编号")) != model_no
                and self.model_status(str(row.get("模型编号"))) == VALID_MODEL_STATUS
            ]
            plan["替换前模型"] = old_models
            plan["关联LTV模型"] = list(dict.fromkeys(keep_models + [model_no]))
            plan["模型版本"] = model.get("模型版本")
            plan["价值记录版本"] = model.get("价值记录版本")
            dataset, message = self.recalculate(
                plan["方案编号"],
                business_version=business_version or None,
                operator=operator,
            )
            if dataset is None:
                plan["关联LTV模型"] = old_models
                return None, message
            plan["处理终点"] = None
            return plan, "已替换为新价值版本并重算：" + message

    # ---------- 打包与文件接收 ----------

    def package(
        self,
        entry_id: int | str,
        *,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        """把本轮生效的渠道预算分配明细数据集打包成可接收的交付包。"""
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            dataset = self.current_dataset(plan)
            if dataset is None:
                return None, "方案没有可打包的数据集"
            if dataset.get("状态") not in (DATASET_VALIDATED, DATASET_EFFECTIVE):
                return None, f"数据集 {dataset.get('数据集编号')} 状态为{dataset.get('状态')}，不能打包"
            if dataset.get("来源状态") != "有效":
                return None, f"数据集 {dataset.get('数据集编号')} 来源失效，不能打包"
            rows = [row for row in store.rows("channel_alloc")
                    if row.get("数据集编号") == dataset.get("数据集编号") and not row.get("历史标记")]
            payload = {
                "方案编号": plan["方案编号"],
                "数据集编号": dataset["数据集编号"],
                "数据集版本": dataset["数据集版本"],
                "业务版本": dataset.get("业务版本"),
                "条目": [
                    {
                        "分配编号": row.get("分配编号"),
                        "渠道": row.get("渠道"),
                        "模型编号": row.get("模型编号"),
                        "渠道额度": _money(row.get("渠道额度")),
                        "ROI偏差": row.get("ROI偏差"),
                    }
                    for row in rows
                ],
            }
            package_no = f"PKG-{_next_id(store.rows('budget_package')):04d}"
            record = _add_row("budget_package", "交付包编号", "PKG", {
                "方案编号": plan["方案编号"],
                "数据集编号": dataset["数据集编号"],
                "数据集版本": dataset["数据集版本"],
                "业务版本": dataset.get("业务版本"),
                "条目数": len(rows),
                "渠道额度合计": round(sum(_money(row.get("渠道额度")) for row in rows), 2),
                "内容摘要": _digest(payload),
                "内容": payload,
                "状态": "已打包",
                "创建时间": _now(),
            })
            record["交付包编号"] = package_no
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "打包渠道预算分配明细数据集",
                "数据集编号": dataset["数据集编号"],
                "数据集版本": dataset["数据集版本"],
                "操作人": operator,
                "说明": f"交付包 {package_no}",
            })
            if plan.get("status") != STATUS_APPROVED:
                plan["停批原因"] = plan.get("停批原因") or "等待文件接收确认"
                plan["复核状态"] = "待复核"
            plan["pending"] = True
            plan["打包编号"] = package_no
            return record, f"数据集 {dataset['数据集编号']} 已打包为 {package_no}，等待文件接收"

    def receive_file(
        self,
        entry_id: int | str,
        values: dict[str, Any],
        *,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        """接收打包文件：只认摘要一致、来源有效的交付包，接收后本轮结果才落定为批准。"""
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            package_no = str(values.get("交付包编号") or "")
            package = None
            for row in store.rows("budget_package"):
                if row.get("交付包编号") == package_no and row.get("方案编号") == plan.get("方案编号"):
                    package = row
                    break
            if package is None:
                return None, f"交付包 {package_no} 不存在或不属于方案 {plan.get('方案编号')}"
            if package.get("状态") == "已接收":
                return None, f"交付包 {package_no} 已接收，重复接收不产生新的处理终点"
            if plan.get("status") == STATUS_APPROVED:
                self._record_conflict(plan, "文件接收", "未生效", "方案已批准，重复接收不生效")
                return None, "方案已批准，文件接收请求不生效"
            receipt_digest = str(values.get("内容摘要") or "")
            if receipt_digest and receipt_digest != package.get("内容摘要"):
                return None, "文件摘要与打包内容不一致，接收失败并保持原状态"
            dataset = None
            for row in self.plan_datasets(plan):
                if row.get("数据集编号") == package.get("数据集编号"):
                    dataset = row
                    break
            if dataset is None or dataset.get("来源状态") != "有效":
                package["状态"] = "接收失败"
                package["失败原因"] = "交付包来源失效"
                return None, "交付包来源失效，文件接收失败并停止批准"
            if plan.get("处理终点"):
                self._record_conflict(plan, "文件接收", "未生效", f"本轮已由「{plan['处理终点']}」落定")
                return None, f"本轮已由「{plan['处理终点']}」落定，文件接收请求不生效"
            if plan.get("来源有效") is False:
                package["状态"] = "接收失败"
                package["失败原因"] = str(plan.get("来源提示"))
                return None, f"方案价值来源失效，文件接收失败：{plan.get('来源提示')}"

            result = self._approve_locked(plan, operator=operator)
            if result[0] is None:
                package["状态"] = "接收失败"
                package["失败原因"] = result[1]
                return None, result[1]
            package["状态"] = "已接收"
            package["接收时间"] = _now()
            package["接收人"] = operator
            receipt_no = f"RCPT-{_next_id(store.rows('budget_receipt')):04d}"
            plan["接收编号"] = receipt_no
            record = _add_row("budget_receipt", "接收编号", "RCPT", {
                "方案编号": plan["方案编号"],
                "交付包编号": package_no,
                "数据集编号": package.get("数据集编号"),
                "数据集版本": package.get("数据集版本"),
                "内容摘要": package.get("内容摘要"),
                "状态": "已接收",
                "接收时间": _now(),
                "接收人": operator,
            })
            record["接收编号"] = receipt_no
            return package, f"交付包 {package_no} 已接收，方案批准结果落定（{receipt_no}）"

    # ---------- 投递待处理 ----------

    def deliver(
        self,
        entry_id: int | str,
        *,
        dataset_no: str | None = None,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        with _decision_lock:
            plan = self.get_plan(entry_id)
            if plan is None:
                return None, f"预算方案 {entry_id} 不存在或已归档"
            target = dataset_no or plan.get("数据集编号")
            dataset = None
            for row in self.plan_datasets(plan):
                if row.get("数据集编号") == target:
                    dataset = row
                    break
            if dataset is None:
                return None, f"预算方案 {entry_id} 没有可投递的数据集"
            if dataset.get("状态") not in (DATASET_VALIDATED, DATASET_EFFECTIVE) or dataset.get("来源状态") != "有效":
                return None, f"数据集 {dataset.get('数据集编号')} 来源失效或已作废，不能投递LTV估算待处理明细"
            if not plan.get("来源有效"):
                return None, f"方案价值来源失效，停止投递：{plan.get('来源提示')}"

            for row in store.rows("ltv_pending"):
                if row.get("数据集版本") == dataset.get("数据集版本"):
                    return row, f"数据集 {dataset.get('数据集编号')} 已在LTV估算待处理明细中，幂等命中"

            item = _add_row("ltv_pending", "待处理编号", "PEND", {
                "方案编号": plan["方案编号"],
                "数据集编号": dataset["数据集编号"],
                "数据集版本": dataset["数据集版本"],
                "业务版本": dataset.get("业务版本"),
                "调用链版本": dataset.get("调用链版本") or plan.get("调用链版本"),
                "模型范围": list(dataset.get("模型范围") or []),
                "排除模型": list(dataset.get("排除模型") or []),
                "渠道额度合计": _money(dataset.get("渠道额度合计")),
                "条目数": int(dataset.get("条目数") or 0),
                "状态": PENDING_OPEN,
                "pending": True,
                "abnormal": False,
                "投递人": operator,
                "投递时间": _now(),
                "说明": "渠道预算分配明细数据集投递到LTV估算待处理明细",
            })
            plan.setdefault("历史", []).append({
                "时间": _now(),
                "动作": "投递LTV估算待处理明细",
                "数据集编号": dataset["数据集编号"],
                "数据集版本": dataset["数据集版本"],
                "操作人": operator,
                "说明": f"待处理编号 {item['待处理编号']}",
            })
            return item, f"数据集 {dataset['数据集编号']} 已投递到LTV估算待处理明细（{item['待处理编号']}）"

    def pending_items(self) -> list[dict[str, Any]]:
        return sorted(
            store.rows("ltv_pending"),
            key=lambda row: (int(row.get("id", 0)), str(row.get("待处理编号") or "")),
        )

    def archive_pending(self, pending_id: int | str, *, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        with _decision_lock:
            for row in store.rows("ltv_pending"):
                if str(row.get("id")) == str(pending_id) or row.get("待处理编号") == str(pending_id):
                    if row.get("状态") != PENDING_OPEN:
                        return None, f"待处理记录 {row.get('待处理编号')} 已归档，无需重复处理"
                    row["状态"] = PENDING_ARCHIVED
                    row["pending"] = False
                    row["归档时间"] = _now()
                    row["归档人"] = operator
                    row["说明"] = "来源失效，停止批准，保留原渠道额度与ROI偏差作为历史"
                    plan = self.get_plan(str(row.get("方案编号")))
                    if plan is not None:
                        plan.setdefault("历史", []).append({
                            "时间": _now(),
                            "动作": "LTV估算待处理明细归档",
                            "数据集编号": row.get("数据集编号"),
                            "数据集版本": row.get("数据集版本"),
                            "操作人": operator,
                            "说明": row["说明"],
                        })
                    return row, f"待处理记录 {row.get('待处理编号')} 已归档，保留历史渠道额度与ROI偏差"
            return None, f"待处理记录 {pending_id} 不存在"

    # ---------- 状态传播 ----------

    def model_usage_map(self, model_nos: list[str]) -> dict[str, list[dict[str, Any]]]:
        """列表页用：汇总每个LTV模型被哪些预算方案引用、方案来源是否有效。"""
        wanted = set(model_nos)
        plans = {str(plan.get("方案编号")): plan for plan in store.rows("budget")}
        usage: dict[str, dict[str, dict[str, Any]]] = {
            model_no: {} for model_no in wanted
        }
        for row in store.rows("channel_alloc"):
            model_no = str(row.get("模型编号") or "")
            if model_no not in wanted:
                continue
            plan_no = str(row.get("方案编号") or "")
            plan = plans.get(plan_no) or {}
            item = usage[model_no].setdefault(plan_no, {
                "方案编号": plan_no,
                "status": plan.get("status"),
                "复核状态": plan.get("复核状态"),
                "数据集编号": plan.get("数据集编号"),
                "数据集版本": plan.get("数据集版本"),
                "来源有效": bool(plan.get("来源有效")),
                "来源提示": plan.get("来源提示") or "",
                "渠道额度": [],
            })
            item["渠道额度"].append({
                "分配编号": row.get("分配编号"),
                "数据集编号": row.get("数据集编号"),
                "渠道": row.get("渠道"),
                "渠道额度": _money(row.get("渠道额度")),
                "原渠道额度": _money(row.get("原渠道额度")),
                "ROI偏差": row.get("ROI偏差"),
                "来源状态": row.get("来源状态"),
                "历史标记": bool(row.get("历史标记")),
                "生效状态": row.get("生效状态"),
            })
        return {model_no: list(items.values()) for model_no, items in usage.items()}

    def _reconcile(self, plan: dict[str, Any]) -> None:
        """按当前LTV模型状态重算方案来源有效性，不做任何静默放行。"""
        rows = self.plan_allocations(plan)
        invalid = sorted({
            str(row.get("模型编号"))
            for row in rows
            if self.model_status(str(row.get("模型编号"))) != VALID_MODEL_STATUS
        })
        dataset = self.current_dataset(plan)
        dataset_ok = bool(
            dataset
            and dataset.get("状态") in (DATASET_VALIDATED, DATASET_EFFECTIVE)
            and dataset.get("来源状态") == "有效"
        )
        if invalid:
            plan["来源有效"] = False
            plan["来源提示"] = f"渠道额度仍引用不可用模型 {'、'.join(invalid)}"
            plan["停批原因"] = "价值来源失效，停止批准"
            return
        if dataset_ok:
            plan["来源有效"] = True
            plan["来源提示"] = ""
            if plan.get("status") != STATUS_APPROVED:
                plan["停批原因"] = ""
        else:
            plan["来源有效"] = False
            plan["来源提示"] = "当前渠道预算分配明细数据集不可用于批准"

    def on_model_state_changed(self, model_no: str, new_status: str, *, action: str) -> list[dict[str, Any]]:
        """LTV模型状态变化时，同批落定引用该模型的渠道额度、方案与数据集状态。"""
        with _decision_lock:
            changed_plans: list[dict[str, Any]] = []
            model = self.get_model(model_no)
            for row in store.rows("channel_alloc"):
                if row.get("模型编号") != model_no:
                    continue
                row["当前模型状态"] = new_status
                if row.get("历史标记"):
                    continue
                row["模型状态"] = new_status
                if new_status != VALID_MODEL_STATUS and _money(row.get("渠道额度")) > 0:
                    row["来源状态"] = "失效"
                    if row.get("生效状态") == "已生效":
                        row["生效状态"] = "已失效"

            for plan in store.rows("budget"):
                linked = model_no in (plan.get("关联LTV模型") or [])
                referenced = any(
                    row.get("模型编号") == model_no and row.get("方案编号") == plan.get("方案编号")
                    for row in store.rows("channel_alloc")
                )
                if not linked and not referenced:
                    continue
                plan.setdefault("历史", []).append({
                    "时间": _now(),
                    "动作": f"LTV模型状态变更（{action}）",
                    "数据集编号": plan.get("数据集编号"),
                    "数据集版本": plan.get("数据集版本"),
                    "说明": f"{model_no} → {new_status}",
                })
                if new_status == VALID_MODEL_STATUS:
                    self._reconcile(plan)
                    changed_plans.append(plan)
                    continue
                has_quota = any(
                    _money(row.get("渠道额度")) > 0 and row.get("模型编号") == model_no
                    for row in self.plan_allocations(plan)
                )
                if not has_quota:
                    # 当前额度已经不引用该模型，只需刷新方案与数据集的可批状态。
                    dataset = self.current_dataset(plan)
                    if dataset and dataset.get("状态") in (DATASET_PENDING, DATASET_VALIDATED, DATASET_EFFECTIVE):
                        dataset["来源状态"] = "失效"
                        dataset["失效模型"] = model_no
                        dataset["失效时间"] = _now()
                        dataset["说明"] = f"LTV模型 {model_no} 状态为{new_status}，数据集来源失效"
                    self._reconcile(plan)
                    changed_plans.append(plan)
                    continue

                affected_channels = sorted({
                    str(row.get("渠道"))
                    for row in self.plan_allocations(plan)
                    if row.get("模型编号") == model_no and _money(row.get("渠道额度")) > 0
                })
                snapshot = [
                    {
                        "渠道": row.get("渠道"),
                        "模型编号": row.get("模型编号"),
                        "模型名称": row.get("模型名称"),
                        "用户价值": row.get("用户价值"),
                        "渠道额度": _money(row.get("渠道额度")),
                        "分配比例": row.get("分配比例"),
                        "ROI偏差": row.get("ROI偏差"),
                        "分配编号": row.get("分配编号"),
                    }
                    for row in self.plan_allocations(plan)
                ]
                self._archive_current(
                    plan,
                    f"LTV模型 {model_no} 状态变为{new_status}",
                    state="已失效" if plan.get("status") == STATUS_APPROVED else "已归档",
                )
                remaining = 0.0
                for item in snapshot:
                    channel = str(item["渠道"])
                    if channel not in affected_channels:
                        # 其他渠道额度不受该模型影响，按原额度保留，等待本轮复核。
                        _add_row("channel_alloc", "分配编号", "CHAN", {
                            "数据集编号": plan.get("数据集编号"),
                            "数据集版本": plan.get("数据集版本"),
                            "方案编号": plan["方案编号"],
                            "渠道": channel,
                            "模型编号": item["模型编号"],
                            "模型名称": item["模型名称"],
                            "模型状态": self.model_status(str(item["模型编号"])),
                            "用户价值": item["用户价值"],
                            "渠道额度": item["渠道额度"],
                            "分配比例": item["分配比例"],
                            "原渠道额度": item["渠道额度"],
                            "ROI偏差": item["ROI偏差"],
                            "来源状态": "有效",
                            "历史标记": False,
                            "生效状态": "待复核",
                            "计算时间": _now(),
                            "生效时间": None,
                            "历史说明": "同一方案的其他渠道额度，随本轮复核保留",
                        })
                        remaining += item["渠道额度"]
                        continue
                    _add_row("channel_alloc", "分配编号", "CHAN", {
                        "数据集编号": plan.get("数据集编号"),
                        "数据集版本": plan.get("数据集版本"),
                        "方案编号": plan["方案编号"],
                        "渠道": channel,
                        "模型编号": model_no,
                        "模型名称": (model or {}).get("模型名称") or "—",
                        "模型状态": new_status,
                        "用户价值": 0.0,
                        "渠道额度": 0.0,
                        "分配比例": 0.0,
                        "原渠道额度": item["渠道额度"],
                        "ROI偏差": pct(0.0, item["渠道额度"]),
                        "来源状态": "失效",
                        "历史标记": False,
                        "生效状态": "已失效",
                        "计算时间": _now(),
                        "生效时间": None,
                        "历史说明": "原渠道额度与ROI偏差保留在归档记录中",
                    })
                dataset = self.current_dataset(plan)
                if dataset and dataset.get("状态") in (DATASET_PENDING, DATASET_VALIDATED, DATASET_EFFECTIVE):
                    dataset["来源状态"] = "失效"
                    dataset["失效模型"] = model_no
                    dataset["失效时间"] = _now()
                    dataset["说明"] = f"LTV模型 {model_no} 状态为{new_status}，数据集来源失效"
                plan["渠道额度"] = round(remaining, 2)
                plan["来源有效"] = False
                plan["来源提示"] = f"LTV模型 {model_no} 状态为{new_status}，渠道额度停止引用该模型"
                plan["停批原因"] = f"LTV模型 {model_no} 状态为{new_status}，不能继续支撑预算批准"
                plan.setdefault("历史", []).append({
                    "时间": _now(),
                    "动作": "渠道额度停止引用废弃模型",
                    "数据集编号": plan.get("数据集编号"),
                    "数据集版本": plan.get("数据集版本"),
                    "说明": (
                        f"受影响渠道：{'、'.join(affected_channels)}；原渠道额度与ROI偏差保留为历史，"
                        f"当前额度 {remaining:.2f} 元；"
                    ) + (
                        "已批准的历史分配保持原预算时点状态"
                        if plan.get("status") == STATUS_APPROVED
                        else "方案冻结待复核"
                    ),
                })
                if plan.get("status") == STATUS_APPROVED:
                    # 已批准的历史分配保持批准时点状态，只把当前额度置零并保留历史。
                    plan["复核状态"] = "来源失效待复核"
                    plan["pending"] = True
                else:
                    plan["status"] = STATUS_FROZEN
                    plan["处理终点"] = None
                    plan["冻结时间"] = plan.get("冻结时间") or _now()
                    plan["复核状态"] = "来源失效待复核"
                    plan["pending"] = True
                changed_plans.append(plan)
            return changed_plans

    # ---------- 核对 ----------

    def normalize_dataset(self, dataset: dict[str, Any]) -> dict[str, Any]:
        """把历史数据集按现行口径补齐字段，保证新旧数据集能同一口径核对。"""
        rows = sorted(
            (row for row in store.rows("channel_alloc") if row.get("数据集编号") == dataset.get("数据集编号")),
            key=lambda row: int(row.get("id", 0)),
        )
        missing = not dataset.get("条目数") or not dataset.get("模型范围")
        derived_quota = round(sum(_money(row.get("渠道额度")) for row in rows), 2)
        derived_models = sorted({str(row.get("模型编号")) for row in rows if row.get("模型编号")})
        normalized = dict(dataset)
        normalized["条目数"] = int(dataset.get("条目数") or len(rows))
        normalized["渠道额度合计"] = _money(dataset.get("渠道额度合计") or derived_quota)
        normalized["模型范围"] = list(dataset.get("模型范围") or derived_models)
        legacy = missing or str(dataset.get("幂等键") or "").startswith("LEGACY")
        normalized["核对口径"] = LEGACY_EVIDENCE if legacy else CURRENT_EVIDENCE
        normalized["渠道条目"] = [
            {
                "分配编号": row.get("分配编号"),
                "渠道": row.get("渠道"),
                "模型编号": row.get("模型编号"),
                "模型状态": row.get("模型状态"),
                "渠道额度": _money(row.get("渠道额度")),
                "原渠道额度": _money(row.get("原渠道额度")),
                "ROI偏差": row.get("ROI偏差"),
                "来源状态": row.get("来源状态"),
                "历史标记": bool(row.get("历史标记")),
            }
            for row in rows
        ]
        return normalized

    def audit(self, entry_id: int | str, *, chain_version: str | None = None) -> dict[str, Any] | None:
        plan = self.get_plan(entry_id)
        if plan is None:
            return None
        material = self.business_material(plan)
        version = chain_version or str(plan.get("调用链版本") or "")
        datasets = [self.normalize_dataset(row) for row in self.plan_datasets(plan)]
        legacy = [
            row for row in datasets
            if str(row.get("幂等键") or "").startswith("LEGACY") or row.get("核对口径") == LEGACY_EVIDENCE
        ]
        legacy_in_chain = [row for row in legacy if row.get("调用链版本") == version] or legacy
        chain = self.value_chain(plan)

        breakpoints: list[dict[str, Any]] = list(chain["断点"])
        for dataset in legacy_in_chain:
            for item in dataset["渠道条目"]:
                status = self.model_status(str(item.get("模型编号")))
                if status != VALID_MODEL_STATUS and _money(item.get("渠道额度")) > 0:
                    breakpoints.append({
                        "数据编号": item.get("分配编号"),
                        "数据集编号": dataset.get("数据集编号"),
                        "断点类型": "历史数据集残留",
                        "模型编号": item.get("模型编号"),
                        "模型状态": status,
                        "渠道": item.get("渠道"),
                        "渠道额度": _money(item.get("渠道额度")),
                        "说明": (
                            f"历史数据集 {dataset.get('数据集编号')} 在调用链 {version} 下仍为"
                            f"{item.get('模型编号')}保留 {_money(item.get('渠道额度')):.2f} 元额度"
                        ),
                    })

        pending_breakpoints = [b for b in breakpoints if b["断点类型"] == "校验中模型参与额度"]
        deprecated_breakpoints = [b for b in breakpoints if "废弃" in b["断点类型"]]
        checks = [
            {
                "检查项": "校验中模型参与预算计算",
                "数据编号": " / ".join(sorted({str(b["数据编号"]) for b in pending_breakpoints})) or "无",
                "结果": "已复现" if pending_breakpoints else "通过",
                "说明": (
                    "LIFE-0002 处于模型校验中，历史数据集仍为其保留渠道额度"
                    if pending_breakpoints else "当前链路没有校验中模型参与额度"
                ),
            },
            {
                "检查项": "废弃模型残留渠道额度",
                "数据编号": " / ".join(sorted({str(b["数据编号"]) for b in deprecated_breakpoints})) or "无",
                "结果": "已复现" if deprecated_breakpoints else "通过",
                "说明": (
                    "废弃模型的历史分配按数据编号可复查，当前额度不得继续引用"
                    if deprecated_breakpoints else "当前没有废弃模型残留额度"
                ),
            },
            {
                "检查项": "同一预算记录只有一个处理终点",
                "数据编号": plan.get("数据集编号") or "无",
                "结果": "通过" if plan.get("处理终点") != TERMINAL_APPROVE or int(plan.get("批准次数") or 0) <= 1 else "不通过",
                "说明": f"当前处理终点：{plan.get('处理终点') or '未落定'}；批准次数 {int(plan.get('批准次数') or 0)}",
            },
            {
                "检查项": "重复批准",
                "数据编号": plan.get("数据集编号") or "无",
                "结果": "通过" if int(plan.get("批准次数") or 0) <= 1 else "不通过",
                "说明": f"批准次数 {int(plan.get('批准次数') or 0)}，批准时点 {plan.get('批准时点') or '—'}",
            },
            {
                "检查项": "失败回退",
                "数据编号": plan.get("数据集编号") or "无",
                "结果": "通过" if plan.get("来源有效") or plan.get("status") in (STATUS_FROZEN, STATUS_APPROVED) else "需复核",
                "说明": "来源失效时保持原状态并冻结待复核，不产生半截写入",
            },
            {
                "检查项": "生效数据集模型均可用",
                "数据编号": plan.get("数据集编号") or "无",
                "结果": "通过" if plan.get("来源有效") else "来源失效",
                "说明": "当前数据集：" + ("、".join(
                    f"{row.get('渠道')} {_money(row.get('渠道额度')):.2f}"
                    f"（{row.get('模型编号')}，{row.get('来源状态')}）"
                    for row in self.plan_allocations(plan)
                ) or "无"),
            },
        ]
        model_nos = list(chain["关联LTV模型"])
        allocation_rows = []
        for row in chain["渠道分配记录"]:
            allocation_rows.append({
                "数据编号": row.get("分配编号"),
                "数据集编号": row.get("数据集编号"),
                "渠道": row.get("渠道"),
                "模型编号": row.get("模型编号"),
                "模型状态": row.get("当前模型状态"),
                "渠道额度": _money(row.get("渠道额度")),
                "原渠道额度": _money(row.get("原渠道额度")),
                "ROI偏差": row.get("ROI偏差"),
                "来源状态": row.get("来源状态"),
                "历史标记": bool(row.get("历史标记")),
                "生效状态": row.get("生效状态"),
            })
        return {
            "复核对象": plan.get("方案编号"),
            "方案编号": plan.get("方案编号"),
            "调用链版本": version,
            "业务版本": plan.get("业务版本"),
            "业务版本材料": (material or {}).get("材料编号"),
            "材料状态": (material or {}).get("材料状态"),
            "复核版本": plan.get("复核版本"),
            "结果版本": plan.get("数据集版本"),
            "来源有效": bool(plan.get("来源有效")),
            "来源提示": plan.get("来源提示"),
            "处理终点": plan.get("处理终点"),
            "复核状态": plan.get("复核状态"),
            "断点": breakpoints,
            "检查项": checks,
            "模型链路": chain["模型"],
            "用户价值记录": chain["用户价值记录"],
            "渠道分配记录": allocation_rows,
            "历史数据集核对": [
                {
                    "数据集编号": row.get("数据集编号"),
                    "数据集版本": row.get("数据集版本"),
                    "状态": row.get("状态"),
                    "来源状态": row.get("来源状态"),
                    "核对口径": row.get("核对口径"),
                    "条目数": row.get("条目数"),
                    "渠道额度合计": row.get("渠道额度合计"),
                    "模型范围": row.get("模型范围"),
                    "失效模型": row.get("失效模型"),
                }
                for row in datasets
            ],
            "渠道额度": self.plan_allocations(plan),
            "历史记录": plan.get("历史") or [],
            "冲突处理": plan.get("冲突处理") or [],
        }

    # ---------- LTV侧视角 ----------

    def model_impact(self, entry_id: int | str) -> dict[str, Any] | None:
        """LTV详情页：该模型支撑的渠道额度、预算方案与待处理项。"""
        model = self.get_model_by_id(entry_id)
        if model is None:
            return None
        model_no = str(model.get("模型编号"))
        plans = []
        for plan in store.rows("budget"):
            rows = [
                row for row in store.rows("channel_alloc")
                if row.get("方案编号") == plan.get("方案编号") and row.get("模型编号") == model_no
            ]
            if not rows and model_no not in (plan.get("关联LTV模型") or []):
                continue
            plans.append({
                "方案编号": plan.get("方案编号"),
                "status": plan.get("status"),
                "复核状态": plan.get("复核状态"),
                "数据集编号": plan.get("数据集编号"),
                "数据集版本": plan.get("数据集版本"),
                "来源有效": plan.get("来源有效"),
                "来源提示": plan.get("来源提示"),
                "渠道额度": [
                    {
                        "分配编号": row.get("分配编号"),
                        "渠道": row.get("渠道"),
                        "渠道额度": _money(row.get("渠道额度")),
                        "原渠道额度": _money(row.get("原渠道额度")),
                        "ROI偏差": row.get("ROI偏差"),
                        "历史标记": bool(row.get("历史标记")),
                        "生效状态": row.get("生效状态"),
                    }
                    for row in sorted(rows, key=lambda row: int(row.get("id", 0)))
                ],
            })
        pending = [
            row for row in store.rows("ltv_pending")
            if model_no in (row.get("模型范围") or []) or model_no in (row.get("排除模型") or [])
        ]
        return {
            "模型编号": model_no,
            "模型名称": model.get("模型名称"),
            "status": model.get("status"),
            "模型版本": model.get("模型版本"),
            "价值记录": self.value_records([model_no]),
            "预算方案": plans,
            "待处理明细": sorted(pending, key=lambda row: int(row.get("id", 0))),
        }


allocation_service = AllocationService()
