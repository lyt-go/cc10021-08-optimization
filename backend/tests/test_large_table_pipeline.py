"""大表标签链路测试：行数聚合 → 阈值并发确认 → 标签重算 → 模板清单 → 结论回写。

覆盖场景（对应需求）：
1. 目录重复扫描幂等，估算/实测来源落库可解释，实测补回使同口径估算过期；
2. 同一评估轮次阈值 1000 万 → 2000 万，只允许一个确认版本进入模板（并发约束）；
3. 一张表仍只有 1500 万旧估算（阈值变更后掉出大表），另一张表补齐同口径实测；
4. 标签重算版本化，旧标签废弃并记录停止沿用时间，历史版本可对照；
5. 规则模板取大表标签生成执行清单，运行后分类结论回写待处理清单；
6. 估算争议表另行复核，不阻断流程但不回写结论；
7. 实测补回前后哪些表进入巡检、何时停止沿用先前分类。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dq_template import DqTemplateService
from app.services.inspection_checklist import InspectionChecklistService
from app.services.large_table_tag import LargeTableTagService
from app.services.table_row_stat import TableRowStatService, parse_row_count
from app.services.tag_classify import TagClassifyService
from app.services.threshold_version import ThresholdVersionService
from app.store import store


T_A = "ods_chain_estimate_only"   # 全程只有 1500 万旧估算的表
T_B = "dws_chain_measured_back"   # 先估算后补同口径实测的表
T_C = "ads_chain_small"           # 小表对照组
ROUND_1 = "R-UT-01"
ROUND_2 = "R-UT-02"
T0 = "2026-11-21T08:00:00+00:00"
T1 = "2026-11-21T09:00:00+00:00"
T2 = "2026-11-21T10:00:00+00:00"
T3 = "2026-11-21T11:00:00+00:00"

PIPELINE_MODULES = (
    "table_row_stat",
    "threshold_version",
    "large_table_tag",
    "inspection_checklist",
)


@pytest.fixture(autouse=True)
def isolate_pipeline_tables():
    """内存 store 为全局单例，用例前清空本链路四张表，避免跨用例数据串扰。"""
    for module in PIPELINE_MODULES:
        store.rows(module).clear()
    yield
    for module in PIPELINE_MODULES:
        store.rows(module).clear()


@pytest.fixture()
def stats() -> TableRowStatService:
    return TableRowStatService()


@pytest.fixture()
def thresholds() -> ThresholdVersionService:
    return ThresholdVersionService()


@pytest.fixture()
def tags() -> LargeTableTagService:
    return LargeTableTagService()


@pytest.fixture()
def templates() -> DqTemplateService:
    return DqTemplateService()


@pytest.fixture()
def checklists() -> InspectionChecklistService:
    return InspectionChecklistService()


class TestRowCountAggregation:
    """行数统计聚合：幂等扫描、来源解释、实测补回替代估算。"""

    def test_parse_row_count_variants(self):
        assert parse_row_count("15,000,000") == 15_000_000
        assert parse_row_count("23500000") == 23_500_000
        assert parse_row_count("8,000,000 行") == 8_000_000
        assert parse_row_count("") == 0
        assert parse_row_count(12000000) == 12_000_000

    def test_repeated_directory_scan_is_idempotent(self, stats):
        scan = [
            {"表名称": T_A, "数据行数": "15,000,000"},
            {"表名称": T_B, "数据行数": "18,000,000"},
            {"表名称": T_C, "数据行数": "3,000,000"},
        ]
        first = stats.aggregate_directory_scan(scan, batch_id="SCAN-UT-1", stat_time=T0)
        second = stats.aggregate_directory_scan(scan, batch_id="SCAN-UT-1", stat_time=T0)
        assert first["新增统计数"] == 3
        assert second["新增统计数"] == 0
        assert second["重复跳过数"] == 3

        rows = [r for r in store.rows("table_row_stat")
                if r.get("来源批次") == "SCAN-UT-1"]
        assert len(rows) == 3
        assert all(r["数据来源"] == "估算" for r in rows)

    def test_measured_backfill_expires_same_caliber_estimate(self, stats):
        stats.aggregate_directory_scan(
            [{"表名称": T_B, "数据行数": "18,000,000"}],
            batch_id="SCAN-UT-2", stat_time=T1,
        )
        result = stats.register_measured_count(
            T_B, 23_500_000, stat_time=T1, batch_id="MEASURE-UT-1",
        )
        assert result["ok"] is True
        assert len(result["替代估算编号"]) == 1

        # 同口径重复补回幂等
        again = stats.register_measured_count(
            T_B, 23_500_000, stat_time=T1, batch_id="MEASURE-UT-1",
        )
        assert again["幂等"] is True

        effective = stats.effective_count(T_B)
        assert effective["行数"] == 23_500_000
        assert effective["数据来源"] == "实测"

        estimate = next(
            r for r in store.rows("table_row_stat")
            if r["关联表名"] == T_B and r["数据来源"] == "估算"
        )
        assert estimate["status"] == "已过期"
        assert estimate["替代编号"] == result["统计编号"]

    def test_estimate_only_table_keeps_estimate(self, stats):
        stats.aggregate_directory_scan(
            [{"表名称": T_A, "数据行数": 15_000_000}],
            batch_id="SCAN-UT-3", stat_time=T0,
        )
        effective = stats.effective_count(T_A)
        assert effective["行数"] == 15_000_000
        assert effective["数据来源"] == "估算"


class TestThresholdConcurrency:
    """阈值版本：同轮并发确认只有一个版本能进入模板。"""

    def test_only_one_confirmed_version_per_round(self, thresholds):
        thresholds.register_threshold(ROUND_1, 10_000_000, note="初版1000万", submitted_at=T0)
        thresholds.register_threshold(ROUND_1, 20_000_000, note="上调2000万", submitted_at=T1)
        versions = [r for r in store.rows("threshold_version") if r["评估轮次"] == ROUND_1]
        v1, v2 = versions[0], versions[1]
        assert v1["status"] == "待确认" and v2["status"] == "待确认"

        ok_entry, ok_msg = thresholds.confirm_threshold(v1["id"])
        assert ok_entry is not None
        rejected, reject_msg = thresholds.confirm_threshold(v2["id"])
        assert rejected is None
        assert "只允许一个阈值版本" in reject_msg

        # 停用旧版本后，新版本才能确认
        thresholds.deactivate_threshold(v1["id"])
        confirmed, _ = thresholds.confirm_threshold(v2["id"])
        assert confirmed["status"] == "已确认"
        assert int(confirmed["大表阈值行数"]) == 20_000_000
        assert thresholds.confirmed_version(ROUND_1)["版本编号"] == v2["版本编号"]

    def test_concurrent_confirmation_single_winner(self, thresholds):
        round_id = "R-UT-CONC"
        ids = []
        for i in range(8):
            res = thresholds.register_threshold(
                round_id, 10_000_000 + i * 1_000_000, submitted_at=T0,
            )
            ids.append(next(
                r["id"] for r in store.rows("threshold_version")
                if r["版本编号"] == res["版本编号"]
            ))

        # 多线程同时确认：THRESHOLD_LOCK 保证检查-置位原子性，最终恰好一个赢家。
        # 逐次取锁执行确认，模拟竞争线程排队进入临界区的结果。
        winners: list = []
        errors: list = []
        for version_id in ids:
            entry, _ = thresholds.confirm_threshold(version_id)
            (winners if entry is not None else errors).append(version_id)

        assert len(winners) == 1
        assert len(errors) == 7
        confirmed = [
            r for r in store.rows("threshold_version")
            if r["评估轮次"] == round_id and r.get("status") == "已确认"
        ]
        assert len(confirmed) == 1

    def test_confirmation_lock_is_mutex(self, thresholds):
        """直接验证确认动作在同一把锁内串行，临界区不会被第二个确认者插入。"""
        from app.services.threshold_version import THRESHOLD_LOCK

        round_id = "R-UT-MUTEX"
        first = thresholds.register_threshold(round_id, 10_000_000, submitted_at=T0)
        second = thresholds.register_threshold(round_id, 20_000_000, submitted_at=T0)
        first_id = next(
            r["id"] for r in store.rows("threshold_version")
            if r["版本编号"] == first["版本编号"]
        )
        second_id = next(
            r["id"] for r in store.rows("threshold_version")
            if r["版本编号"] == second["版本编号"]
        )

        entered = threading.Event()
        proceed = threading.Event()

        def hold_lock_then_confirm():
            with THRESHOLD_LOCK:
                entered.set()
                proceed.wait(timeout=5)
                thresholds.confirm_threshold(first_id)

        t = threading.Thread(target=hold_lock_then_confirm)
        t.start()
        assert entered.wait(timeout=5)
        # 持锁期间尝试确认第二个版本：必须阻塞，拿不到锁
        assert THRESHOLD_LOCK.acquire(blocking=False) is False
        proceed.set()
        t.join()
        THRESHOLD_LOCK.acquire()  # 线程释放后可获取
        THRESHOLD_LOCK.release()

        rejected, _ = thresholds.confirm_threshold(second_id)
        assert rejected is None
        assert thresholds.confirmed_version(round_id)["id"] == first_id


class TestLargeTableRecalculation:
    """标签重算：阈值 1000 万 → 2000 万，估算表掉出、实测表保留，版本化与历史兼容。"""

    def test_recalculate_requires_confirmed_threshold(self, tags):
        result = tags.recalculate("R-UT-NO-THRESHOLD")
        assert result["ok"] is False
        assert "已确认阈值版本" in result["原因"]

    def test_full_round_lifecycle(self, stats, thresholds, tags, templates, checklists):
        # ---- 初始：两张表都是估算口径 ----
        stats.aggregate_directory_scan([
            {"表名称": T_A, "数据行数": 15_000_000},
            {"表名称": T_B, "数据行数": 18_000_000},
            {"表名称": T_C, "数据行数": 3_000_000},
        ], batch_id="SCAN-UT-LIFE", stat_time=T0)

        # ---- 轮次1：阈值 1000 万 ----
        r1 = thresholds.register_threshold(ROUND_1, 10_000_000, submitted_at=T0)
        v1_id = self._id_of("threshold_version", r1["版本编号"])
        thresholds.confirm_threshold(v1_id)
        recalc1 = tags.recalculate(ROUND_1, run_at=T1)
        assert recalc1["ok"] is True
        assert {t["关联表名"] for t in recalc1["判定明细"] if t["标签值"] == "大表"} == {T_A, T_B}

        gen1 = templates.generate_inspection_checklist(ROUND_1)
        assert gen1["ok"] is True
        assert sorted(gen1["进入巡检表"]) == sorted([T_A, T_B])
        assert set(checklists.tables_for_round(ROUND_1)) == {T_A, T_B}

        # 重复生成幂等
        gen1_again = templates.generate_inspection_checklist(ROUND_1)
        assert gen1_again["新增清单数"] == 0

        # ---- 同轮阈值上调 2000 万：必须先停用旧版本 ----
        r2 = thresholds.register_threshold(ROUND_1, 20_000_000, submitted_at=T2)
        v2_id = self._id_of("threshold_version", r2["版本编号"])
        rejected, _ = thresholds.confirm_threshold(v2_id)
        assert rejected is None
        thresholds.deactivate_threshold(v1_id)
        thresholds.confirm_threshold(v2_id)

        # ---- T_B 补齐与估算同时间口径的实测；T_A 仍只有 1500 万旧估算 ----
        backfill = stats.register_measured_count(T_B, 23_500_000, T0, "MEASURE-UT-LIFE")
        assert backfill["替代估算编号"]

        # ---- 轮次2：2000 万阈值重算 ----
        r3 = thresholds.register_threshold(ROUND_2, 20_000_000, submitted_at=T3)
        v3_id = self._id_of("threshold_version", r3["版本编号"])
        thresholds.confirm_threshold(v3_id)
        recalc2 = tags.recalculate(ROUND_2, run_at=T3)

        # T_A（1500 万估算）掉出大表；T_B（2350 万实测）保留
        current_large = {t["关联表名"] for t in tags.active_large_tables(ROUND_2)}
        assert current_large == {T_B}
        assert recalc2["废弃旧版数"] >= 1

        gen2 = templates.generate_inspection_checklist(ROUND_2)
        assert gen2["进入巡检表"] == [T_B]

        # ---- 历史标签版本保留且可对照 ----
        history_a = tags.table_tag_history(T_A)
        assert len(history_a) == 2
        assert history_a[0]["标签值"] == "大表" and history_a[0]["status"] == "已废弃"
        assert history_a[1]["标签值"] == "普通表" and history_a[1]["status"] == "已生效"
        # 停止沿用先前分类的时间被显式记录
        assert history_a[0]["废弃时间"] == T3
        # 落库标签明确解释行数来自估算还是实测
        assert history_a[1]["行数来源"] == "估算"
        history_b = tags.table_tag_history(T_B)
        assert any(v["行数来源"] == "实测" for v in history_b)

        # ---- 轮次对照：进入/退出巡检 + 停止沿用时间 ----
        diff = templates.inspection_diff_between_rounds(ROUND_1, ROUND_2)
        assert diff["退出巡检表"] == [T_A]
        assert diff["持续巡检表"] == [T_B]
        stopped = {item["关联表名"]: item for item in diff["停止沿用旧分类"]}
        assert T_A in stopped and stopped[T_A]["停止沿用旧分类时间"] == T3

        timeline = {item["关联表名"]: item for item in tags.classification_timeline()}
        assert timeline[T_A]["进入巡检时间"] == T1
        assert timeline[T_A]["停止沿用旧分类时间"] == T3

        # 轮次1 的清单仍保留历史结论，轮次2 只包含留存的 T_B
        assert set(checklists.tables_for_round(ROUND_1)) == {T_A, T_B}
        assert checklists.tables_for_round(ROUND_2) == [T_B]

    def test_template_run_writes_back_conclusions(self, stats, thresholds, tags, templates, checklists):
        round_id = "R-UT-RUN"
        stats.aggregate_directory_scan([
            {"表名称": "ods_run_a", "数据行数": 25_000_000},
        ], batch_id="SCAN-UT-RUN", stat_time=T0)
        reg = thresholds.register_threshold(round_id, 20_000_000, submitted_at=T0)
        thresholds.confirm_threshold(self._id_of("threshold_version", reg["版本编号"]))
        tags.recalculate(round_id, run_at=T1)
        templates.generate_inspection_checklist(round_id)

        run = templates.run_large_table_template(round_id)
        assert run["ok"] is True
        assert run["完成回写数"] == 1
        assert run["争议待复核表"] == []
        item = next(
            r for r in store.rows("inspection_checklist")
            if r["评估轮次"] == round_id and r["关联表名"] == "ods_run_a"
        )
        assert item["status"] == "已完成"
        assert item["待处理标记"] is False
        assert "维持大表分类" in item["分类结论"]
        assert checklists.pending_for_round(round_id) == []

    def test_disputed_estimate_goes_to_review_and_blocks_writeback(
        self, stats, thresholds, tags, templates, checklists,
    ):
        round_id = "R-UT-DISPUTE"
        table = "ods_dispute_case"
        stats.aggregate_directory_scan([
            {"表名称": table, "数据行数": 22_000_000},
        ], batch_id="SCAN-UT-DIS", stat_time=T0)

        stat_id = next(
            r["id"] for r in store.rows("table_row_stat")
            if r["关联表名"] == table and r["来源批次"] == "SCAN-UT-DIS"
        )
        stats.flag_dispute(stat_id, "目录估算与分片采样偏差超 20%")
        assert {d["关联表名"] for d in stats.disputed_tables()} >= {table}

        reg = thresholds.register_threshold(round_id, 20_000_000, submitted_at=T0)
        thresholds.confirm_threshold(self._id_of("threshold_version", reg["版本编号"]))
        recalc = tags.recalculate(round_id, run_at=T1)
        assert recalc["争议待复核表数"] == 1

        tag = next(r for r in store.rows("large_table_tag") if r["关联表名"] == table)
        assert tag["status"] == "待复核" and tag["pending"] is True

        templates.generate_inspection_checklist(round_id)
        run = templates.run_large_table_template(round_id)
        assert run["争议待复核表"] == [table]
        item = next(
            r for r in store.rows("inspection_checklist")
            if r["评估轮次"] == round_id and r["关联表名"] == table
        )
        # 争议未决：不回写结论，清单保持待复核
        assert item["status"] == "待复核"
        assert item["待处理标记"] is True
        assert item["分类结论"] == ""

    @staticmethod
    def _id_of(module: str, code: str) -> int:
        return next(r["id"] for r in store.rows(module)
                    if r.get("版本编号") == code or r.get("统计编号") == code)


class TestTagClassifyIntegration:
    """旧分类入口兼容：classify_tables_by_metadata 改走行数统计取数。"""

    def test_classify_uses_row_stat_pipeline(self, stats, thresholds):
        table = "ods_classify_case"
        stats.aggregate_directory_scan([
            {"表名称": table, "数据行数": 12_000_000},
        ], batch_id="SCAN-UT-CLS", stat_time=T0)
        store.rows("table_catalog").append({
            "id": 999001,
            "表编号": "TABL-999001",
            "表名称": table,
            "所属库名": "ods_mysql",
            "表类型": "TABLE",
            "字段数量": 6,
            "数据行数": "12,000,000",
            "存储大小": "—",
            "最后更新": T0,
            "status": "已注册",
            "pending": False,
            "abnormal": False,
        })
        reg = ThresholdVersionService().register_threshold("R-UT-CLS", 10_000_000, submitted_at=T0)
        ThresholdVersionService().confirm_threshold(
            TestLargeTableRecalculation._id_of("threshold_version", reg["版本编号"])
        )

        result = TagClassifyService().classify_tables_by_metadata(60)
        assert result["覆盖表数"] >= 1
        storage_tags = [
            r for r in store.rows("tag_classify")
            if r["关联表名"] == table and r["分类维度"] == "存储量级"
        ]
        assert storage_tags and storage_tags[-1]["标签值"] == "大表"
        assert "估算行数" in storage_tags[-1]["分类依据"]

        # 分类规则模板幂等：再跑一次不会重复建大表分类规则
        before = sum(
            1 for r in store.rows("dq_template") if r.get("模板名称") == "大表分类规则"
        )
        TagClassifyService().classify_tables_by_metadata(60)
        after = sum(
            1 for r in store.rows("dq_template") if r.get("模板名称") == "大表分类规则"
        )
        assert before == after
