# Stock Strategy Lab 场景使用说明

本文档用于说明 Stock Strategy Lab 在真实使用时应该如何运行、每一层智能体会做什么、内部流程是什么、会生成哪些产物、用户应该检查哪些文件，以及如果想扩展策略库应该修改哪些 skill 文件。

项目采用三层智能体结构：

```text
信号层智能体 SignalAgent
-> 预算层智能体 BudgetAgent
-> 组合层智能体 PortfolioAgent
```

这三层既可以单独使用，也可以串联使用。

- **只研究某个资产如何交易**：只用信号层。
- **只研究一组资产如何配置**：只用预算层。
- **希望融合单资产择时和资产池配置**：用完整三层流程。

## 0. 使用前准备

### 0.1 安装

```powershell
cd D:\path\to\stock_strategy_lab
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e ".[quant,agents,data,dev]"
```

### 0.2 配置环境变量

复制环境变量模板：

```powershell
copy .env.example .env
```

常见配置：

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro

MOONSHOT_API_KEY=
MOONSHOT_BASE_URL=https://api.moonshot.cn/v1
MOONSHOT_MODEL=kimi-k2.6

QMT_USERDATA=
QMT_ACCOUNT_ID=
```

### 0.3 初始化目录

```powershell
python -m strategy_lab.cli init
```

### 0.4 查看配置

```powershell
python -m strategy_lab.cli config
```

### 0.5 配置文件说明

项目里有两类配置：一类是本机私密配置，放在 `.env`；另一类是项目默认配置，放在 `configs/` 目录。

`.env` 是本机运行时配置，通常由 `.env.example` 复制而来。它不应该提交到 GitHub。这里主要填写大模型 API Key、模型地址、MiniQMT 本地路径和账号等内容。

常见可改项：

```text
DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
MOONSHOT_API_KEY / MOONSHOT_BASE_URL / MOONSHOT_MODEL
QMT_USERDATA / QMT_ACCOUNT_ID
SIGNAL_AGENT_* / BUDGET_AGENT_* / PORTFOLIO_AGENT_*
CRITIC_AGENT_* / BUDGET_CRITIC_AGENT_* / PORTFOLIO_CRITIC_AGENT_*
```

如果只想统一使用一个模型，优先填写通用模型配置即可。如果想让不同智能体使用不同模型，可以单独填写对应智能体的变量。例如：信号层用 DeepSeek，复盘智能体用 Kimi，预算层继续用 DeepSeek。

`configs/agent.yaml` 是智能体配置。它定义各个智能体默认使用哪个 provider、base_url、model、thinking、reasoning_effort、工具超时时间、上下文压缩阈值等。

常见可改项：

```text
agents.strategy_agent.max_strategy_iterations
agents.budget_agent.tool_timeout_seconds
agents.portfolio_agent.tool_timeout_seconds
agents.context_windows.deepseek.effective_max_input_tokens
agents.context_windows.kimi.effective_max_input_tokens
```

说明：

- `max_strategy_iterations` 控制信号层默认探索轮数，终端运行时也可以通过命令参数或自然语言要求覆盖。
- `tool_timeout_seconds` 控制工具执行的最长等待时间。量化回测和多智能体任务可能很久，通常不建议设太短。
- `effective_max_input_tokens` 是 DeepAgents 上下文压缩的参考阈值，不是模型厂商真实最大上下文。模型变更后可以按模型上下文能力保守调整。

`configs/backtest.yaml` 是通用回测默认配置，主要用于信号层或通用回测服务。

常见可改项：

```text
initial_cash      初始资金
commission        手续费率
slippage_perc     滑点
allow_short       是否允许做空
benchmark_symbol  默认基准
generate_report   默认是否生成报告
```

注意：单次任务也可以在命令行或 run_state 里覆盖这些默认值。也就是说，配置文件给的是默认值，具体某次实验可以单独改。

`configs/budget.yaml` 是预算层默认配置。它包含预算层默认模式、默认资产池名称、预算层最大探索轮数、基准类型、预算层回测设置等。

常见可改项：

```text
max_strategy_iterations
benchmark
benchmark_options
backtest.initial_cash
backtest.commission
backtest.slippage_perc
backtest.execution_price
```

其中 `benchmark_options` 是预算层可选基准，例如等权定期再平衡、等权买入持有、简单动量 TopK、现金等。预算层最终使用哪个基准，可以由智能体根据任务要求选择，也可以由用户在对话中指定。

`configs/data_sources.yaml` 是数据源开关配置。当前主要包括：

```text
data_sources.miniqmt.enabled
data_sources.akshare.enabled
```

通常优先使用 MiniQMT，其次使用 AKShare。若本机没有打开 QMT 客户端，MiniQMT 相关调用可能失败，智能体会尝试根据任务情况切换数据源或提示用户。

`configs/qmt.yaml` 是 MiniQMT / xtquant 相关配置。它从 `.env` 读取：

```text
QMT_USERDATA
QMT_ACCOUNT_ID
QMT_ACCOUNT_TYPE
```

如果更换券商客户端目录、账号或账号类型，就改 `.env`，一般不直接改这个 yaml。

`configs/app.yaml` 是项目运行目录和日志配置。

常见可改项：

```text
project.artifacts_dir
project.default_timezone
logging.level
```

除非你想把所有运行产物放到别的目录，否则一般不需要改。

`configs/safety.yaml` 是策略脚本安全检查配置。它定义策略脚本允许写入的位置、禁止导入的模块、禁止调用的函数等。

一般不建议随便放宽这些限制。LLM 会自动写策略脚本，安全配置可以减少策略脚本误读写系统文件、执行网络请求或调用危险函数的风险。如果你明确要扩展策略脚本能力，再有针对性地调整。

配置修改建议：

- 改模型、API Key、本地 QMT 路径：优先改 `.env`。
- 改默认探索轮数、上下文压缩阈值、工具超时：改 `configs/agent.yaml`。
- 改手续费、滑点、初始资金、基准：改 `configs/backtest.yaml` 或 `configs/budget.yaml`。
- 改策略搜索范围、策略类型、智能体工作方式：不要优先改配置文件，应改对应 skill 的 `SKILL.md` 或 references 文档。
- 改某一次具体实验：优先在终端对话里告诉智能体，让它写入本次任务目录的状态文件和策略文件。

## 1. 总体运行逻辑

Stock Strategy Lab 的每一层都不是简单跑一次脚本，而是一个智能体闭环：

```text
创建任务目录
-> 获取或整理数据
-> 生成画像
-> 编写策略
-> 回测评估
-> 复盘归因
-> 修改策略
-> 再次评估
-> 最终选择
```

每一层都有自己的运行目录和状态文件。

```text
artifacts/
├── signal_runs/       # 信号层任务
├── budget_runs/       # 预算层任务
└── portfolio_runs/    # 组合层任务
```

三层之间通过“最终产物”衔接：

```text
信号层 final 策略
-> 预算层读取多个信号层结果，形成资产池预算策略
-> 组合层读取预算层最终结果和各资产信号层结果，形成最终组合策略
```

## 2. 交互式终端

三层主智能体都支持交互式终端。

```powershell
python -m strategy_lab.cli signal chat
python -m strategy_lab.cli budget chat
python -m strategy_lab.cli portfolio chat
```

进入后可以直接用自然语言发任务，也可以用 `/` 命令管理会话。

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看当前交互终端支持的命令 |
| `/exit` | 退出当前交互终端 |
| `/quit` | `/exit` 的别名 |
| `/q` | `/exit` 的短别名 |
| `/new` | 创建新的持久化会话 |
| `/reset` | 创建新会话，作为 `/new` 的别名 |
| `/sessions` | 列出历史对话会话 |
| `/resume THREAD_ID` | 恢复指定历史会话 |
| `/clear` | 清屏 |
| `/runs` | 列出最近的该层 run |
| `/load RUN_ID` | 载入某个 run，使后续对话围绕该任务继续 |
| `/status` | 查看当前已加载 run 的状态摘要 |
| `/memory` | 查看当前 run 的智能体记忆文件，如果存在 |
| `/pause` | 表示希望暂停当前轮次边界；正在执行的长工具通常不会被强行中断 |

典型交互：

```text
> /runs
> /load signal_512880_SH_20260512_000656
> /status
> /memory
> 请继续优化上一轮策略，这次更重视最大回撤和资金利用率。
```

## 3. 数据智能体 DataAgent

### 3.1 它做什么

DataAgent 是三层之外的支撑智能体，负责数据相关任务：

- 获取 MiniQMT / xtquant 数据。
- 获取 AKShare 公开数据。
- 根据用户要求保存为标准文件。
- 生成数据摘要和 manifest。
- 在被其他智能体调用时，把结果路径写回对应状态文件。

### 3.2 谁会调用它

- SignalAgent 会调用它获取单资产 OHLCV 数据。
- BudgetAgent 在缺失多资产数据时可以调用它补数据。
- PortfolioAgent 在组合层需要额外数据时也可以调用它。

### 3.3 用户怎么直接用

```powershell
python -m strategy_lab.cli data agent "请使用 MiniQMT 获取 512880.SH 的日线 OHLCV 数据，时间范围为 2020-01-01 到 2025-12-31，保存为 parquet。"
```

更常见的是不要直接调用 DataAgent，而是在 SignalAgent / BudgetAgent / PortfolioAgent 的自然语言任务里说清楚需要什么数据，由主智能体调用它。

### 3.4 相关 skill

| Skill | 文件 |
| --- | --- |
| MiniQMT 数据能力 | `src/strategy_lab/skills/data_agent/miniqmt/SKILL.md` |
| MiniQMT 参考脚本 | `src/strategy_lab/skills/data_agent/miniqmt/reference_scripts/` |
| xtquant 文档 | `src/strategy_lab/skills/data_agent/miniqmt/references/xtquant_docs.md` |
| AKShare 数据能力 | `src/strategy_lab/skills/data_agent/akshare/SKILL.md` |
| AKShare 文档 | `src/strategy_lab/skills/data_agent/akshare/references/akshare_docs/` |

## 4. 场景一：只用信号层，为单个资产生成交易策略

### 4.1 适用场景

你只想研究某一个资产，例如：

- 某只 ETF
- 某个指数
- 某只股票

目标是让智能体自行寻找这个资产的交易策略。

### 4.2 启动方式

```powershell
python -m strategy_lab.cli signal chat
```

### 4.3 示例对话

```text
请为 512880.SH 生成一个单资产交易策略。
数据范围使用 2015-01-01 到 2025-12-31 的日线数据。
目标优先提高夏普比率，同时控制最大回撤。
请自主获取数据、生成市场画像、编写策略、回测、复盘并迭代，最后选择一个最终策略。
```

如果已经有一个 run 想继续：

```text
> /runs
> /load signal_512880_SH_20260512_000656
> 请继续优化当前策略，这次重点解决资金利用率偏低的问题。
```

### 4.4 SignalAgent 会做什么

SignalAgent 的内部流程大致是：

```text
1. 创建 signal run 目录和状态文件
2. 调用 DataAgent 获取单资产数据
3. 生成数据切分
4. 生成市场画像
5. 阅读策略编写 skill 和策略库
6. 编写多个候选策略
7. 调用评估服务进行回测和参数搜索
8. 调用 CriticAgent 复盘
9. 根据复盘继续修改策略
10. 多轮迭代后选择最终策略
```

### 4.5 信号层目录结构

典型目录：

```text
artifacts/signal_runs/signal_512880_SH_20260512_000656/
├── run_state.json
├── data/
├── market_profile/
├── strategies/
├── attempts/
├── reports/
├── logs/
└── final/
```

### 4.6 用户重点检查什么

#### 任务状态

```text
run_state.json
```

这是信号层任务总状态。你可以看：

- 当前 run_id
- 目标资产
- 数据路径
- 已生成的 attempts
- 最终选择
- 各类报告路径

#### 市场画像

```text
market_profile/
```

重点看：

- 市场阶段划分
- 趋势阶段
- 波动和回撤
- 走势图

这些文件帮助判断智能体为什么选择某类策略。

#### 每轮策略 attempt

```text
attempts/{attempt_id}/
```

每个 attempt 通常包含：

```text
strategy/        # 策略脚本、策略说明、参数空间、元数据
backtests/       # full/train/validation/walk-forward 回测结果
optimization/    # 参数搜索结果、候选结果、摘要
analysis/        # 阶段归因
review/          # 复盘结果
logs/            # 日志
```

重点看：

- 策略说明
- 参数空间
- full 回测指标
- validation / walk-forward 表现
- 阶段归因
- 复盘报告

#### 智能体记忆

```text
reports/signal_agent_memory.md
```

这里记录智能体尝试过的策略、阶段判断、失败原因、下一步优化方向。

#### 最终策略

```text
final/
```

最终选择的策略会被登记到这里或在状态文件里记录路径。

### 4.7 信号层扩展策略库

如果你想让信号层探索更多策略，不需要改主程序，优先改 skill。

| 想扩展什么 | 修改文件 |
| --- | --- |
| 信号层工作流程、策略编写要求 | `src/strategy_lab/skills/signal_agent/strategy-authoring/SKILL.md` |
| 信号层策略库、Alpha 类型、过滤器、出场、仓位映射 | `src/strategy_lab/skills/signal_agent/strategy-authoring/references/signal_strategy_library.md` |
| 示例策略脚本 | `src/strategy_lab/skills/signal_agent/strategy-authoring/references/example_strategy.py` |
| 示例参数空间 | `src/strategy_lab/skills/signal_agent/strategy-authoring/references/example_param_space.json` |

可以扩展的方向包括：

- 机器学习因子
- 缠论结构
- 多因子模型
- 统计套利
- 事件驱动
- 新闻情绪
- 强化学习
- 高频或分钟级策略

## 5. 场景二：只用预算层，为一组资产生成配置策略

### 5.1 适用场景

你已经有一组资产，想让智能体寻找组合配置策略，例如：

- 行业 ETF 轮动
- 宽基指数轮动
- 股债商品配置
- 主题基金组合

预算层可以两种方式使用：

1. 读取已经完成的信号层结果。
2. 只基于用户指定资产池和行情数据做预算层配置。

当前项目更推荐第一种：先完成信号层，再进入预算层。

### 5.2 启动方式

```powershell
python -m strategy_lab.cli budget chat
```

### 5.3 示例对话：读取信号层结果

```text
请基于以下信号层结果目录创建预算层任务：
artifacts/signal_runs

