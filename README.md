# schemaevo · Parquet 字段 ID 与类型演进映射

湖仓表治理小工具：上游提交 Parquet 表 schema 变更时，先算清「字段怎么变了、兼容级别是什么、迁移 SQL 按什么顺序执行、谁会读坏」。

## 能力

- 字段匹配：`field_id` 优先，无 id 时按名称精确匹配；改名、字段 ID 变更、新增、删除逐条标注依据。
- 类型提升矩阵：代码内显式表（int8/int16/int32/int64、float/double、decimal 精度、string→int、date→timestamp 等），表外类型对一律 `unknown`（不猜）。
- 兼容级别：按「旧写新读 / 新写旧读」双向判定，输出 `full` / `backward` / `forward` / `none`。
- 迁移计划：有序 `ALTER TABLE` 序列（改名 → 加列/默认值 → 回填 → 类型变更 → 删列最后）、需回填对象、读写方升级顺序；分区字段被删或改类型直接阻断。
- 列引用检查：数据文件清单里引用被删/被阻断列的 → `broken_consumers`；引用改名列的 → 需同步改名。
- **批次验收**：按业务批次把多表变更整体验收——按读取任务的关联键和投影分别对账新旧结果，差异逐项定位（关联错列/数据丢失/类型不再可关联），再决定整批能否发布；各表单独看都兼容 ≠ 合起来兼容。
- 验收重放语义：任务逐个执行逐个落盘，中断后续跑只执行未完成任务；失败任务隔离；半批结果不能标通过；失败时保留旧版通过结果，输入指纹变化后旧结论作废；重试新建验收不混用两次结果；复核记录与下载清单指向同一次验收。
- 5 个页面：首页（场景 + 样例 + 演示步骤）、对比工作台、结果页、批次验收工作台、复核与导出（单表历史 + 批次验收记录 + SQL/清单下载 + 矩阵说明）。
- 运行记录以 JSON 落在 `/app/data/runs/`、验收记录落在 `/app/data/acceptances/`（compose 命名卷），同一输入可反复重跑，历史可回看。

## 核心规则（纯函数模块）

