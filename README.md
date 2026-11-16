# 广播受众估计服务（Audience Estimation Service）

由**虚构**的入户招募资料与个人收听日记，计算可复算的节目收听人数（reach）与人均收听时长，并在
报告中明确指出**哪些目标群体覆盖不足**，同时**拒绝**用这些数字比较两档节目的表现。

本服务另含**对外受众表格的披露风险检查**：研究主管发布地区、年龄与节目交叉表前，系统以
**未加权家庭数阈值 + 二次抑制** 的明确策略（不声称差分隐私）防止多表相减或联立还原出单个家庭行为，
经精确整数可行解验证后才允许导出。

不接入真实人口库、真实收听设备或任何真实个人数据。数据集见 `demo/fixtures.py`，全部为构造值。

- 技术栈：Python 3.12 / FastAPI / PostgreSQL 18 / psycopg 3（无 ORM，显式 SQL）
- 项目类型：**后端服务**，因此提供 OpenAPI 3.x 与 Swagger UI：
  - Swagger UI: `http://127.0.0.1:8000/docs`
  - OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`（OpenAPI 3.1.0，35 条路径）

---

## 1. 一键启动

### 1.1 Docker Compose（推荐）

```bash
docker compose up --build -d          # 启动 db + api，自动建表并写入虚构样本
docker compose ps                     # 等待 api 变为 healthy
curl -s http://127.0.0.1:8000/health  # {"status":"ok",...}
docker compose --profile demo run --rm demo   # 完整演示脚本（见第 5 节）
docker compose down -v                # 停止并清除容器
```

`api` 容器启动时执行 `docker/entrypoint.sh`：等待 PostgreSQL 就绪 → 幂等写入虚构数据（已写入则跳过）→ 启动 uvicorn。

### 1.2 本地开发（不使用 Docker）

容器内已预装 PostgreSQL 18（`127.0.0.1:5432`，用户/库/密码均为 `ccdev`）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export AUDIENCE_DATABASE_URL=postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev
export AUDIENCE_DB_SCHEMA=audience_dev      # 任意合法 schema，服务会自动建表
export AUDIENCE_ADMIN_API_KEY=dev-admin-key
export AUDIENCE_ANALYST_API_KEY=dev-analyst-key

.venv/bin/python scripts/seed.py --reset    # 重建 schema 并写入虚构数据
.venv/bin/uvicorn app.main:app --port 8000
```

`scripts/seed.py` 不带 `--reset` 时若库里已有批次会直接退出，不会重复写入。

---

## 2. 账号与鉴权

无需登录账号；使用 API Key（`X-API-Key` 请求头）：

| Key | 值 | 用途 |
| --- | --- | --- |
| 管理员 | `dev-admin-key` | 全部接口 |
| 分析员 | `dev-analyst-key` | 全部接口（当前实现两者权限相同，便于演示） |

`/health` 不需要鉴权。生产环境请通过环境变量改掉默认 Key。

---

## 3. 数据模型要点

| 概念 | 说明 |
| --- | --- |
| 招募批次 `recruitment_batches` | 只追加。每行注明抽样层、家庭标识、个人标识、入样概率、调查期间。入样概率为 `null` 时**不能**形成正式总体估计。 |
| 目标批次 `target_batches` | 年龄 / 地区 / 性别三个维度的已知边际总量；三个维度合计必须都等于 `population_total`，否则拒绝入库。 |
| 节目批次 `program_batches` | 节目单，含时区与 `[start_local, end_local)` 半开时段（`end_local` 允许 `24:30` 之类跨午夜写法）。 |
| 日记提交 `diary_submissions` | 只追加，`submission_seq` 即截止序号。重交必须声明 `replacement_scope`；旧行用 `superseded_by_submission_seq` 标记退出现行计算，**不删除历史**。 |
| 运行 `runs` | 创建时锁定四个输入序号（招募 / 目标 / 节目 / 日记截止）、配置与随机种子，并计算 `input_hash`。阶段化执行，可中断续跑。 |
| 报告 `run_reports` | 发布后不可更新（数据库触发器强制）。迟到日记或更正人口目标只会产生**新运行**。 |

### 运行阶段