请读取其中已经完成最终选择的信号层 run，汇总资产池，生成多资产行情面板，训练预算层轮动策略。
我的目标是控制最大回撤，同时不要让资金长期闲置。
```

如果信号层结果分散在多个目录：

```text
请基于以下多个信号层结果目录创建预算层任务：
artifacts/signal_runs/batch_a
artifacts/signal_runs/batch_b

只纳入已经有最终策略的资产。
```

### 5.4 BudgetAgent 会做什么

预算层内部流程：

```text
1. 创建 budget run 目录和状态文件
2. 扫描信号层 run_state
3. 复制每个资产的最终信号策略小文件
4. 汇总资产池
5. 生成多资产行情面板
6. 生成数据切分
7. 生成预算层资产池画像
8. 根据画像和用户偏好选择策略簇
9. 编写预算策略
10. 评估预算策略
11. 调用 BudgetCriticAgent 复盘
12. 多轮迭代后选择最终预算策略
```

### 5.5 预算层目录结构

典型目录：

```text
artifacts/budget_runs/budget_sector_pool_20260512_120000/
├── budget_run_state.json
├── signal_artifacts/
├── data/
├── profile/
├── policies/
├── reports/
├── logs/
└── final/
```

### 5.6 预算层如何和信号层衔接

预算层不是凭空知道信号层任务的。

你需要在对话里告诉 BudgetAgent 信号层结果在哪里，例如：

```text
artifacts/signal_runs
```

或者某几个具体 run：

```text
artifacts/signal_runs/signal_512880_SH_20260512_000656
artifacts/signal_runs/signal_159995_SZ_20260512_030843
```

BudgetAgent 会扫描这些目录下的 `run_state`，找出：

- 资产代码
- 数据范围
- 原始数据路径
- 最终信号策略
- 信号层策略说明
- 信号层智能体记忆

然后复制必要小文件到预算层自己的任务目录。

### 5.7 用户重点检查什么

#### 预算层状态

```text
budget_run_state.json
```

重点看：

- asset_pool
- 数据面板路径
- profile 路径
- 已评估策略
- 最终预算策略

#### 信号层源产物汇总

```text
signal_artifacts/
```

重点看：

- 每个资产是否都被纳入
- 每个资产是否有最终策略
- 是否缺少数据或策略文件

#### 多资产数据

```text
data/
```

重点看：

- 多资产 OHLCV 面板
- 收益率矩阵
- 数据切分 manifest

#### 资产池画像

```text
profile/
```

重点看：

- 资产数量
- 资产元数据
- 收益、波动、回撤
- 相关性
- 市场阶段
- 资产池是否适合轮动、均衡、防御或集中策略

#### 预算策略评估

```text
policies/
```

典型内容包括：

- 预算策略配置
- 参数空间
- 每日预算权重
- 参数搜索结果
- full/train/validation/walk-forward 回测
- 阶段归因
- 复盘结果

#### 最终预算策略

```text
final/
```

这里是后续组合层最常用的入口。

### 5.8 预算层扩展策略库

| 想扩展什么 | 修改文件 |
| --- | --- |
| 预算层工作流程、策略编写要求 | `src/strategy_lab/skills/budget_agent/budget-policy-authoring/SKILL.md` |
| 预算策略库、策略簇、资产准入、打分器、分配引擎、风险覆盖 | `src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/budget_policy_library.md` |
| 示例预算策略配置 | `src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/example_budget_policy_config.json` |
| 示例参数空间 | `src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/example_param_space.json` |

可以扩展：

- 新的动量评分器
- 低波偏好
- 风险平价
- 相关性惩罚
- 最大回撤保护
- 行业/主题分组约束
- 波动目标
- 仓位上限
- 再平衡规则

## 6. 场景三：组合层融合信号层和预算层

### 6.1 适用场景

当你已经完成预算层，并且预算层里已经保留了各资产信号层结果，就可以进入组合层。

组合层目标：

> 把预算层的资产配置能力和信号层的单资产择时能力融合成最终组合交易策略。

预算层更偏：

- 资产选择
- 权重分配
- 风险预算
- 组合轮动

信号层更偏：

- 单资产择时
- 单资产仓位强弱
- 单资产风险控制

组合层要决定两者如何结合。

### 6.2 启动方式

```powershell
python -m strategy_lab.cli portfolio chat
```

### 6.3 示例对话

```text
请基于这个预算层最终结果目录创建组合层任务：
artifacts/budget_runs/budget_sector_pool_20260512_120000

