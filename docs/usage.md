# 使用指南

## 安装方式

运行环境和安装步骤见项目首页。Windows 用户在 WSL2 Linux 终端运行。uv 提供锁定依赖的一致环境，pip 安装后可直接调用 `evogate` 和 `evogate-eval` 命令。[安装与启动](../README.md#快速开始)

## 运行模式

```sh
uv run --locked evogate --workspace workspaces/demo
uv run --locked evogate --live --env-file .env --workspace workspaces/business
```

同一工作区固定 demo/live 模式。真实模型配置支持 `EVOGATE_API_KEY` 或 `DEEPSEEK_API_KEY`、`EVOGATE_API_BASE`、`EVOGATE_MODEL`。默认模型为 `deepseek-flash`，查询和优化使用同一服务。`.env` 只在显式传入 `--env-file` 时加载。

## 接入 SQLite

```sh
uv run --locked evogate --live --env-file .env --workspace workspaces/business \
  --database /absolute/path/business.sqlite --db-id business \
  --context /absolute/path/field-notes.txt --skill /absolute/path/SKILL.md
```

`--context` 和 `--skill` 可省略，默认使用内置通用 `sql-analysis` Skill。注册后退出，再运行服务器。界面查询页的连接表单提供相同能力。数据库结构改变后需重新注册数据库。

在 Windows WSL2 中，请使用 Linux 可访问的数据库路径，例如 `/mnt/c/data/business.sqlite`。

## Skill 生命周期

导入的 Skill 需要 YAML frontmatter，含 `name` 和 `description`，正文包含 Markdown 标题与操作指导。支持范围是 SQLite 分析及应用已有工具。

导入文档保存在 `skill_catalog`，同一文档可绑定多个数据库。绑定时创建一个独立分支，将导入文档作为 `v0`。各分支独立进化，历史分支和文件仍保留在工作区。重新绑定建立新的分支，原分支可从界面的分支选择器恢复。版本页可下载完整 Markdown，回退支持初始版本和已接受版本。

```sh
uv run --locked evogate --live --env-file .env --workspace workspaces/business \
  --skill /absolute/path/SKILL.md --db-id business
```

内置销售演示从空 Skill 开始展示生成与发布流程。真实数据库默认绑定通用 `sql-analysis` Skill，也可以导入自己的初始文档。

## 反馈与独立验证案例

查询结果页允许提交已确认的正确 SQL 与业务说明。相同问题及相同确认不会重复计入触发数量。执行通过的查询默认标记 `unverified`，SQL 错误可作为执行失败经验，已确认结果会带来源和评分记录。

为真实用户数据库导入独立验证案例后，候选才能自动发布。案例格式如下。

```json
[
  {"question": "有多少订单？", "reference_sql": "SELECT COUNT(*) FROM orders", "split": "training"},
  {"question": "最大商品金额？", "reference_sql": "SELECT MAX(amount) FROM items", "split": "selection"},
  {"question": "有哪些地区？", "reference_sql": "SELECT DISTINCT region FROM orders", "split": "holdout"}
]
```

```sh
uv run --locked evogate --live --env-file .env --workspace workspaces/business \
  --db-id business --cases /absolute/path/cases.json
```

也可在查询页导入 JSON 文件。导入检查参考 SQL 可执行性、重复问题和跨划分 SQL 模板，整批通过后保存。已进入经验库的题目不能再成为选择或保留案例。选择和保留题仅通过评测入口执行。

## 后台进化

版本页可手动发起一次后台任务。自动模式可通过界面或 `--auto-evolve` 开启，服务器每五秒检查条件。数量触发与同类失败触发均使用去重后的新反馈。

默认配置如下。

| 参数 | 默认值 |
|---|---:|
| `enabled` | false |
| `batch_size` | 50 |
| `recurring_failures` | 3 |
| `cooldown_seconds` | 3600 |
| `max_attempts_per_day` | 4 |
| `max_calls_per_attempt` | 300 |
| `replay_size` | 40 |
| `min_net_improvements` | 1 |

界面提供常用配置，完整参数可通过 `POST /api/evolution/config` 更新。门禁采用选择集逐题配对结果，报告改善题数、退化题数和净改善门槛。收益不足与缺少验证证据保存为 `pending`，净退化或结构不合法保存为 `rejected`。

当前门禁要求全部验证题评分已知。参考 SQL 正常时，模型 SQL 明确超过结果行数或大小限制计为失败。数据库故障等导致评分未知时，整个候选会保留为待定。可在版本的逐题结果中检查原因，确认后再安排后续评测。

回放混合近期案例、历史未解决失败和成功反例。待定或拒绝不会删除经验，也不会将整个失败方向标记为无效。已发布版本通过案例回放后才更新其解决状态。回退或切换分支后重新等待回放验证。

每次后台任务最多生成一个候选，后续手动修订或新增反馈可再次触发。中断和预算失败保留原发布版本与经验，任务页显示失败原因。任务运行时可以继续查询与纠错，切换分支、回退和导入案例需等任务结束。

文档未变化时跳过候选评测。在评测条件相同且已有完整结论时，重复候选复用历史结果。每次尝试都保留记录，原始反馈可在后续新经验到来后继续使用。

## 快照评测

效果对比页固定当前版本、初始版本、模型和保留案例，比较初始 Skill、进化 Skill、无 Skill 三组。相同 Skill 共用观测，每题缓存可用于中断恢复。

每个数据库的该轮保留集报告只生成一次，重复点击返回原报告。应用仍可继续接收新反馈。后续正式效果评测使用新的未暴露保留集及独立评测工作区。

## 本地文件

`state.json` 保存注册表、分支、经验、任务与报告。`skills/<分支哈希>/<版本>/<内容哈希>/SKILL.md` 保存不可变文档。`frozen-*.json` 和 `evaluations/` 保存评测快照及逐题缓存。

真实调用向配置的模型服务发送问题、schema、业务说明、工具结果、Skill，以及优化所需的反馈与轨迹。工作区记录保存在本机。

模型查询和进化都会产生 API 调用。工作区使用 JSON 与本地文件保存状态，适合单用户本地使用，同一目录由一个应用进程持有。数据库文件的结构改变后，使用新的工作区重新注册。