`validate_inputs → build_sample → program_exposure → calibrate → bootstrap → estimate → publish_report`

`POST /v1/runs/{id}/execute` 带 `max_steps`，每次推进若干阶段；中断后再次调用同一接口会从上次位置继续，输入与种子不变。

---

## 4. 估计方法（与需求逐条对应）

### 4.1 基本权重与校准

- 基本权重 = `1 / inclusion_probability`。任一人概率未知 → 创建运行即返回 `422 inclusion_probability_unknown`，不做“猜测补齐”。
- 分析员登记年龄 / 地区 / 性别边际总量；入库时三维合计必须相等，否则 `422 target_totals_inconsistent`。
- 校准为**有界 raking（迭代比例拟合）**：
  - 每轮遍历三个维度调整权重 → **每轮后限幅** → 再检查全部边际；
  - 权重上下界为 `[0.3 × 本人设计权重, 4.0 × 本人设计权重]`（可在 `config.raking` 调整）；
  - 容差 `1e-4`，默认最多 100 轮；
  - 以下情况**保留残差并拒绝发布**，不以任何方式补造样本：
    - `empty_cell`：某边际单元样本为 0；
    - `bounds_unreachable`：限幅使目标不可达；
    - `non_convergence`：迭代结束仍未达容差。
- 允许分析员显式合并抽样层（`config.raking.stratum_merges`）或用新配置另跑一次；失败的运行整行保留，可查 `/calibration`。

### 4.2 日记重建

- 原始起止、台站、来源序号与修订号全部保存；统一转换为 `Asia/Shanghai` 后再按半开区间 `[start, end)` 求交。
- 日记日 = 当地 `00:00 → 次日 00:00`。跨午夜记录（如 `23:30 → 00:20`）被拆成两天的 `30` / `20` 分钟，合计不变。
- 同人同台重叠只算一次（区间并集）；不同台重叠生成 `run_conflicts`，状态 `pending`，**不按最后上传选择**。
  未解决冲突所涉及的分钟计为 `unknown`，并且该人在样本构建阶段被排除（`unresolved_conflict`），绝不折算为“没听”。
- 重交：必须先声明替换范围。替换范围跨调查日时，起止必须落在调查日边界上，否则 `422 diary_partial_day_replacement`。
  只替换范围内的时间；跨日替换不会把未涉及的那一天抹掉。

### 4.3 每日时段分类

对每个调查日，把声明窗口精确划分为四个互斥且合计等于窗口长度的部分：

| 分类 | 含义 |
| --- | --- |
| `listening` | 已填且有收听 |
| `no_listening` | 已填但确认没收听 |
| `unknown` | 已填但台站冲突，无法确认 |
| `uncovered` | 未填（漏访） |

`missing = unknown + uncovered`。**空白一律不当作没听广播**：漏填计入 `uncovered`，缺失日记的人整人被排除，其分钟数不被填 0。

### 4.4 分析样本

采用**预先声明**的完整回收条件（默认 `full_coverage`：每个调查日 `missing ≤ 0`），与节目表现无关，
因此在看到节目数字之前就已确定。可改为 `min_complete_days` 并给出最少完整天数。

### 4.5 估计量

- 节目覆盖人数：收听该节目达到 `reach_minutes`（默认 5 分钟）阈值的**独立个人权重求和**。
- 平均时长同时给出两种分母：
  - `mean_minutes_per_person` = 加权分钟 / 全样本权重；
  - `mean_minutes_per_listener` = 加权分钟 / 听众权重。
- 并列未加权样本数 `unweighted_n`、听众样本数 `listener_unweighted_n`、权重范围 `weight_min`/`weight_max`、
  有效样本量 `effective_sample_size`（Kish）。
- 另按年龄 / 地区 / 性别给出分组估计，并标注 `reliability`。

### 4.6 误差估计

- 抽样层内**整户重采样**：在每个有效抽样层内对家庭做有放回抽样，家庭被抽中多次即为多次独立副本，
  **不因标识相同去重**。