请读取预算层最终策略和各资产信号层最终策略，生成组合层画像和信号策略画像。
然后编写融合策略，探索预算层权重和信号层目标仓位如何结合。
目标是提高收益和夏普，同时避免资金利用率过低。
请持续回测、复盘和优化，最后选择一个最终组合层策略。
```

如果已经有组合层 run：

```text
> /runs
> /load portfolio_sector_pool_20260513_210000
> /status
> 请基于上一轮复盘继续优化融合策略，这次重点解决现金闲置和强信号资产参与不足的问题。
```

### 6.4 PortfolioAgent 会做什么

组合层内部流程：

```text
1. 创建 portfolio run 目录和状态文件
2. 复制预算层最终策略
3. 复制各资产信号层最终策略
4. 准备组合层行情数据
5. 生成组合层数据切分
6. 生成组合层市场画像
7. 生成每个资产的信号策略画像
8. 编写融合策略
9. 执行组合层回测
10. 调用 PortfolioCriticAgent 复盘
11. 根据复盘继续优化融合策略
12. 选择最终组合层策略
```

### 6.5 组合层目录结构

典型目录：

```text
artifacts/portfolio_runs/portfolio_sector_pool_20260513_210000/
├── portfolio_run_state.json
├── source_artifacts/
├── data/
├── profile/
├── versions/
├── reports/
├── logs/
└── final/
```

### 6.6 组合层如何和预算层、信号层衔接

你只需要给 PortfolioAgent 一个预算层最终结果目录。

例如：

```text
artifacts/budget_runs/budget_sector_pool_20260512_120000
```

PortfolioAgent 会从预算层目录中读取：

- 预算层最终策略
- 预算层状态文件
- 预算层记忆报告
- 预算层数据和权重
- 预算层保存的信号层源产物

然后在组合层目录中形成：

```text
source_artifacts/
├── budget/
└── signals/
```

### 6.7 用户重点检查什么

#### 组合层状态

```text
portfolio_run_state.json
```

重点看：

- source_artifacts 是否完整
- data 是否准备完成
- profile 是否生成
- signal_profile 是否生成
- versions 是否有多个融合策略版本
- final_selection 是否完成

#### 源产物快照

```text
source_artifacts/
```

重点看：

- 预算层最终策略是否复制过来
- 每个资产信号层最终策略是否复制过来
- 预算层和信号层记忆文件是否存在

#### 组合层画像

```text
profile/
```

重点看：

- 资产池整体表现
- 预算层权重特征
- 信号层目标仓位特征
- 预算和信号的差异
- 资金利用率
- 相关性和阶段表现

#### 信号策略画像

通常由 `portfolio-signal-profile` 生成。

重点看：

- 每个资产的信号策略类型
- 平均暴露
- 仓位档位
- 防御程度
- 适合的市场状态
- 风险和限制

#### 融合策略版本

```text
versions/{version_id}/
```

典型内容：

```text
融合策略脚本
策略说明
参数空间
元数据
evaluation/
```

重点看：

- 融合逻辑是否合理
- 是否过度保守
- 是否资金利用率过低
- 是否充分利用强信号资产
- 是否比预算层单独策略更好

#### 最终组合策略

```text
final/
```

这里是最终组合层策略的统一入口。后续如果要做完整模拟、提交、实盘前验证，优先读取这里。

### 6.8 组合层扩展策略库

| 想扩展什么 | 修改文件 |
| --- | --- |
| 组合层工作流程、融合策略编写要求 | `src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/SKILL.md` |
| 组合层融合策略库 | `src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/fusion_policy_library.md` |
| 预算直用示例 | `src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_budget_direct_fusion_policy.py` |
| 信号直用示例 | `src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_signal_direct_fusion_policy.py` |
| 混合融合示例 | `src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_simple_mixed_fusion_policy.py` |
| 每日组合智能体提示词示例 | `src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_daily_portfolio_agent_prompt.md` |

组合层可以扩展：

- 预算主导 + 信号修正
- 信号主导 + 预算约束
- 闲置资金再分配
- 强信号资产小幅突破预算
- 弱信号资产降仓
- 市场阶段切换融合公式
- 资金利用率目标
- 换手率约束
- 每日组合审核智能体

## 7. 三层完整全流程示例

假设你要研究一组行业 ETF，完整流程可以这样跑。

### 7.1 第一步：逐个资产跑信号层

启动：

```powershell
python -m strategy_lab.cli signal chat
```

对每个资产分别说：

```text
请为 512880.SH 生成单资产策略，使用 2015-01-01 到 2025-12-31 日线数据，目标优先提高夏普比率并控制最大回撤。
```

完成后得到多个目录：

```text
artifacts/signal_runs/
├── signal_512880_SH_xxx/
├── signal_512800_SH_xxx/
├── signal_159995_SZ_xxx/
└── ...
```

每个目录都应该有最终策略。

检查：

```text
artifacts/signal_runs/{run_id}/run_state.json
artifacts/signal_runs/{run_id}/reports/signal_agent_memory.md
artifacts/signal_runs/{run_id}/final/
```

### 7.2 第二步：预算层读取信号层结果

启动：

```powershell
python -m strategy_lab.cli budget chat
```

输入：

```text
请读取 artifacts/signal_runs 下所有已经完成最终策略选择的信号层任务。
基于这些资产创建预算层任务，训练一个多资产预算策略。
目标是提高夏普比率，控制最大回撤，同时避免资金长期闲置。
```

完成后得到：

```text
artifacts/budget_runs/{budget_run_id}/
```

检查：

```text
budget_run_state.json
signal_artifacts/
data/
profile/
policies/
reports/
final/
```

### 7.3 第三步：组合层读取预算层最终结果

启动：

```powershell
python -m strategy_lab.cli portfolio chat
```

输入：

```text
请基于这个预算层最终结果目录创建组合层任务：
artifacts/budget_runs/{budget_run_id}

