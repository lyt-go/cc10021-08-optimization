"""表行数统计服务：聚合目录扫描的估算行数与补回的实测行数，为大表标签提供可重算数据。

domain_chain: 表行数统计 (table_row_stat)
  └─ 聚合 → 大表标签 (large_table_tag)
  └─ 取数 → 规则模板 (dq_template)

设计要点：
- 目录重复扫描按「表 + 来源批次 + 来源」幂等落库，不再每次扫描都追加重复记录；
- 每条统计都标注数据来源（估算/实测）、统计时间口径与来源批次，落库即可解释；
- 实测补回同一时间口径的数据时，旧估算标记为已过期并指向替代记录，历史仍可追溯；
- effective_count 给出当前生效口径：同一时间口径实测优先于估算。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 兼容 "15,000,000"、"12000000"、"8,000,000 行" 等写法，取第一段连续数字
_ROW_COUNT_RE = re.compile(r"[\d,]+")
NO_DISPUTE = "无争议"
ACTIVE_STATUSES = ("待确认", "有效", "争议中")


def parse_row_count(raw: Any) -> int:
    """把目录里各种字符串形态的行数解析为整数，无法解析时返回 0。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).replace(" ", "").strip()
    if not text:
        return 0
    match = _ROW_COUNT_RE.search(text)
    if not match:
        return 0
    return int(match.group(0).replace(",", ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TableRowStatService(BaseModuleService):
    MODULE = "table_row_stat"
    ENTITY = "行数统计"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "统计编号", "关联表名", "数据来源", "行数", "统计时间", "来源批次",
        "争议标记", "替代编号", "口径版本",
    ]
    REQUIRED_FIELDS = ["统计编号", "关联表名", "行数"]
    STATUS_ORDER = ["待确认", "有效", "争议中", "已过期"]
    ACTION_RULES = {"确认统计": "有效", "标记争议": "争议中", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = ["标记争议", "标记过期"]

    # ------------------------------------------------------------------ 写入
    def aggregate_directory_scan(
        self,
        scan_rows: list[dict[str, Any]],
        batch_id: str,
        stat_time: str | None = None,
        source: str = "估算",
    ) -> dict[str, Any]:
        """聚合一次目录扫描的行数，按批次幂等落库，重复扫描不会产生重复统计。

        scan_rows 元素支持「表名称/关联表名」「数据行数/行数」两种字段命名，
        便于直接传入表目录记录或采集器的扫描结果。
        """
        rows = store.rows(self.MODULE)
        stat_time = stat_time or _now_iso()
        inserted, skipped = 0, 0
        tables: list[str] = []

        for item in scan_rows:
            table = item.get("表名称") or item.get("关联表名")
            if not table:
                skipped += 1
                continue
            count = parse_row_count(item.get("数据行数", item.get("行数", 0)))

            # 幂等键：同表 + 同批次 + 同来源且未过期，视为同一次扫描的重复投递
            duplicated = any(
                r.get("关联表名") == table
                and r.get("来源批次") == batch_id
                and r.get("数据来源") == source
                and r.get("status") != "已过期"
                for r in rows
            )
            if duplicated:
                skipped += 1
                continue

            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": new_id,
                "统计编号": f"TRS-{new_id:04d}",
                "关联表名": table,
                "数据来源": source,
                "行数": count,
                "统计时间": stat_time,
                "来源批次": batch_id,
                "争议标记": NO_DISPUTE,
                "替代编号": "",
                "口径版本": f"{source}-v1",
                "备注": "目录扫描聚合" if source == "估算" else "批量实测聚合",
                "status": "有效",
                "pending": False,
                "abnormal": False,
            })
            inserted += 1
            tables.append(table)

        return {
            "来源批次": batch_id,
            "数据来源": source,
            "统计时间": stat_time,
            "扫描表数": len(scan_rows),
            "新增统计数": inserted,
            "重复跳过数": skipped,
            "覆盖表": tables,
        }

    def register_measured_count(
        self,
        table_name: str,
        measured_rows: int,
        stat_time: str,
        batch_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        """补回一张表在指定时间口径的实测行数。

        同一时间口径已有实测记录时幂等返回；同口径的旧估算统一置为已过期，
        并通过「替代编号」指向新实测记录，保证历史标签版本仍能回溯当时依据。
        """
        rows = store.rows(self.MODULE)

        existing_measured = next(
            (r for r in rows
             if r.get("关联表名") == table_name
             and r.get("数据来源") == "实测"
             and r.get("统计时间") == stat_time
             and r.get("status") != "已过期"),
            None,
        )
        if existing_measured:
            return {"ok": True, "幂等": True, "统计编号": existing_measured["统计编号"]}

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        new_code = f"TRS-{new_id:04d}"
        replaced_estimates: list[str] = []

        # 相同时间口径的估算全部过期，并记录替代关系
        for r in rows:
            if (
                r.get("关联表名") == table_name
                and r.get("数据来源") == "估算"
                and r.get("统计时间") == stat_time
                and r.get("status") != "已过期"
            ):
                r["status"] = "已过期"
                r["pending"] = False
                r["替代编号"] = new_code
                r["备注"] = f"实测补回后停用估算依据；{r.get('备注', '')}".strip("；")
                replaced_estimates.append(r.get("统计编号", ""))

        rows.append({
            "id": new_id,
            "统计编号": new_code,
            "关联表名": table_name,
            "数据来源": "实测",
            "行数": int(measured_rows),
            "统计时间": stat_time,
            "来源批次": batch_id,
            "争议标记": NO_DISPUTE,
            "替代编号": "",
            "口径版本": "实测-v1",
            "备注": note or "实测行数补回",
            "status": "有效",
            "pending": False,
            "abnormal": False,
        })

        return {
            "ok": True,
            "幂等": False,
            "统计编号": new_code,
            "关联表名": table_name,
            "行数": int(measured_rows),
            "统计时间": stat_time,
            "替代估算编号": replaced_estimates,
        }

    # ------------------------------------------------------------------ 读取
    def effective_count(self, table_name: str) -> dict[str, Any] | None:
        """返回一张表当前生效的行数口径：同口径实测优先，否则取最新估算。"""
        active = [
            r for r in store.rows(self.MODULE)
            if r.get("关联表名") == table_name and r.get("status") in ACTIVE_STATUSES
        ]
        if not active:
            return None
        measured = [r for r in active if r.get("数据来源") == "实测"]
        pool = measured or active
        latest = max(pool, key=lambda r: str(r.get("统计时间", "")))
        return {
            "关联表名": table_name,
            "行数": int(latest.get("行数", 0)),
            "数据来源": latest.get("数据来源", "估算"),
            "统计时间": latest.get("统计时间", ""),
            "统计编号": latest.get("统计编号", ""),
            "争议标记": latest.get("争议标记", NO_DISPUTE),
            "来源批次": latest.get("来源批次", ""),
        }

    def active_tables(self) -> list[str]:
        """所有仍有生效行数统计的表，驱动标签全量重算。"""
        seen: list[str] = []
        for r in store.rows(self.MODULE):
            if r.get("status") in ACTIVE_STATUSES and r.get("关联表名") not in seen:
                seen.append(r["关联表名"])
        return seen

    def disputed_tables(self) -> list[dict[str, Any]]:
        """行数估算仍有争议、需要另行复核的表。"""
        result = []
        for r in store.rows(self.MODULE):
            if r.get("status") in ACTIVE_STATUSES and r.get("争议标记") not in ("", NO_DISPUTE):
                result.append({
                    "关联表名": r.get("关联表名"),
                    "统计编号": r.get("统计编号"),
                    "行数": r.get("行数"),
                    "数据来源": r.get("数据来源"),
                    "争议标记": r.get("争议标记"),
                })
        return result

    def flag_dispute(self, stat_id: int, reason: str) -> tuple[dict[str, Any] | None, str]:
        """把一条估算统计标记为争议中，触发表行数的另行复核。"""
        entry = store.find(self.MODULE, stat_id)
        if entry is None:
            return None, f"行数统计 {stat_id} 不存在"
        entry["争议标记"] = reason or "估算行数存在争议，待复核"
        entry["status"] = "争议中"
        entry["pending"] = True
        entry["abnormal"] = True
        return entry, "已标记争议，等待人工复核"

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合目录估算行数与实测行数，输出可重算的行数口径"
        snapshot["artifact"] = "表行数统计数据集"
        snapshot["artifact_desc"] = "区分估算与实测来源、带时间口径与批次的行数统计"
        return snapshot
