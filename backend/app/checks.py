"""端到端验收脚本：覆盖校验中参与、废弃残留、重复批准与失败回退。

直接以服务层为入口跑一遍故障链，便于在没有浏览器时复查业务口径。
运行：backend/.venv/bin/python -m app.checks
"""
from __future__ import annotations

from app.services.allocation import allocation_service as allocations
from app.services.lifetime import LifetimeService
from app.store import store

lifetime_service = LifetimeService()


def _check(title: str, condition: bool, detail: str) -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {title}：{detail}")
    return condition


def main() -> int:
    passed = True

    plan = allocations.get_plan("BUDG-0001")
    audit = allocations.audit("BUDG-0001") or {}
    breakpoints = audit.get("断点") or []
    passed &= _check(
        "校验中模型参与预算计算",
        any(item["模型编号"] == "LIFE-0002" and item["断点类型"] == "校验中模型参与额度"
            for item in breakpoints),
        f"遗留数据集断点 {len(breakpoints)} 条，LIFE-0002 仍在为信息流保留额度",
    )
    passed &= _check(
        "BUDG-0001 为复核对象且来源失效",
        plan is not None and plan.get("来源有效") is False,
        f"来源提示：{plan.get('来源提示') if plan else '方案缺失'}",
    )

    dataset, message = allocations.recalculate("BUDG-0001")
    passed &= _check(
        "重算生成版本化数据集",
        dataset is not None and dataset["数据集版本"].startswith("BUDG-0001-BV-2026-11-A-V")
        and dataset["数据集版本"].endswith("V2"),
        message,
    )
    passed &= _check(
        "重算排除校验中模型",
        dataset is not None and "LIFE-0002" in (dataset.get("排除模型") or [])
        and "LIFE-0002" not in (dataset.get("模型范围") or []),
        f"模型范围 {dataset.get('模型范围') if dataset else '—'}",
    )
    current = allocations.plan_allocations(plan) if plan else []
    passed &= _check(
        "当前渠道额度不再引用不可用模型",
        all(allocations.model_status(str(row.get("模型编号"))) == "已启用" for row in current),
        "、".join(f"{row['渠道']}={row['模型编号']}({row['渠道额度']})" for row in current),
    )
    passed &= _check(
        "历史渠道额度与ROI偏差保留",
        all(row.get("原渠道额度") is not None and row.get("ROI偏差") for row in current),
        "、".join(f"{row['渠道']} 原{row['原渠道额度']}" for row in current),
    )

    approved, approve_message = allocations.approve("BUDG-0001")
    passed &= _check("按业务版本批准", approved is not None, approve_message)
    passed &= _check(
        "结果版本由业务版本材料确认",
        (approved or {}).get("复核版本") == "BV-2026-11-A"
        and (approved or {}).get("数据集版本", "").startswith("BUDG-0001-BV-2026-11-A-"),
        f"结果版本 {(approved or {}).get('数据集版本')}",
    )
    wrong, wrong_message = allocations.recalculate("BUDG-0002", business_version="BV-2099-Z")
    passed &= _check("非材料版本不能落定", wrong is None, wrong_message)
    repeated, repeated_message = allocations.approve("BUDG-0001")
    passed &= _check("重复批准不产生第二个终点", repeated is None, repeated_message)
    frozen, frozen_message = allocations.freeze("BUDG-0001")
    passed &= _check("批准后冻结请求回退", frozen is None, frozen_message)

    delivered, deliver_message = allocations.deliver("BUDG-0001")
    passed &= _check(
        "数据集投递到LTV估算待处理明细",
        delivered is not None,
        deliver_message,
    )
    redelivered, redeliver_message = allocations.deliver("BUDG-0001")
    passed &= _check(
        "重复投递幂等",
        redelivered is not None and redelivered.get("待处理编号") == (delivered or {}).get("待处理编号"),
        redeliver_message,
    )

    entry, action_message = lifetime_service.run_action(3, "废弃模型")
    changed = allocations.on_model_state_changed("LIFE-0003", entry["status"], action="废弃模型") if entry else []
    deprecated_audit = allocations.audit("BUDG-0003") or {}
    passed &= _check(
        "废弃LTV状态传播到渠道额度",
        any(item["方案编号"] == "BUDG-0003" for item in changed),
        action_message,
    )
    passed &= _check(
        "废弃后残留可复查",
        any(item["模型编号"] == "LIFE-0003" and item["模型状态"] == "已废弃"
            for item in deprecated_audit.get("断点") or []),
        "数据编号 " + "、".join(str(item["数据编号"]) for item in deprecated_audit.get("断点") or []),
    )
    budg3 = allocations.get_plan("BUDG-0003")
    passed &= _check(
        "已批准方案保持原预算时点状态",
        budg3 is not None and budg3.get("批准次数") == 1
        and budg3.get("批准时点") == "2026-10-15T09:30:00",
        f"批准次数 {budg3.get('批准次数') if budg3 else '—'}，复核状态 {budg3.get('复核状态') if budg3 else '—'}",
    )
    passed &= _check(
        "废弃后不能批准",
        allocations.approve("BUDG-0003")[0] is None,
        allocations.approve("BUDG-0003")[1],
    )

    package, package_message = allocations.package("BUDG-0004")
    passed &= _check("打包渠道预算分配明细数据集", package is not None, package_message)
    received, receive_message = allocations.receive_file(
        "BUDG-0004",
        {"交付包编号": (package or {}).get("交付包编号"), "内容摘要": (package or {}).get("内容摘要")},
    )
    passed &= _check("文件接收落定批准结果", received is not None, receive_message)
    passed &= _check(
        "重复接收不产生第二个终点",
        allocations.receive_file(
            "BUDG-0004",
            {"交付包编号": (package or {}).get("交付包编号")},
        )[0] is None,
        allocations.receive_file("BUDG-0004", {"交付包编号": (package or {}).get("交付包编号")})[1],
    )
    receipt_plan, receipt_message = allocations.ltv_receipt("BUDG-0002")
    passed &= _check(
        "LTV下线回执先冻结待复核",
        receipt_plan is not None and receipt_plan.get("处理终点") == "LTV下线回执冻结",
        receipt_message,
    )
    passed &= _check(
        "回执落定后批准请求不生效",
        allocations.approve("BUDG-0002")[0] is None,
        allocations.approve("BUDG-0002")[1],
    )

    passed &= _check(
        "待处理明细承接数据集",
        any(item.get("数据集编号") == (delivered or {}).get("数据集编号") for item in allocations.pending_items()),
        f"待处理 {len(allocations.pending_items())} 条",
    )
    audit_final = allocations.audit("BUDG-0001") or {}
    passed &= _check(
        "复核断点可复查",
        len(audit_final.get("历史数据集核对") or []) >= 2,
        "历史数据集 " + "、".join(str(row["数据集编号"]) for row in audit_final.get("历史数据集核对") or []),
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
