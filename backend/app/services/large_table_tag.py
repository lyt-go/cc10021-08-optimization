"""大表标签服务：按阈值版本对可重算的行数统计进行大表判定，并维护版本化标签历史。

domain_chain: 大表标签 (large_table_tag)
  └─ 取数 → 表行数统计 (table_row_stat)
  └─ 约束 → 阈值版本 (threshold_version)
  └─ 供数 → 规则模板 (dq_template)

重算规则：
- 只允许使用当前评估轮次「已确认」的唯一阈值版本，未确认阈值直接拒绝；
- 取数来自 table_row_stat 的生效口径（同时间口径实测优先于估算），落库即解释来源；
- 标签按表版本化：结论翻转时新版本「已生效」，旧版本「已废弃」并记录废弃时间，
  该时间即「停止沿用先前分类」的时间点，历史标签版本保留可对照；
- 估算行数处于争议中的表仍会出标签，但标记为待复核，不阻断模板取数。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.table_row_stat import TableRowStatService
from app.services.threshold_version import THRESHOLD_LOCK
from app.store import store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump_version(version: str) -> str:
    """标签版本号递进：v1.0 → v2.0（大版本翻转），v2.0 → v2.1（同主版本修订）。"""
    if not version or not version.startswith("v"):
        return "v2.0"
    parts = version[1:].split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return "v2.0"
    return f"v{major}.{minor + 1}" if major >= 2 else "v2.0"


class LargeTableTagService(BaseModuleService):
    MODULE = "large_table_tag"
    ENTITY = "大表标签"
    OPERATION_TERM = "重算"
    LIST_FIELDS = [
        "标签编号", "关联表名", "标签值", "阈值版本", "阈值行数", "判定行数",
        "行数来源", "统计编号", "统计时间", "标签版本", "分类依据", "生效时间",
        "废弃时间", "复核状态",
    ]
    REQUIRED_FIELDS = ["标签编号", "关联表名", "标签值", "阈值版本"]
    STATUS_ORDER = ["待复核", "已生效", "已废弃"]
    ACTION_RULES = {"确认生效": "已生效", "标记废弃": "已废弃", "发起复核": "待复核"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    def __init__(self) -> None:
        self._row_stat = TableRowStatService()

    # ------------------------------------------------------------------ 重算
    def recalculate(self, round_id: str, run_at: str | None = None) -> dict[str, Any]:
        """按指定评估轮次已确认的阈值，全量重算大表标签。

        并发上与阈值确认共用 THRESHOLD_LOCK，确保重算期间不会有第二个阈值版本被确认。
        """
        run_at = run_at or _now_iso()
        with THRESHOLD_LOCK:
            from app.services.threshold_version import ThresholdVersionService
            threshold = ThresholdVersionService().confirmed_version(round_id)
            if threshold is None:
                return {
                    "ok": False,
                    "原因": f"评估轮次 {round_id} 尚无已确认阈值版本，禁止重算大表标签",
                }
            threshold_rows = int(threshold["大表阈值行数"])
            threshold_code = threshold["版本编号"]

            tag_rows = store.rows(self.MODULE)
            stat_tables = self._row_stat.active_tables()

            created, replaced, unchanged, disputed = 0, 0, 0, 0
            decisions: list[dict[str, Any]] = []

            for table in stat_tables:
                effective = self._row_stat.effective_count(table)
                if effective is None:
                    continue
                row_count = int(effective["行数"])
                new_value = "大表" if row_count >= threshold_rows else "普通表"
                is_disputed = effective.get("争议标记", "无争议") != "无争议"
                if is_disputed:
                    disputed += 1

                current = next(
                    (r for r in tag_rows
                     if r.get("关联表名") == table and r.get("status") in ("已生效", "待复核")),
                    None,
                )

                # 结论与阈值版本都未变化：沿用当前标签，避免无意义翻版本；
                # 但待复核标签需要按最新争议状态刷新（争议升级或复核解除）。
                if current and current.get("标签值") == new_value \
                        and current.get("阈值版本") == threshold_code:
                    if current.get("status") == "待复核" or is_disputed:
                        current["复核状态"] = "待复核" if is_disputed else "无需复核"
                        current["status"] = "待复核" if is_disputed else "已生效"
                        current["pending"] = is_disputed
                        current["abnormal"] = is_disputed
                        current["分类依据"] = (
                            f"{effective['数据来源']}行数 {row_count:,} "
                            f"({effective['统计编号']} @ {effective['统计时间']}) "
                            f"对比阈值 {threshold_rows:,}（{threshold_code}）"
                        )
                        current["统计编号"] = effective["统计编号"]
                        current["行数来源"] = effective["数据来源"]
                        current["判定行数"] = row_count
                    unchanged += 1
                    decisions.append(self._decision(table, current, "沿用"))
                    continue

                # 结论翻转或阈值升级：旧标签废弃并记录停止沿用时间，新版本生效
                if current:
                    current["status"] = "已废弃"
                    current["pending"] = False
                    current["abnormal"] = False
                    current["废弃时间"] = run_at
                    next_version = _bump_version(str(current.get("标签版本", "v1.0")))
                    replaced += 1
                else:
                    next_version = "v1.0"

                new_id = max((int(r.get("id", 0)) for r in tag_rows), default=0) + 1
                basis = (
                    f"{effective['数据来源']}行数 {row_count:,} "
                    f"({effective['统计编号']} @ {effective['统计时间']}) "
                    f"对比阈值 {threshold_rows:,}（{threshold_code}）"
                )
                entry = {
                    "id": new_id,
                    "标签编号": f"LTT-{new_id:04d}",
                    "关联表名": table,
                    "标签值": new_value,
                    "阈值版本": threshold_code,
                    "评估轮次": round_id,
                    "阈值行数": threshold_rows,
                    "判定行数": row_count,
                    "行数来源": effective["数据来源"],
                    "统计编号": effective["统计编号"],
                    "统计时间": effective["统计时间"],
                    "来源批次": effective.get("来源批次", ""),
                    "标签版本": next_version,
                    "分类依据": basis,
                    "生效时间": run_at,
                    "废弃时间": "",
                    "复核状态": "待复核" if is_disputed else "无需复核",
                    "status": "待复核" if is_disputed else "已生效",
                    "pending": is_disputed,
                    "abnormal": is_disputed,
                }
                tag_rows.append(entry)
                created += 1
                decisions.append(self._decision(table, entry, "废弃旧版" if current else "新增"))

        return {
            "ok": True,
            "评估轮次": round_id,
            "阈值版本": threshold_code,
            "阈值行数": threshold_rows,
            "重算时间": run_at,
            "参与表数": len(stat_tables),
            "新增标签数": created,
            "废弃旧版数": replaced,
            "沿用标签数": unchanged,
            "争议待复核表数": disputed,
            "判定明细": decisions,
        }

    @staticmethod
    def _decision(table: str, entry: dict[str, Any], action: str) -> dict[str, Any]:
        return {
            "关联表名": table,
            "动作": action,
            "标签值": entry.get("标签值"),
            "标签版本": entry.get("标签版本"),
            "行数来源": entry.get("行数来源"),
            "判定行数": entry.get("判定行数"),
            "状态": entry.get("status"),
        }

    # ------------------------------------------------------------------ 读取
    def active_large_tables(self, round_id: str | None = None) -> list[dict[str, Any]]:
        """当前生效（含待复核）的大表标签，供规则模板生成执行清单。"""
        result = []
        for r in store.rows(self.MODULE):
            if r.get("status") not in ("已生效", "待复核") or r.get("标签值") != "大表":
                continue
            if round_id is not None and r.get("评估轮次") != round_id:
                continue
            result.append(r)
        return result

    def table_tag_history(self, table_name: str) -> list[dict[str, Any]]:
        """单表标签全版本历史，用于对照旧分类与确认停止沿用时间。"""
        return [
            r for r in store.rows(self.MODULE)
            if r.get("关联表名") == table_name
        ]

    def classification_timeline(self) -> list[dict[str, Any]]:
        """每张表的分类时间线：何时进入大表巡检、何时停止沿用先前分类。"""
        history: dict[str, list[dict[str, Any]]] = {}
        for r in store.rows(self.MODULE):
            history.setdefault(r.get("关联表名", ""), []).append(r)

        timeline = []
        for table, versions in history.items():
            versions.sort(key=lambda x: str(x.get("生效时间", "")))
            # 进入巡检时间：历史上首次命中大表（含后来被废弃的版本）
            entered = next(
                (v["生效时间"] for v in versions if v.get("标签值") == "大表"),
                "",
            )
            # 停止沿用旧分类：最近一次标签废弃的时间
            stopped = next(
                (v["废弃时间"] for v in reversed(versions)
                 if v.get("status") == "已废弃" and v.get("废弃时间")),
                "",
            )
            current = next(
                (v for v in versions if v.get("status") in ("已生效", "待复核")), None
            )
            timeline.append({
                "关联表名": table,
                "进入巡检时间": entered,
                "停止沿用旧分类时间": stopped,
                "当前标签值": current.get("标签值", "") if current else "无生效标签",
                "当前标签版本": current.get("标签版本", "") if current else "",
                "历史版本数": len(versions),
            })
        return timeline

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "重算"
        snapshot["desc"] = "按已确认阈值版本重算大表标签并保留版本化历史"
        snapshot["artifact"] = "大表标签数据集"
        snapshot["artifact_desc"] = "区分估算与实测依据、可重算可回溯的大表标签"
        return snapshot