| 模块 | 文件 | 规则 |
|---|---|---|
| 字段匹配 | `app/matching.py` | id 相同优先；id 同名称变 → renamed；名称同 id 变 → id_changed（人工确认）；无匹配 → added/removed |
| 类型矩阵 | `app/types.py` | 显式提升/阻断表；decimal 精度提升兼容、收窄阻断、标度变更 unknown |
| 兼容判定 | `app/compat.py` | 双向各自判定后合成级别；分区字段增删改类型双向阻断 |
| 迁移计划 | `app/migration.py` | 固定顺序出 ALTER，阻断项不产出 ALTER，删列最后 |
| 引用检查 | `app/consumers.py` | 逐文件逐列比对匹配结果，输出 broken / needs_update / unknown |
| 报表编排 | `app/report.py` | 汇总成可重放的运行记录 + 下载脚本 |
| 批次输入 | `app/batchio.py` | 批次（多表 + 读取任务）解析校验；输入指纹 sha256 |
| 跨表对账 | `app/reconcile.py` | 按任务的关联键/投影分别对账新旧结果；旧名被新列占用 → 关联错列；键/列被删 → 数据丢失 |
| 验收编排 | `app/acceptance.py` | 逐任务执行 + 断点续跑 + 失败隔离 + 整批结论 + 清单；旧结论作废推导 |
| 验收存储 | `app/accept_store.py` | 验收记录落 `data/acceptances/*.json`，最多保留 100 条 |

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18216/health
```

页面：<http://127.0.0.1:18216/>

## 演示步骤

1. 首页点「用样例会 B 执行对比」，工作台预填 `dwd_payment` 旧/新 schema 与数据文件清单。
2. 点「执行对比」→ 结果页：改名 1、新增 2、回填 1、`decimal(10,2)→(18,2)`，兼容级别 `backward`，ALTER 4 条。
3. 换成样例 C（`dwd_shipment`）→ 级别 `none`、3 条阻断原因、`broken_consumers` 若干、`can_execute=false`。
4. 到「复核与导出」看历史、下载 `*.sql`、查看完整矩阵；同一输入可重复执行。
5. 进「批次验收工作台」用样例 D 执行批次验收：三张表单独验收都是 `full`/`backward` 可执行，但整批对账出「关联错列 1 项 + 投影列丢失 1 项」→ 整批不可发布。
6. 在批次工作台填「演示中断 = 1」再执行：验收中断、半批结果不能标通过；到结果页点「续跑」只执行剩余任务；点「重试」会用同一输入新建验收从头执行。
7. 样例 E 是整批通过的对照组；复核页的「批次验收记录」与下载清单指向同一次验收。

样例：

| 样例 | 表 | 预期 |
|---|---|---|
| A | `dwd_orders` | `full`，0 条迁移 SQL |
| B | `dwd_payment` | `backward`，rename 1 / added 2 / 回填 1 / ALTER 4 |
| C | `dwd_shipment` | `none`，3 条阻断 + broken_consumers |
| D | 批次 `settle-2026-11`（3 表 3 任务） | 各表 `full`/`backward`，整批不可发布：关联错列 1 + 数据丢失 1 |
| E | 批次 `growth-2026-11`（2 表 1 任务） | 整批通过，1 处类型提升为提示级 |

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（场景、样例、演示步骤） |
| GET | `/workbench?sample=A|B|C` | 对比工作台（预填样例） |
| POST | `/api/compare` | `{sample}` 或 `{old,new,consumers}`；返回 `run_id`/`summary`/`redirect`；非法输入 400 |
| GET | `/result/{run_id}` | 结果页（匹配表、类型结论、兼容级别、迁移、broken_consumers） |
| GET | `/review` | 历史对比 + SQL 下载 + 矩阵说明 |
| GET | `/api/runs`、`/api/runs/{run_id}` | 运行记录 |
| GET | `/api/runs/{run_id}/migration.sql` | 下载迁移脚本 |
| GET | `/api/matrix` | 完整类型矩阵 |
| GET | `/api/samples/{key}` | 样例原文 |
| GET | `/batch?sample=D\|E` | 批次验收工作台（预填批次样例） |
| POST | `/api/acceptances` | `{sample}` 或 `{batch}`，可选 `simulate_interrupt_after`（演示中断）；返回 `acceptance_id`/`status`/`redirect`；非法输入 400 |
| POST | `/api/acceptances/{id}/resume` | 续跑：只执行未完成任务；没有待跑任务返回 400 |
| POST | `/api/acceptances/{id}/retry` | 重试：同一输入快照新建验收（新 id）从头执行，不混用旧结果 |
| GET | `/acceptance/{id}` | 验收结果页（各表对照、关联键/投影对账、差异逐项、续跑/重试） |
| GET | `/api/acceptances`、`/api/acceptances/{id}` | 验收记录 |
| GET | `/api/acceptances/{id}/manifest` | 下载验收清单（与复核记录同一 acceptance_id） |
| GET | `/api/batch-samples/{key}` | 批次样例原文 |
| GET | `/health` | 健康检查 |

## 目录

```text
app/            main.py + 10 个纯函数模块 + templates/ + static/
sample_data/    a/b/c 三组单表样例 + d/e 两组批次样例 + index.json
tests/          test_matching / test_types / test_compat / test_migration / test_api / test_acceptance
data/           运行记录 + 验收记录（本地直跑时生成，已 gitignore）
```

## 批次验收语义（与页面一致）

- **整体验收**：批次 = 多表 old/new（+ 可选 consumers）+ 读取任务清单（joins/projections）；任务引用的列以旧 schema 为准。
- **分别对账**：关联键（新旧口径各自解析到哪一列、能否关联）与投影（输出列是否还在、类型是否可读）两条线独立对账，差异逐项定位到 任务 → 关联/投影 → 表.列。
- **整批结论**：全部任务完成且无失败、无阻断、无待确认 → `passed`；否则 `rejected`；还有未完成任务 → `interrupted`（半批结果不能标通过）。
- **续跑 / 重试**：续跑只执行待跑任务（已完成/失败的不重跑）；重试 = 同一输入新建验收从头执行，两次结果互不混用。
- **旧结论**：整批失败/中断时，同批次最近一次通过仍生效；输入指纹变化后，旧通过结论自动作废并标注。

## 已知限制

- 只做 schema 级别的静态判定，不读取 Parquet 文件字节，也不校验数据文件真实 id 编码；`consumers.json` 里的列引用来自人工/采集清单。
- 类型矩阵是显式小表，未覆盖的合法类型对（如 `string→double`、`timestamp→date`）返回 `unknown`，需人工确认，不自动推断。
- `ALTER TABLE` 为通用 SQL 片段（Spark/Trino 语义），不连接具体引擎执行；回填动作只给建议 SQL 与对象清单。
- 运行记录保存最近 200 条、验收记录保存最近 100 条，超出按修改时间裁剪；`data/` 被 gitignore，容器重建需通过命名卷保留。
- 批次验收的「中断」是演示参数（`simulate_interrupt_after`：执行 N 个任务后模拟进程崩溃），用于演练断点续跑；真实部署中由进程崩溃自然产生，断点文件语义相同。
- 批次对账是 schema 级静态判定：关联键「可关联」按类型矩阵推导（任一方向可无损提升即可关联），不读取真实数据行验证关联基数。
- 无登录/权限，单机演示定位。