- 每个重复样本**重新执行权重校准**；校准失败的副本单独记录，不替换、不隐藏。
- 固定种子（默认 `20260901`），结果可复现。
- 95% 百分位区间。若校准失败副本比例超过 `bootstrap.max_calibration_failure_rate`（默认 10%），
  **停止输出区间**，写入 `interval_status = suppressed_calibration_failure_rate`，并在 `/bootstrap` 列出失败副本与原因。
- 同一节目内的分组差值可给区间（`/programs/{key}/differences`）。

### 4.7 覆盖不足

- 覆盖判定使用**合格日记样本的未加权占比**与目标人口占比之比（`representation_ratio`），
  低于 `thresholds.under_representation_ratio`（默认 0.70）或样本数低于 `min_cell_persons`（默认 5）即标为
  `undercovered`；样本为 0 标为 `no_sample`。
- 不用校准后权重判定：raking 会强行把加权合计对齐边际，用加权份额检查永远“看起来完整”，
  反而掩盖“某群体几乎没交日记”这一事实。
- 报告与 `/coverage-gaps` 都给出覆盖不足的组；同时说明**不能据此比较两档节目**。

### 4.8 跨节目比较

服务**明确拒绝**跨节目表现比较：

- 运行配置 `allow_program_comparison=true` → 创建运行返回 `422 cross_program_comparison_not_supported`；
- `GET /v1/runs/{id}/compare?program_a=&program_b=` 返回 `supported: false`、`difference_estimate: null`，并解释原因；
- 报告中固定包含 `comparison.supported = false` 的理由。

理由：每档节目的估计建立在不同的、覆盖不完整的子总体上，跨节目差值会把覆盖差异误读成节目表现差异。

### 4.9 对外受众表格的披露风险检查

研究主管发布地区、年龄与节目交叉表前，系统执行基于**未加权家庭数阈值 + 二次抑制**的披露控制
（**不声称满足差分隐私**，也不对数字加随机噪声）：

- **主抑制**：未加权家庭数低于阈值（默认 `< 3`）的单元格必须隐藏。只打星号不够——行合计减去其余格
  就能算回它；两张分组略有差别的表也可能联立还原。
- **二次抑制**：再选择额外单元格或合计隐藏，使每个被保护格在**全部已公开约束**下至少存在规定宽度
  （默认 `≥ 3`）的可行整数计数范围。
- **共同检查**：待发布表与**同一数据快照上已公开的表**（含已撤回的表——撤回不假定外部已忘记）一起
  进入检查。底层单元是**互斥的家庭特征组合（原子）**，各表通过成员关系形成整数计数约束；重叠听众
  （同一家庭看两个节目）按成员关系计入，**不能硬当互斥行相加**——合计是并集计数，不是各格之和。
- **精确验证**：对小表**枚举可行整数解**验证上下界（系统按共享变量分解为独立组件后逐一枚举）。
  超出计算预算（默认 50 万节点）就**不放行**，绝不用启发式挑几格后宣布安全；启发式只用于"选哪格
  做二次抑制"，安全结论永远来自精确枚举。
- **加权人数与百分比不旁路**：被隐藏格的加权人数、百分比一并隐藏；百分比只在分子分母都已公开时
  才发布；无法纳入约束系统的衍生列（人均分钟、听众比、个人权重等）**一概不公开**。
- **主管确认**：计划给出抑制数量与可读性损失，主管（admin key）比较后确认；导出严格采用批准单元格，
  **响应标为隐藏但导出仍含原值视为失败**（导出前有硬性检查，公开文件不得含隐藏原值或个人标识）。
- **并发与修订**：并发申请两张表时以**公开表目录修订**共同检查，后一张不能绕过先发布表；样本或分组
  变更需新审（请求与其 `run_id` + 规格哈希绑定，不允许改规格）；撤回已公开表不假定外部已忘记，
  其单元格在后续检查中仍作为约束。

流程：`POST /v1/disclosure/requests`（建申请并立即检查）→ 主管 `POST .../approve` →
`POST .../export`（登记目录并导出）。被阻断的申请保存**阻断依据**（违反格、可行范围、目录证书），
被批准的计划保存**最终可行范围**。演示见 `demo/run_disclosure_demo.py`，产物在 `demo/out/disclosure/`。

---

## 5. 验证方式（以下均为实际执行过的验证）