请生成组合层画像和信号策略画像，编写融合策略，持续回测和复盘，最后选择最终组合层策略。
```

完成后得到：

```text
artifacts/portfolio_runs/{portfolio_run_id}/
```

检查：

```text
portfolio_run_state.json
source_artifacts/
profile/
versions/
reports/
final/
```

### 7.4 三层最终关系

完整链路是：

```text
signal_runs/{多个单资产 run}
-> budget_runs/{一个资产池预算 run}
-> portfolio_runs/{一个组合层融合 run}
```

每层都可以继续独立恢复会话：

```powershell
python -m strategy_lab.cli signal chat --resume-latest
python -m strategy_lab.cli budget chat --resume-latest
python -m strategy_lab.cli portfolio chat --resume-latest
```

或者在交互终端内：

```text
> /sessions
> /resume THREAD_ID
> /runs
> /load RUN_ID
```

## 8. 各层主智能体和子智能体对应关系

| 层级 | 主智能体 | 子智能体 / 支撑能力 | 说明 |
| --- | --- | --- | --- |
| 数据层 | DataAgent | MiniQMT skill、AKShare skill | 为其他层提供数据 |
| 信号层 | SignalAgent | CriticAgent、DataAgent | 单资产策略探索 |
| 预算层 | BudgetAgent | BudgetCriticAgent、DataAgent | 多资产预算策略探索 |
| 组合层 | PortfolioAgent | PortfolioCriticAgent、DataAgent | 前两层融合策略探索 |

主智能体负责任务推进和决策；复盘智能体负责分析结果和提出建议；确定性 services 负责回测、画像、参数搜索和状态更新。

## 9. 全部 skill 对照表

| Skill | 所属 | 作用 |
| --- | --- | --- |
| `data_agent/akshare` | DataAgent | AKShare 数据获取说明 |
| `data_agent/miniqmt` | DataAgent | MiniQMT / xtquant 数据获取说明 |
| `common/image-review` | 通用 | 图片理解工具说明 |
| `signal_agent/signal-run` | SignalAgent | 创建或读取信号层 run |
| `signal_agent/data-split` | SignalAgent | 信号层数据切分 |
| `signal_agent/market-profile` | SignalAgent | 单资产市场画像 |
| `signal_agent/strategy-authoring` | SignalAgent | 信号策略编写指南 |
| `signal_agent/attempt-evaluation` | SignalAgent | 信号策略一键评估 |
| `critic_agent/single-attempt-review` | CriticAgent | 单个信号策略复盘 |
| `critic_agent/multi-attempt-comparison` | CriticAgent | 多个信号策略比较 |
| `budget_agent/budget-run` | BudgetAgent | 创建预算层 run 并汇总信号层结果 |
| `budget_agent/budget-data-panel` | BudgetAgent | 多资产行情面板 |
| `budget_agent/budget-data-split` | BudgetAgent | 预算层数据切分 |
| `budget_agent/budget-profile` | BudgetAgent | 资产池画像 |
| `budget_agent/budget-policy-authoring` | BudgetAgent | 预算策略编写指南 |
| `budget_agent/budget-policy-evaluation` | BudgetAgent | 预算策略一键评估 |
| `budget_agent/budget-final-selection` | BudgetAgent | 最终预算策略登记 |
| `budget_critic_agent/single-policy-review` | BudgetCriticAgent | 单个预算策略复盘 |
| `budget_critic_agent/multi-policy-comparison` | BudgetCriticAgent | 多个预算策略比较 |
| `portfolio_agent/portfolio-run` | PortfolioAgent | 创建组合层 run 并复制源产物 |
| `portfolio_agent/portfolio-data-split` | PortfolioAgent | 组合层数据切分 |
| `portfolio_agent/portfolio-profile` | PortfolioAgent | 组合层画像 |
| `portfolio_agent/portfolio-signal-profile` | PortfolioAgent | 信号策略画像 |
| `portfolio_agent/portfolio-policy-authoring` | PortfolioAgent | 组合层融合策略编写指南 |
| `portfolio_agent/portfolio-fusion-version` | PortfolioAgent | 初始化融合策略版本 |
| `portfolio_agent/portfolio-evaluation` | PortfolioAgent | 组合层策略评估 |
| `portfolio_agent/portfolio-final-selection` | PortfolioAgent | 最终组合策略登记 |
| `portfolio_critic_agent/portfolio-review` | PortfolioCriticAgent | 组合层复盘 |

## 10. 常见问题

### 10.1 我只想做单资产策略，需要跑预算层和组合层吗？

不需要。只用 SignalAgent 即可。

### 10.2 我只想做资产池轮动，不关心单资产策略，可以只用预算层吗？

可以。预算层可以作为独立资产配置智能体使用。但如果你希望组合层进一步利用每个资产自己的择时信号，建议先跑信号层。

### 10.3 预算层如何知道信号层结果在哪里？

你需要在 BudgetAgent 对话中告诉它信号层结果目录，例如：

```text
artifacts/signal_runs
```

它会扫描其中的 signal run。

### 10.4 组合层如何知道预算层和信号层结果在哪里？

你只需要告诉 PortfolioAgent 预算层最终结果目录。预算层目录里会保存信号层源产物信息，组合层会继续读取和复制。

### 10.5 我想让智能体探索新的策略类型，应该改代码还是改 skill？

优先改 skill。

- 信号层改 `signal_strategy_library.md`
- 预算层改 `budget_policy_library.md`
- 组合层改 `fusion_policy_library.md`

只有当你要增加新的确定性服务、回测引擎或数据处理能力时，才需要改 Python 代码。

### 10.6 artifacts 目录要提交到 GitHub 吗？

不建议。`artifacts/` 是本地运行产物目录，可能包含数据、日志、策略结果和账号相关信息，默认应该被 `.gitignore` 忽略。
