"""大表阈值版本服务：管理评估轮次内的阈值变更，并通过并发确认保证同轮只有一个生效版本。

domain_chain: 阈值版本 (threshold_version)
  └─ 约束 → 大表标签 (large_table_tag)
  └─ 取数 → 规则模板 (dq_template)

并发规则：同一评估轮次（评估轮次号）内，只允许一个阈值版本进入「已确认」。
所有确认动作在同一把进程内锁内完成「检查同轮已确认 → 置为已确认」，
后续在同轮把阈值从 1000 万改到 2000 万的新版本只能停留在待确认，必须先停用旧版本。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 进程内并发锁：阈值确认与标签重算都在该锁内做「轮次唯一性」校验
THRESHOLD_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ThresholdVersionService(BaseModuleService):
    MODULE = "threshold_version"
    ENTITY = "阈值版本"
    OPERATION_TERM = "确认"
    LIST_FIELDS = [
        "版本编号", "评估轮次", "大表阈值行数", "版本说明", "提交时间", "确认时间", "版本状态",
    ]
    REQUIRED_FIELDS = ["版本编号", "评估轮次", "大表阈值行数"]
    STATUS_ORDER = ["待确认", "已确认", "已停用"]
    ACTION_RULES = {"确认版本": "已确认", "停用版本": "已停用"}
    NEGATIVE_ACTIONS = ["停用版本"]

    def register_threshold(
        self,
        round_id: str,
        threshold_rows: int,
        note: str = "",
        submitted_at: str | None = None,
    ) -> dict[str, Any]:
        """登记一个阈值版本；同轮次同阈值幂等返回，变更阈值则产生新的候选版本。"""
        rows = store.rows(self.MODULE)
        same = [r for r in rows if r.get("评估轮次") == round_id]
        duplicate = next(
            (r for r in same
             if int(r.get("大表阈值行数", 0)) == int(threshold_rows)
             and r.get("status") != "已停用"),
            None,
        )
        if duplicate:
            return {"ok": True, "幂等": True, "版本编号": duplicate["版本编号"]}

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        seq = len(same) + 1
        entry = {
            "id": new_id,
            "版本编号": f"THV-{round_id}-{seq:02d}",
            "评估轮次": round_id,
            "大表阈值行数": int(threshold_rows),
            "版本说明": note or f"{round_id} 轮次候选阈值 {int(threshold_rows):,} 行",
            "提交时间": submitted_at or _now_iso(),
            "确认时间": "",
            "版本状态": "候选",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        return {"ok": True, "幂等": False, "版本编号": entry["版本编号"], "版本状态": "待确认"}

    def confirm_threshold(self, version_id: int) -> tuple[dict[str, Any] | None, str]:
        """确认阈值版本进入模板；同轮已有确认版本时拒绝（并发安全）。"""
        with THRESHOLD_LOCK:
            entry = store.find(self.MODULE, version_id)
            if entry is None:
                return None, f"阈值版本 {version_id} 不存在"
            if entry.get("status") == "已停用":
                return None, "该阈值版本已停用，不能确认"
            if entry.get("status") == "已确认":
                return entry, "该阈值版本已是同轮唯一生效版本"

            rows = store.rows(self.MODULE)
            confirmed = next(
                (r for r in rows
                 if r.get("评估轮次") == entry.get("评估轮次")
                 and r.get("status") == "已确认"
                 and int(r.get("id", 0)) != version_id),
                None,
            )
            if confirmed is not None:
                return None, (
                    f"评估轮次 {entry['评估轮次']} 已有生效阈值版本 "
                    f"{confirmed['版本编号']}（{int(confirmed['大表阈值行数']):,} 行），"
                    "同轮只允许一个阈值版本进入模板，请先停用旧版本"
                )

            entry["status"] = "已确认"
            entry["版本状态"] = "已确认"
            entry["确认时间"] = _now_iso()
            entry["pending"] = False
            entry["abnormal"] = False
            return entry, f"{entry['评估轮次']} 轮次阈值已确认，可进入规则模板"

    def deactivate_threshold(self, version_id: int) -> tuple[dict[str, Any] | None, str]:
        """停用当前生效阈值，给同轮新阈值腾出唯一确认位。"""
        with THRESHOLD_LOCK:
            entry = store.find(self.MODULE, version_id)
            if entry is None:
                return None, f"阈值版本 {version_id} 不存在"
            entry["status"] = "已停用"
            entry["版本状态"] = "已停用"
            entry["pending"] = False
            return entry, "阈值版本已停用"

    def confirmed_version(self, round_id: str) -> dict[str, Any] | None:
        """读取某评估轮次唯一确认的阈值版本，供标签重算与模板取数。"""
        with THRESHOLD_LOCK:
            return next(
                (r for r in store.rows(self.MODULE)
                 if r.get("评估轮次") == round_id and r.get("status") == "已确认"),
                None,
            )

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "确认"
        snapshot["desc"] = "按评估轮次管理大表阈值，并发保证同轮只有一个生效版本"
        snapshot["artifact"] = "大表阈值版本集"
        snapshot["artifact_desc"] = "带轮次与确认状态的阈值版本记录"
        return snapshot
