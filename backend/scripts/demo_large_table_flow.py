"""端到端场景演示：大表标签从行数聚合到模板巡检的完整数据流。

运行（在 backend 目录下）：
    .venv/bin/python -I scripts/demo_large_table_flow.py

场景设定：
- 表 A ods_est_legacy：全程只有 1500 万行的旧估算；
- 表 B dws_measured：先有 1800 万估算，后补齐同一时间口径的 2350 万实测；
- 表 C ads_small：300 万行小表对照；
- 轮次 R1 阈值 1000 万；同轮申请上调 2000 万，验证并发唯一确认被拒；
- 轮次 R2 正式确认 2000 万，重算标签、重建模板清单、运行并回写结论，
  对照实测补回前后哪些表进入巡检、何时停止沿用先前分类。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.dq_template import DqTemplateService
from app.services.inspection_checklist import InspectionChecklistService
from app.services.large_table_tag import LargeTableTagService
from app.services.table_row_stat import TableRowStatService
from app.services.threshold_version import ThresholdVersionService
from app.store import store

ROUND_1 = "R-20261121-1"
ROUND_2 = "R-20261121-2"
T0 = "2026-11-21T08:00:00+00:00"
T1 = "2026-11-21T09:00:00+00:00"
T2 = "2026-11-21T10:00:00+00:00"
T3 = "2026-11-21T11:00:00+00:00"
TABLE_A = "ods_est_legacy"
TABLE_B = "dws_measured"
TABLE_C = "ads_small"


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_row_stats() -> None:
    for r in store.rows("table_row_stat"):
        print(
            f"  {r['统计编号']} | {r['关联表名']:<16} | {r['数据来源']} | "
            f"{int(r['行数']):>12,} | {r['统计时间']} | {r['status']:<5} | "
            f"替代:{r.get('替代编号') or '—'}"
        )


def show_tags() -> None:
    for r in store.rows("large_table_tag"):
        print(
            f"  {r['标签编号']} | {r['关联表名']:<16} | {r['标签值']:<4} | "
            f"{r['标签版本']} | 阈值版本 {r['阈值版本']} | 依据:{r['行数来源']} "
            f"{int(r['判定行数']):>12,} | {r['status']:<5} | 废弃于 {r['废弃时间'] or '—'}"
        )


def main() -> None:
    # 演示使用独立内存态，清空链路表避免种子数据干扰
    for module in ("table_row_stat", "threshold_version", "large_table_tag",
                   "inspection_checklist", "dq_template"):
        store.rows(module).clear()

    row_stat = TableRowStatService()
    threshold_svc = ThresholdVersionService()
    tag_svc = LargeTableTagService()
    template_svc = DqTemplateService()

    line("步骤1 聚合目录扫描（估算口径，重复扫描幂等）")
    scan = [
        {"表名称": TABLE_A, "数据行数": "15,000,000"},
        {"表名称": TABLE_B, "数据行数": "18,000,000"},
        {"表名称": TABLE_C, "数据行数": "3,000,000"},
    ]
    first = row_stat.aggregate_directory_scan(scan, batch_id="SCAN-20261121-1", stat_time=T0)
    second = row_stat.aggregate_directory_scan(scan, batch_id="SCAN-20261121-1", stat_time=T0)
    print(f"  首次扫描新增 {first['新增统计数']} 条；重复扫描新增 {second['新增统计数']} 条、"
          f"跳过 {second['重复跳过数']} 条")
    show_row_stats()

    line("步骤2 轮次R1阈值1000万；同轮再提2000万，并发确认只允许一个版本进入模板")
    v1 = threshold_svc.register_threshold(ROUND_1, 10_000_000, "初版1000万", submitted_at=T0)
    v_dup = threshold_svc.register_threshold(ROUND_1, 20_000_000, "同轮上调2000万", submitted_at=T2)
    v1_id = next(r["id"] for r in store.rows("threshold_version")
                 if r["版本编号"] == v1["版本编号"])
    v_dup_id = next(r["id"] for r in store.rows("threshold_version")
                    if r["版本编号"] == v_dup["版本编号"])
    threshold_svc.confirm_threshold(v1_id)
    rejected, msg = threshold_svc.confirm_threshold(v_dup_id)
    print(f"  同轮直接确认2000万版本被拒：{msg}")
    print(f"  R1 唯一生效版本：{threshold_svc.confirmed_version(ROUND_1)['版本编号']}")

    line("步骤3 按1000万口径重算并生成模板执行清单（实测补回前）")
    tag_svc.recalculate(ROUND_1, run_at=T1)
    before = template_svc.generate_inspection_checklist(ROUND_1)
    print(f"  进入巡检的表：{before['进入巡检表']}")
    print(f"  命中大表 {before['命中大表数']} 张，待处理清单 {before['待处理清单数']} 条")

    line("步骤4 表B补齐同一时间口径实测；表A仍只有1500万旧估算")
    backfill = row_stat.register_measured_count(TABLE_B, 23_500_000, T0, "MEASURE-20261121")
    print(f"  实测记录 {backfill['统计编号']}，替代估算 {backfill['替代估算编号']}")
    show_row_stats()

    line("步骤5 新一轮R2确认2000万阈值，重算标签（版本化，旧分类废弃）")
    v2 = threshold_svc.register_threshold(ROUND_2, 20_000_000, "正式上调2000万", submitted_at=T3)
    v2_id = next(r["id"] for r in store.rows("threshold_version")
                 if r["版本编号"] == v2["版本编号"])
    threshold_svc.confirm_threshold(v2_id)
    recalc = tag_svc.recalculate(ROUND_2, run_at=T3)
    print(f"  新增标签 {recalc['新增标签数']}，废弃旧版 {recalc['废弃旧版数']}，"
          f"争议待复核 {recalc['争议待复核表数']}")
    show_tags()

    line("步骤6 模板按新标签重建执行清单并运行，分类结论回写待处理清单")
    after = template_svc.generate_inspection_checklist(ROUND_2)
    print(f"  实测补回+阈值变更后进入巡检的表：{after['进入巡检表']}")
    run = template_svc.run_large_table_template(ROUND_2)
    for item in run["执行结果"]:
        print(f"  {item['关联表名']:<16} -> {item['处理状态']} | {item['分类结论']}")

    line("步骤7 轮次对照与时间线：进入/退出巡检、何时停止沿用先前分类")
    diff = template_svc.inspection_diff_between_rounds(ROUND_1, ROUND_2)
    print(f"  新进入巡检：{diff['新进入巡检表']}")
    print(f"  退出巡检：{diff['退出巡检表']}")
    print(f"  持续巡检：{diff['持续巡检表']}")
    for item in tag_svc.classification_timeline():
        print(
            f"  {item['关联表名']:<16} | 当前 {item['当前标签值']:<4} {item['当前标签版本']} | "
            f"进入巡检 {item['进入巡检时间'] or '—'} | "
            f"停止旧分类 {item['停止沿用旧分类时间'] or '—'} | 历史版本 {item['历史版本数']}"
        )

    line("步骤8 两轮待处理清单最终状态（结论回写后关闭）")
    for r in store.rows("inspection_checklist"):
        print(
            f"  {r['清单编号']} | {r['评估轮次']} | {r['关联表名']:<16} | "
            f"{r['处理状态']:<5} | 待处理 {r['待处理标记']} | 结论: {r['分类结论'] or '—'}"
        )

    line("结论")
    print("  - 行数统计区分估算/实测，同口径实测补回后估算过期且保留替代关系；")
    print("  - 同一评估轮次并发确认保证只有一个阈值版本进入模板；")
    print("  - 表A 因 1500 万估算低于 2000 万新阈值掉出巡检，旧标签废弃时间即停止沿用时间；")
    print("  - 表B 以 2350 万实测保留大表分类，落库标签明确标注行数来源为实测。")


if __name__ == "__main__":
    main()