### 5.1 单元 + 集成测试

```bash
export AUDIENCE_TEST_DATABASE_URL=postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev
.venv/bin/python -m pytest tests/ -q
```

实测结果：**77 passed**（`tests/test_core.py` 18 项纯算法测试；`tests/test_api.py` 35 项接口集成测试；
`tests/test_sdc.py` 12 项披露控制引擎测试；`tests/test_disclosure_api.py` 12 项披露控制接口测试，
均使用独立随机 schema，结束后自动清理）。

覆盖的断言包括：

- 半开区间求交：端点相接贡献 0 分钟；
- 跨午夜 `23:30→00:20` 拆为 30 / 20；
- 已填无收听 = `no_listening`，未填 = `uncovered`，两者绝不混淆；
- 同台重叠只算一次，跨台重叠记为 `unknown`；
- 重交只退役范围内旧行、历史仍可查，跨日部分替换被拒绝；
- raking 收敛达标 / 空单元 / 限幅不可达 / 不收敛四种结局都保留残差；
- 报告不可变（重新执行不再改写、数据库 UPDATE 触发异常）；
- 迟到日记与更正目标各自生成新运行，旧报告哈希不变；
- 未知入样概率、跨节目比较、空单元、限幅失败均按预期拒绝或标记；
- **按日记份数直接平均会高估青年节目**：实测 `youth_drive` 未加权均值 80.00 分钟，校准加权后 46.70 分钟（1.71 倍）；
  偏老节目方向相反（`midnight_news` 5.23 → 7.22），说明偏差并非常数。
- **披露控制**：单表相减（`12−0−7−4=1`）被二次抑制闭合；跨表联立（`4+3=7`）被目录证书阻断；
  重叠听众并集（`41 < 23+20`）不等于相加；计算预算超限即阻断；并发两张表按目录修订串行化；
  撤回表仍约束后续检查；导出无隐藏原值、无个人标识。

### 5.2 完整演示脚本

服务启动并 seed 后：

```bash
.venv/bin/python demo/run_demo.py --base-url http://127.0.0.1:8000
.venv/bin/python demo/run_disclosure_demo.py --base-url http://127.0.0.1:8000
```

实测输出依次覆盖：

1. 已发布运行的校准状态、bootstrap 成功/失败数、区间是否可用；
2. 三档节目：加权覆盖人数、两种分母的平均时长、未加权样本数、权重范围、有效样本量、覆盖区间；
3. 覆盖不足群体与 `comparison.supported=false`；
4. 单个节目数字的血缘：样本行数、权重调整方法、排除原因汇总；
5. `S404` 已填但零收听（`no_listening=1440`、`uncovered=0`）对比 `S601` 漏填一小时（`uncovered=60`）；
6. 完全没有日记的人被排除（`treat_missing_as_zero_listening=false`）；
7. 四人家庭 `H04` 在 bootstrap 中只算 1 户（`households` 小于人数，`duplicate_identifier_copies_deduplicated=false`）；
8. `S301` 跨午夜 30 / 20 拆分；
9. `S501` 跨台冲突保持 `pending`；
10. 重交前后 `S701` 当前分钟数变化，且退役行数 = 2；
11. 空单元目标 → `rejected` + `empty_cells`，报告 404；
12. 限幅不可达目标 → `rejected` + `unreachable_cells` + 残差；
13. 空样本抽样层 `rural-remote` → `strata_without_eligible_sample` 列出，无任何合成样本；
14. 未知入样概率 → `422` 且列出具体人；
15. 迟到日记 → 新运行发布，旧报告哈希完全不变。

`run_disclosure_demo.py` 依次覆盖：

1. 单表相减攻击（`12−0−7−4=1`）与系统的主抑制+二次抑制（`25-44` 与列合计隐藏，可行范围 `[0,∞)`）；
2. 并发两张表按目录修订串行化（第二张 409 → 重检 → 批准 → 导出）；
3. 跨表联立（`4+3=7`）→ 目录证书阻断（`protection_width_unreachable`，`pinned_by_catalog`）；
4. 重叠听众并集（`41 < 23+20`）不等于相加；
5. 计算预算超限（`enumeration_budget_exceeded`，1001 节点）→ 阻断；
6. 撤回表仍约束（`suppressed_catalog`）；
7. 导出清洁检查（无隐藏原值、无个人标识），产物写入 `demo/out/disclosure/`。

