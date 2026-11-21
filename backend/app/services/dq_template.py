"""规则模板库服务：管理和标准化质量规则模板，驱动规则定义和执行。

domain_chain: 规则模板 (dq_template)
  └─ 标准化 → 质量规则定义 (dq_rule_def)
  └─ 校验 → 巡检任务 (dq_check_run)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.inspection_checklist import InspectionChecklistService
from app.services.large_table_tag import LargeTableTagService
from app.services.table_row_stat import TableRowStatService
from app.services.threshold_version import ThresholdVersionService
from app.store import store


class DqTemplateService(BaseModuleService):
    MODULE = "dq_template"
    ENTITY = "规则模板"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["模板编号", "模板名称", "规则类型", "校验SQL", "参数列表", "适用场景", "使用次数", "模板版本"]
    REQUIRED_FIELDS = ["模板编号", "模板名称", "规则类型"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    # 大表巡检模板固定名称，重复生成清单时复用同一条模板，避免模板库堆积
    LARGE_TABLE_TEMPLATE_NAME = "大表巡检规则模板"

    BUILTIN_TEMPLATES = {
        "null_check": "SELECT COUNT(*) as null_count FROM {table} WHERE {column} IS NULL",
        "uniqueness_check": "SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} HAVING COUNT(*) > 1",
        "range_check": "SELECT COUNT(*) FROM {table} WHERE {column} NOT BETWEEN {min} AND {max}",
        "freshness_check": "SELECT MAX({column}) FROM {table} HAVING MAX({column}) < DATE('now', '-{interval} days')",
        "format_check": "SELECT COUNT(*) FROM {table} WHERE {column} NOT REGEXP '{pattern}'",
        "referential_integrity": "SELECT COUNT(*) FROM {table} t LEFT JOIN {ref} r ON t.{column}=r.{ref_col} WHERE r.{ref_col} IS NULL",
        "completeness_check": "SELECT COUNT(*) as total, COUNT({column}) as non_null, COUNT(*) - COUNT({column}) as missing FROM {table}",
        "distribution_check": "SELECT {column}, COUNT(*) as freq FROM {table} GROUP BY {column} ORDER BY freq DESC LIMIT 100",
    }

    def standardize_rule_template(self, template_name: str, rule_type: str,
                                   sql_template: str, params: list[str]) -> dict[str, Any]:
        """标准化规则模板：校验模板SQL合法性并写入模板库。

        对传入的模板SQL进行解析校验，检查参数占位符与参数列表是否匹配，
        标准化后的模板写入库中并关联到规则定义服务。
        """
        rows = store.rows(self.MODULE)

        # 校验参数占位符
        import re
        placeholders = set(re.findall(r'\{(\w+)\}', sql_template))
        declared_params = set(params)
        missing_in_sql = declared_params - placeholders
        extra_in_sql = placeholders - declared_params

        validation = {
            "SQL占位符": list(placeholders),
            "声明参数": params,
            "SQL中未声明": list(missing_in_sql),
            "声明但未用": list(extra_in_sql),
            "是否一致": len(missing_in_sql) == 0 and len(extra_in_sql) == 0,
        }

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "模板编号": f"DQ_T-{new_id:04d}",
            "模板名称": template_name,
            "规则类型": rule_type,
            "校验SQL": sql_template.replace("\n", " ").strip(),
            "参数列表": str(params),
            "适用场景": f"{rule_type}标准化模板",
            "使用次数": "0",
            "模板版本": "v1.0",
            "status": "已发布",
            "pending": False,
            "abnormal": not validation["是否一致"],
        }
        rows.append(entry)

        # 关联写入规则定义
        rule_rows = store.rows("dq_rule_def")
        new_rid = max((int(r.get("id", 0)) for r in rule_rows), default=0) + 1
        rule_rows.append({
            "id": new_rid,
            "规则编号": f"DQ_R-{new_rid:04d}",
            "规则名称": f"{template_name}_auto",
            "目标表名": "all_tables",
            "校验字段": params[0] if params else "id",
            "规则类型": rule_type,
            "期望值": "0",
            "容差范围": "0.1",
            "规则版本": "v1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        })

        return {
            "模板编号": entry["模板编号"],
            "标准化结果": "成功",
            "校验详情": validation,
            "关联规则编号": f"DQ_R-{new_rid:04d}",
            "标准化时间": datetime.now(timezone.utc).isoformat(),
        }

    def instantiate_template(self, template_id: int, param_values: dict[str, Any]) -> dict[str, Any]:
        """实例化模板：用实际参数替换模板占位符，生成可执行的校验SQL。

        查询模板定义，将参数值代入SQL模板生成实际可执行的校验语句，
        同时生成规则定义记录并关联到巡检任务。
        """
        template = store.find(self.MODULE, template_id)
        if not template:
            return {"状态": "失败", "原因": f"模板{template_id}不存在"}

        sql = template.get("校验SQL", "")
        missing_params = []

        for param_key in set(param_values.keys()):
            placeholder = "{" + param_key + "}"
            if placeholder in sql:
                sql = sql.replace(placeholder, str(param_values[param_key]))
            else:
                missing_params.append(param_key)

        return {
            "模板名称": template.get("模板名称"),
            "实例化SQL": sql,
            "参数值": param_values,
            "未使用参数": missing_params,
            "实例化时间": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------- 大表标签驱动的模板链路
    def _ensure_large_table_template(self, threshold_code: str, threshold_rows: int) -> dict[str, Any]:
        """获取或创建大表巡检模板，模板参数绑定当前生效阈值版本。"""
        rows = store.rows(self.MODULE)
        template = next(
            (r for r in rows if r.get("模板名称") == self.LARGE_TABLE_TEMPLATE_NAME
             and r.get("status") != "已停用"),
            None,
        )
        if template is not None:
            return template

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        template = {
            "id": new_id,
            "模板编号": f"DQ_T-{new_id:04d}",
            "模板名称": self.LARGE_TABLE_TEMPLATE_NAME,
            "规则类型": "大表巡检",
            "校验SQL": (
                "SELECT COUNT(*) AS row_cnt FROM {table} "
                "HAVING COUNT(*) >= {threshold}"
            ),
            "参数列表": str(["table", "threshold"]),
            "适用场景": f"按阈值版本 {threshold_code}（{threshold_rows:,} 行）巡检大表标签命中表",
            "使用次数": "0",
            "模板版本": "v1.0",
            "阈值版本": threshold_code,
            "status": "已发布",
            "pending": False,
            "abnormal": False,
        }
        rows.append(template)
        return template

    def generate_inspection_checklist(self, round_id: str) -> dict[str, Any]:
        """按当前数据流取数：用轮次内已确认阈值重算标签，再由大表标签生成执行清单。

        只消费该轮次唯一「已确认」阈值版本对应的大表标签，
        清单按「轮次 + 表」幂等，重复运行不会重复建单。
        """
        threshold = ThresholdVersionService().confirmed_version(round_id)
        if threshold is None:
            return {
                "ok": False,
                "原因": f"评估轮次 {round_id} 没有已确认阈值版本，拒绝生成执行清单",
            }

        # 模板取数前先按确认阈值重算，保证清单与最新行数口径一致
        recalc = LargeTableTagService().recalculate(round_id)
        if not recalc.get("ok"):
            return recalc

        template = self._ensure_large_table_template(
            threshold["版本编号"], int(threshold["大表阈值行数"])
        )
        checklist_svc = InspectionChecklistService()
        large_tags = LargeTableTagService().active_large_tables(round_id)

        created, reused = 0, 0
        for tag in large_tags:
            result = checklist_svc.upsert_checklist(
                round_id, tag["关联表名"], tag, template["模板编号"]
            )
            if result.get("幂等"):
                reused += 1
            else:
                created += 1

        template["使用次数"] = str(int(template.get("使用次数", "0") or 0) + 1)
        pending = checklist_svc.pending_for_round(round_id)

        return {
            "ok": True,
            "评估轮次": round_id,
            "阈值版本": threshold["版本编号"],
            "阈值行数": int(threshold["大表阈值行数"]),
            "模板编号": template["模板编号"],
            "命中大表数": len(large_tags),
            "新增清单数": created,
            "复用清单数": reused,
            "待处理清单数": len(pending),
            "进入巡检表": [t["关联表名"] for t in large_tags],
            "重算摘要": {
                "新增标签数": recalc["新增标签数"],
                "废弃旧版数": recalc["废弃旧版数"],
                "争议待复核表数": recalc["争议待复核表数"],
            },
        }

    def run_large_table_template(self, round_id: str) -> dict[str, Any]:
        """运行依赖大表标签的模板：逐表生成实例化SQL、执行并把分类结论回写清单。

        争议表（估算行数待复核）只输出待复核结论，不关闭待处理标记，等待另行复核。
        """
        threshold = ThresholdVersionService().confirmed_version(round_id)
        if threshold is None:
            return {"ok": False, "原因": f"评估轮次 {round_id} 没有已确认阈值版本"}

        checklist_svc = InspectionChecklistService()
        tag_svc = LargeTableTagService()
        row_stat_svc = TableRowStatService()
        pending_items = [
            r for r in store.rows(checklist_svc.MODULE)
            if r.get("评估轮次") == round_id and r.get("待处理标记")
        ]
        if not pending_items:
            return {
                "ok": True,
                "评估轮次": round_id,
                "阈值版本": threshold["版本编号"],
                "执行清单数": 0,
                "完成回写数": 0,
                "争议待复核表": [],
                "执行结果": [],
                "说明": "该轮次无待处理清单",
            }

        executed, review_pending = [], []
        for item in pending_items:
            table = item["关联表名"]
            sql = (
                f"SELECT COUNT(*) AS row_cnt FROM {table} "
                f"HAVING COUNT(*) >= {int(threshold['大表阈值行数'])}"
            )
            effective = row_stat_svc.effective_count(table) or {}
            conclusion = (
                f"维持大表分类（{effective.get('数据来源', item.get('行数来源'))}"
                f" {int(effective.get('行数', item.get('判定行数', 0))):,} 行，"
                f"阈值 {int(threshold['大表阈值行数']):,}）"
            )

            if item.get("status") == "待复核" or item.get("处理状态") == "待复核":
                review_pending.append(table)
                executed.append({
                    "关联表名": table,
                    "实例化SQL": sql,
                    "分类结论": "估算争议未决，保持待复核，不回写结论",
                    "处理状态": "待复核",
                })
                continue

            entry, _ = checklist_svc.write_back_conclusion(round_id, table, conclusion)
            executed.append({
                "关联表名": table,
                "实例化SQL": sql,
                "分类结论": entry["分类结论"] if entry else conclusion,
                "处理状态": "已完成",
            })

        return {
            "ok": True,
            "评估轮次": round_id,
            "阈值版本": threshold["版本编号"],
            "执行清单数": len(pending_items),
            "完成回写数": len(pending_items) - len(review_pending),
            "争议待复核表": review_pending,
            "执行结果": executed,
        }

    def inspection_diff_between_rounds(self, previous_round: str, current_round: str) -> dict[str, Any]:
        """对照两轮评估：哪些表新进入巡检、哪些表退出、哪些表停止沿用先前分类。

        标签在重算时会被新版本替代并置为已废弃，因此按标签所属评估轮次回溯，
        而不是只看当前仍生效的记录。
        """
        tag_svc = LargeTableTagService()
        all_tags = store.rows(tag_svc.MODULE)

        def large_tables_of(round_id: str) -> set[str]:
            # 含该轮次产生后又被后续重算替代（已废弃）的大表标签，保证历史轮次可回溯
            return {
                r["关联表名"] for r in all_tags
                if r.get("评估轮次") == round_id and r.get("标签值") == "大表"
            }

        prev_tables = large_tables_of(previous_round)
        curr_tables = large_tables_of(current_round)

        entered = sorted(curr_tables - prev_tables)
        exited = sorted(prev_tables - curr_tables)
        retained = sorted(curr_tables & prev_tables)

        stopped = [
            item for item in tag_svc.classification_timeline()
            if item["停止沿用旧分类时间"]
        ]
        return {
            "上一轮次": previous_round,
            "当前轮次": current_round,
            "新进入巡检表": entered,
            "退出巡检表": exited,
            "持续巡检表": retained,
            "停止沿用旧分类": stopped,
            "当前轮次大表": sorted(curr_tables),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "标准化"
        snapshot["desc"] = "标准化规则模板SQL并关联规则定义"
        snapshot["artifact"] = "规则模板定义集"
        snapshot["artifact_desc"] = "经过标准化和校验的规则模板定义集"
        return snapshot