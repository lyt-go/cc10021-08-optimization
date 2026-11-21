"""分类标签服务：基于表结构和元数据自动关联分类标签，驱动数据资产分类管理。

domain_chain: 分类标签 (tag_classify)
  └─ 关联 → 元数据采集 (metadata_crawler)
  └─ 标准化 → 规则模板 (dq_template)

大表（存储量级）判定不再现场解析目录字符串，而是统一取 table_row_stat 的生效口径：
同一时间口径实测优先于估算，目录中没有统计记录时回退到目录行数解析并即时补一条估算。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.table_row_stat import TableRowStatService, parse_row_count
from app.services.threshold_version import ThresholdVersionService
from app.store import store

DEFAULT_LARGE_TABLE_THRESHOLD = 10_000_000


class TagClassifyService(BaseModuleService):
    MODULE = "tag_classify"
    ENTITY = "分类标签"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["标签编号", "标签名称", "分类维度", "关联表名", "标签值", "分类依据", "生效时间", "标签版本"]
    REQUIRED_FIELDS = ["标签编号", "标签名称", "分类维度"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._row_stat = TableRowStatService()

    def _resolve_storage_level(self, cat: dict[str, Any], fallback_batch: str) -> tuple[str, str, int]:
        """返回（大表/小表, 行数来源, 生效行数），优先读聚合后的行数统计。"""
        table = cat.get("表名称", "")
        effective = self._row_stat.effective_count(table)
        if effective is None:
            # 目录已有行数但还没聚合过：补一条估算统计，避免重复扫描目录
            count = parse_row_count(cat.get("数据行数", 0))
            if count > 0:
                self._row_stat.aggregate_directory_scan(
                    [{"表名称": table, "数据行数": count}],
                    batch_id=fallback_batch,
                    stat_time=cat.get("最后更新") or datetime.now(timezone.utc).isoformat(),
                )
                effective = self._row_stat.effective_count(table)
        if effective is None:
            return "小表", "无统计", 0

        threshold = self._current_threshold()
        level = "大表" if effective["行数"] >= threshold else "小表"
        return level, effective["数据来源"], effective["行数"]

    @staticmethod
    def _current_threshold() -> int:
        """取最新轮次已确认阈值；尚无确认版本时回退到默认 1000 万行。"""
        versions = store.rows("threshold_version")
        confirmed = [r for r in versions if r.get("status") == "已确认"]
        if not confirmed:
            return DEFAULT_LARGE_TABLE_THRESHOLD
        latest = max(confirmed, key=lambda r: str(r.get("确认时间", "")))
        return int(latest.get("大表阈值行数", DEFAULT_LARGE_TABLE_THRESHOLD))

    def classify_tables_by_metadata(self, min_quality: float = 60.0) -> dict[str, Any]:
        """基于元数据对表进行自动分类，关联分类标签并标准化表分类体系。

        查询表目录和字段画像数据，按分层、存储和访问模式关联分类标签。
        存储量级统一从表行数统计的生效口径（估算/实测）判定。
        """
        catalog_rows = store.rows("table_catalog")
        tag_rows = store.rows(self.MODULE)
        fallback_batch = f"catalog-fallback-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"

        categories = {"ODS层": [], "DWS层": [], "ADS层": [], "DIM层": []}
        classified = 0
        storage_basis: dict[str, str] = {}

        for cat in catalog_rows:
            db_name = cat.get("所属库名", "")
            field_count = int(cat.get("字段数量", 0))

            # 关联分类标签：根据库名分层
            if "ods" in db_name.lower():
                layer = "ODS层"
            elif "dws" in db_name.lower():
                layer = "DWS层"
            elif "ads" in db_name.lower():
                layer = "ADS层"
            elif "dim" in db_name.lower():
                layer = "DIM层"
            else:
                layer = "未知"

            # 存储分类：取行数统计聚合结果（实测优先），不再现场字符串扫阈值
            storage, source, row_count = self._resolve_storage_level(cat, fallback_batch)
            storage_basis[storage] = source

            for tag_type, tag_value in [("分层", layer), ("存储量级", storage)]:
                basis = f"基于{db_name}来源和{field_count}字段自动分类"
                if tag_type == "存储量级":
                    basis = f"{source}行数 {row_count:,} 按阈值 {self._current_threshold():,} 判定"
                new_tid = max((int(r.get("id", 0)) for r in tag_rows), default=0) + 1
                tag_rows.append({
                    "id": new_tid,
                    "标签编号": f"TAG_-{new_tid:04d}",
                    "标签名称": f"{cat.get('表名称', '')}_{tag_type}",
                    "分类维度": tag_type,
                    "关联表名": cat.get("表名称", ""),
                    "标签值": tag_value,
                    "分类依据": basis,
                    "生效时间": datetime.now(timezone.utc).isoformat(),
                    "标签版本": "v1.0",
                    "status": "已生效",
                    "pending": False,
                    "abnormal": False,
                })
                classified += 1

            if layer != "未知":
                categories[layer].append(cat.get("表名称", ""))

        # 标准化分类规则写入模板服务（按当前确认阈值，幂等）
        self._sync_classification_rules()

        return {
            "分类时间": datetime.now(timezone.utc).isoformat(),
            "总分类标签数": classified,
            "覆盖表数": len(catalog_rows),
            "分层统计": {k: len(v) for k, v in categories.items() if v},
            "存储量级依据": storage_basis,
            "大表阈值行数": self._current_threshold(),
        }

    def _sync_classification_rules(self) -> None:
        """标准化分类规则，将分类模式写入规则模板库供复用（重复分类不重复建模板）。"""
        template_rows = store.rows("dq_template")
        threshold = self._current_threshold()
        rules = [
            ("ODS层分类规则", "ODS", "ods_开头表自动归入ODS层"),
            ("ADS层分类规则", "ADS", "ads_开头表自动归入ADS层"),
            ("大表分类规则", "存储量级", f"生效行数>={threshold:,}标记为大表（取表行数统计，实测优先）"),
        ]
        for tpl_name, rule_type, desc in rules:
            if any(r.get("模板名称") == tpl_name and r.get("status") != "已停用"
                   for r in template_rows):
                continue
            new_tid = max((int(r.get("id", 0)) for r in template_rows), default=0) + 1
            template_rows.append({
                "id": new_tid,
                "模板编号": f"DQ_T-{new_tid:04d}",
                "模板名称": tpl_name,
                "规则类型": rule_type,
                "校验SQL": f"SELECT * FROM classification WHERE type='{rule_type}'",
                "参数列表": "[\"tag_layer\", \"storage_level\"]",
                "适用场景": desc,
                "使用次数": "1",
                "模板版本": "v1",
                "status": "已发布",
                "pending": False,
                "abnormal": False,
            })

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "关联"
        snapshot["desc"] = "基于元数据关联分类标签并标准化分类规则"
        snapshot["artifact"] = "分类标签数据集"
        snapshot["artifact_desc"] = "按分层和存储量级分类的标签数据集"
        return snapshot