### 5.3 手工最小验证

```bash
KEY='X-API-Key: dev-admin-key'
curl -s localhost:8000/health
# 先取当前可用日记截止序号，作为运行输入之一
CUT=$(curl -s -H "$KEY" localhost:8000/v1/diary/cutoff | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['available_max_submission_seq'])")
# 建运行：锁定招募 / 目标 / 节目批次序号与日记截止序号
RID=$(curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST localhost:8000/v1/runs \
  -d "{\"note\":\"manual\",\"inputs\":{\"recruitment_batch_seq\":1,\"target_batch_seq\":1,\"program_batch_seq\":1,\"diary_seq_cutoff\":$CUT}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['run']['run_id'])")
# 执行并取报告
curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST \
  localhost:8000/v1/runs/$RID/execute -d '{"max_steps":20}'
curl -s -H "$KEY" localhost:8000/v1/runs/$RID/report
```

已实际执行上式（本地端口 8000 换成当时端口）：运行状态 `published`，报告含三档节目，
`/docs` 返回 200，`/openapi.json` 为 OpenAPI `3.1.0`，共 35 条路径。

### 5.4 关于 Docker 的说明

当前执行环境**未安装 Docker**，因此 `docker compose up` 未在本机实际运行。
已完成的静态校验：`docker-compose.yml` 可被 YAML 解析且包含 `db/api/demo` 三个服务与 db 健康检查；
`docker/entrypoint.sh` 通过 `sh -n` 语法检查；`Dockerfile` 的依赖列表与本地实测通过的
`requirements.txt` 完全一致。**Docker 路径未经实际执行验证**，请以第 1.2 节本地流程为准。

---

## 6. 接口一览

OpenAPI 3.1.0 共 **35 条路径**（同路径的 GET / POST 在表内合并为一行）。完整可交互文档见 `/docs`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/v1/meta/config` | 维度、默认配置、策略说明 |
| POST/GET | `/v1/recruitment/batches` | 招募批次（只追加，支持 `batch_ref` 幂等） |
| POST/GET | `/v1/targets/batches` | 人口目标边际（三维合计必须相等） |
| POST/GET | `/v1/programs/batches`、`/v1/programs/batches/{seq}` | 节目单 |
| POST/GET | `/v1/diary/submissions`、`/v1/diary/submissions/{seq}` | 日记提交与历史 |
| GET | `/v1/diary/cutoff` | 当前可用最大提交序号 |
| POST/GET | `/v1/runs`、`/v1/runs/{id}` | 创建 / 查看运行（锁定输入） |
| POST | `/v1/runs/{id}/execute` | 分步执行或续跑 |
| GET | `/v1/runs/{id}/status` | 阶段、校准、bootstrap、区间状态 |
| GET | `/v1/runs/{id}/report` | 已发布报告（不可变） |
| GET | `/v1/runs/{id}/programs/{key}` | **单个节目数字的血缘追溯** |
| GET | `/v1/runs/{id}/programs/{key}/sample` | 支撑该数字的样本行 |
| GET | `/v1/runs/{id}/programs/{key}/differences` | 同节目内分组差值 + 区间 |
| GET | `/v1/runs/{id}/compare` | 跨节目比较：**明确拒绝**并说明原因 |
| GET | `/v1/runs/{id}/calibration` | 边际目标、残差、轮次、限幅人数 |
| GET | `/v1/runs/{id}/bootstrap` | 副本成功/失败明细 |
| GET | `/v1/runs/{id}/exclusions` | 排除原因分组与明细 |
| GET | `/v1/runs/{id}/diary-usage` | 每条日记行的处置（使用 / 退役 / 冲突） |
| GET | `/v1/runs/{id}/conflicts` | 跨台冲突（默认 `pending`） |
| POST | `/v1/conflicts/resolutions` | 分析员确认冲突（只影响**新运行**） |
| GET | `/v1/runs/{id}/coverage-gaps` | 覆盖不足群体 |
| GET | `/v1/runs/{id}/days`、`/v1/runs/{id}/persons` | 每日分类明细、人员权重与排除 |
| POST/GET | `/v1/disclosure/requests`、`/v1/disclosure/requests/{id}` | 披露检查申请（建即检查）/ 查看 |
| POST | `/v1/disclosure/requests/{id}/recheck` | 按当前目录重检（规格不变） |
| POST | `/v1/disclosure/requests/{id}/approve`、`/reject` | 主管批准 / 拒绝（仅 admin key） |
| POST/GET | `/v1/disclosure/requests/{id}/export` | 导出（严格采用批准单元格）/ 取回 |
| GET | `/v1/disclosure/catalog` | 公开表目录（含已撤回） |
| POST | `/v1/disclosure/catalog/{id}/withdraw` | 撤回公开表（仍约束后续检查） |

### 从一个节目数字追溯回去

```text
GET /v1/runs/{id}/programs/youth_drive
  ├─ number.estimates          加权覆盖人数 / 两种平均时长 / 有效样本量
  ├─ number.intervals          95% 家庭 bootstrap 区间（含分组区间）
  ├─ trace.inputs              四个输入序号（招募 / 目标 / 节目 / 日记截止）
  ├─ trace.stages              各阶段载荷：样本量、排除计数、分层诊断、校准摘要
  ├─ trace.sample_rows         实际参与该节目计算的样本行数
  ├─ trace.weight_adjustment   → /calibration（每单元目标、合计、残差、轮次、限幅）
  ├─ trace.interval_decomposition → /bootstrap（成功 / 失败副本）
  ├─ trace.exclusions          每类排除原因与示例
  ├─ trace.conflicts           → /conflicts
  └─ trace.diary_usage         → /diary-usage
