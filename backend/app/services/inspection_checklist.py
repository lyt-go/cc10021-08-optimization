"""巡检清单服务：规则模板依据大表标签生成执行清单，并承接分类结论回写。

domain_chain: 巡检清单 (inspection_checklist)
  └─ 取数 → 大表标签 (large_table_tag)
  └─ 回写 → 规则模板 (dq_template) 待处理清单

清单按「评估轮次 + 表」幂等：同一轮次重复生成不会重复建单；
模板运行后的分类结论回写到清单，待处理项关闭，争议表保持待复核。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InspectionChecklistService(BaseModuleService):
    MODULE = "inspection_checklist"
    ENTITY = "巡检清单"
    OPERATION_TERM = "生成"
    LIST_FIELDS = [
        "清单编号", "评估轮次", "关联表名", "标签版本", "阈值版本", "行数来源",
        "判定行数", "模板编号", "处理状态", "分类结论", "待处理标记", "生成时间", "处理时间",
    ]
    REQUIRED_FIELDS = ["清单编号", "评估轮次", "关联表名"]
    STATUS_ORDER = ["待处理", "巡检中", "已完成", "待复核"]
    ACTION_RULES = {"开始巡检": "巡检中", "完成回写": "已完成", "退回复核": "待复核"}
    NEGATIVE_ACTIONS = ["退回复核"]

    def upsert_checklist(
        self,
        round_id: str,
        table_name: str,
        tag: dict[str, Any],
        template_code: str,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """按「轮次 + 表」幂等写入执行清单项；已存在则按最新标签刷新待办状态。"""
        rows = store.rows(self.MODULE)
        existing = next(
            (r for r in rows
             if r.get("评估轮次") == round_id and r.get("关联表名") == table_name),
            None,
        )
        needs_review = tag.get("复核状态") == "待复核"
        if existing:
            # 清单保留一条，但标签版本/复核状态以最新重算结果为准；
            # 已完成回写的清单不被重算重新打开。
            if existing.get("处理状态") != "已完成":
                existing["标签编号"] = tag.get("标签编号", existing.get("标签编号", ""))
                existing["标签版本"] = tag.get("标签版本", existing.get("标签版本", ""))
                existing["行数来源"] = tag.get("行数来源", existing.get("行数来源", ""))
                existing["判定行数"] = tag.get("判定行数", existing.get("判定行数", 0))
                existing["复核原因"] = "行数估算存在争议" if needs_review else ""
                existing["处理状态"] = "待复核" if needs_review else "待处理"
                existing["status"] = "待复核" if needs_review else "待处理"
                existing["pending"] = True
                existing["abnormal"] = needs_review
                existing["待处理标记"] = True
            return {"ok": True, "幂等": True, "清单编号": existing["清单编号"]}

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "清单编号": f"ICL-{new_id:04d}",
            "评估轮次": round_id,
            "关联表名": table_name,
            "标签编号": tag.get("标签编号", ""),
            "标签版本": tag.get("标签版本", ""),
            "阈值版本": tag.get("阈值版本", ""),
            "行数来源": tag.get("行数来源", ""),
            "判定行数": tag.get("判定行数", 0),
            "模板编号": template_code,
            "处理状态": "待复核" if needs_review else "待处理",
            "分类结论": "",
            "待处理标记": True,
            "复核原因": "行数估算存在争议" if needs_review else "",
            "生成时间": created_at or _now_iso(),
            "处理时间": "",
            "status": "待复核" if needs_review else "待处理",
            "pending": True,
            "abnormal": needs_review,
        }
        rows.append(entry)
        return {"ok": True, "幂等": False, "清单编号": entry["清单编号"]}

    def write_back_conclusion(
        self,
        round_id: str,
        table_name: str,
        conclusion: str,
        processed_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """把模板运行后的分类结论回写到待处理清单并关闭待处理标记。"""
        rows = store.rows(self.MODULE)
        entry = next(
            (r for r in rows
             if r.get("评估轮次") == round_id and r.get("关联表名") == table_name),
            None,
        )
        if entry is None:
            return None, f"轮次 {round_id} 中没有表 {table_name} 的巡检清单项"
        entry["分类结论"] = conclusion
        entry["处理状态"] = "已完成"
        entry["待处理标记"] = False
        entry["处理时间"] = processed_at or _now_iso()
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        return entry, "分类结论已回写，清单项关闭"

    def pending_for_round(self, round_id: str) -> list[dict[str, Any]]:
        """某轮次仍待处理（含待复核）的清单项。"""
        return [
            r for r in store.rows(self.MODULE)
            if r.get("评估轮次") == round_id and r.get("待处理标记")
        ]

    def tables_for_round(self, round_id: str) -> list[str]:
        """某轮次进入巡检的全部表（实测补回前后可对照）。"""
        return [
            r.get("关联表名", "")
            for r in store.rows(self.MODULE)
            if r.get("评估轮次") == round_id
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "生成"
        snapshot["desc"] = "依据大表标签生成模板执行清单并回写分类结论"
        snapshot["artifact"] = "大表巡检执行清单"
        snapshot["artifact_desc"] = "模板待处理清单与分类结论回写记录"
        return snapshot
