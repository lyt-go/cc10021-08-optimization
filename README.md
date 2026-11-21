# 数据质量监控与元数据治理平台

面向数据质量标准定义、自动巡检、异常告警、元数据采集与数据目录构建的数据质量治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 质量规则定义 | `dq_rule_def` | 质量规则 | 规则编号、规则名称、目标表名 |
| 巡检任务管理 | `dq_check_run` | 巡检任务 | 任务编号、关联规则集、数据范围 |
| 质量事件管理 | `dq_incident` | 质量事件 | 事件编号、关联规则、触发时间 |
| 元数据采集器 | `metadata_crawler` | 采集器配置 | 采集器编号、数据源类型、连接地址 |
| 数据表目录 | `table_catalog` | 表目录 | 表编号、表名称、所属库名 |
| 字段画像分析 | `column_profile` | 字段画像 | 画像编号、关联表名、字段名称 |
| 血缘图谱 | `lineage_graph` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 指标波动监控 | `metric_monitor` | 监控指标 | 指标编号、目标表名、监控字段 |
| 质量评分卡 | `dq_scorecard` | 评分记录 | 记录编号、目标表名、完整性得分 |
| 数据分类标签 | `tag_classify` | 分类标签 | 标签编号、标签名称、分类维度 |
| 规则模板库 | `dq_template` | 规则模板 | 模板编号、模板名称、规则类型 |
| 异常模式识别 | `anomaly_pattern` | 异常模式 | 模式编号、模式名称、检测方法 |
| 质量报告生成 | `dq_report` | 质量报告 | 报告编号、报告周期、覆盖表数 |
| 元数据变更对比 | `metadata_diff` | 变更记录 | 变更编号、变更对象、变更类型 |
| 告警通知通道 | `dq_alert_channel` | 告警配置 | 配置编号、关联规则集、通知方式 |
| 质量基线管理 | `dq_baseline` | 基线定义 | 基线编号、目标表名、基线指标 |
| 主数据实体注册 | `mdm_entity` | 主数据实体 | 实体编号、实体名称、业务域 |
| 质量趋势看板 | `dq_trend` | 趋势记录 | 记录编号、目标表名、日期 |
| 表行数统计 | `table_row_stat` | 行数统计 | 统计编号、关联表名、数据来源（估算/实测）、行数、统计时间、来源批次 |
| 大表阈值版本 | `threshold_version` | 阈值版本 | 版本编号、评估轮次、大表阈值行数、版本状态 |
| 大表标签 | `large_table_tag` | 大表标签 | 标签编号、关联表名、标签值、阈值版本、行数来源、标签版本、废弃时间 |
| 模板待处理清单 | `inspection_checklist` | 巡检清单 | 清单编号、评估轮次、关联表名、处理状态、分类结论 |

## 大表标签数据流（可重算）

旧实现按目录里的行数字符串和硬编码 1000 万阈值现场判定大表，重复扫描会重复落库、
阈值变更后旧分类也难以对照。现行数据流改为四段可重算链路：

```text
目录扫描/实测补回                阈值并发确认              标签重算                  模板巡检
table_row_stat ──聚合(幂等)──▶ threshold_version ──唯一确认──▶ large_table_tag ──取数──▶ dq_template
  估算/实测同口径替代             同轮只允许一个版本          版本化、旧版废弃留痕        inspection_checklist
  落库即解释行数来源              THRESHOLD_LOCK 互斥         兼容历史标签版本            结论回写关闭待办
```

关键约定：

- **行数聚合幂等**：按「表 + 来源批次 + 来源」去重，重复扫描目录不再产生重复统计；
  实测补回同一时间口径数据时，旧估算置「已过期」并以`替代编号`指向实测记录，历史可追溯。
- **阈值唯一确认**：同一评估轮次内只允许一个阈值版本进入「已确认」，确认与标签重算共用
  `THRESHOLD_LOCK`；同轮阈值从 1000 万上调 2000 万时，新版本必须等旧版本停用后才能确认。
- **标签版本化**：重算只消费已确认阈值，结论翻转时新版本生效、旧版本废弃，
  旧版`废弃时间`即「停止沿用先前分类」的时间点；每条标签记录行数来自估算还是实测。
- **清单回写**：规则模板用大表标签生成执行清单（轮次+表幂等），运行后分类结论回写清单；
  估算行数有争议的表保持「待复核」，不回写结论，等待另行复核。
- 端到端演示：`backend/scripts/demo_large_table_flow.py`；
  链路测试：`backend/tests/test_large_table_pipeline.py`。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