```

---

## 7. 目录结构

```
app/
  main.py            FastAPI 路由、鉴权、错误映射
  config.py          环境变量配置
  db.py              连接池 + schema 引导
  schema.sql         全部表、索引、报告不可变触发器
  models.py          Pydantic 请求模型与校验
  ingest.py          招募 / 目标 / 节目 / 日记的只追加入库
  timeutil.py        半开区间运算、时区、调查日与节目时段
  analysis.py        样本构建、日记重建、冲突裁决、节目暴露
  runs.py            运行编排：校验→样本→暴露→校准→bootstrap→估计→发布
  report.py          报告组装（覆盖不足、缺口、限制）
  lineage.py         单个数字的血缘追溯
  differences.py     组内差值区间；跨节目比较的明确拒绝
  analytics/
    diary.py         日记重建与四分类日核算
    raking.py        有界 raking 与失败状态
    estimate.py      估计量、家庭分层 bootstrap、百分位区间
    sdc.py           披露控制引擎：原子、精确整数枚举、二次抑制
  disclosure.py      发布申请 / 批准 / 导出 / 目录修订 / 撤回
demo/
  fixtures.py        虚构家庭 / 个人 / 节目 / 目标 / 日记（含全部边界场景）
  run_demo.py        端到端演示脚本
  run_disclosure_demo.py  披露控制演示（攻击样例、阻断依据、可行范围、清洁导出）
  out/disclosure/    演示产物（计划、阻断依据、公开导出）
scripts/seed.py      幂等种子写入
tests/               77 项单元 + 集成测试
docker/              entrypoint
Dockerfile, docker-compose.yml, requirements.txt, pytest.ini
```

---

## 8. 已知限制

- 数据全部虚构，不接入真实人口库或收听设备。
- bootstrap 副本数与 `reach_minutes` 阈值可配置；区间宽度随副本数与样本量变化。
- 日记日固定为当地日历日（`00:00→24:00`）；若改用广播日，需要新的 `diary_window` 约定并重跑。
- 分析员确认冲突时点会影响后续运行结果，但**不会**改写已发布报告。
- 披露控制只覆盖**同一数据快照**的公开表；不同快照（不同运行）之间不做联立检查。
  精确枚举受计算预算限制，过大的联合系统会被阻断而非近似放行。
- Docker 配置未在本次环境实际运行（该环境没有 Docker），见 5.4。
