---
name: miniqmt
description: "使用本机 MiniQMT / xtquant 获取和处理行情、财务、板块、交易日历、tick、Level2、账户相关数据。适用于从 QMT、MiniQMT、xtdata、xtquant 获取 A 股股票、指数、ETF、可转债、期货等数据的任务。"
license: Proprietary project skill
---

# MiniQMT / xtquant 数据 Skill

## 适用场景

- 用户明确提到 MiniQMT、QMT、xtdata、xtquant。
- 用户需要 A 股股票、指数、ETF、可转债、期货等本地行情数据。
- 用户需要历史 K 线、分钟线、tick、板块成分、交易日历、财务数据、合约信息、指数成分、Level2 或其他 xtquant 支持的数据。
- 用户需要查询 MiniQMT 账号、资产、持仓、当日委托或成交。
- 当前单资产策略闭环中需要获取可回测的价格数据。

## 配置来源

MiniQMT 配置从项目配置和环境变量读取：

- `configs/qmt.yaml`
- `.env`
- `QMT_USERDATA`
- `QMT_ACCOUNT_ID`
- `QMT_ACCOUNT_TYPE`

可以自己写 Python 脚本或在命令行中执行 Python。脚本里可以使用项目配置加载器读取配置，例如：

```python
from strategy_lab.config.loader import load_config_file
qmt_cfg = load_config_file("qmt")["qmt"]
```

不要在输出中展示敏感信息。账号查询需要 QMT 客户端已启动并登录。行情、财务、板块、交易日历等数据主要通过 `xtquant.xtdata` 获取。

## 执行原则

按照用户要求的数据和格式，你可以：

- 直接在命令行执行短 Python 命令。
- 在 `artifacts/data_agent_workspace/scripts` 中编写任务脚本，然后用 `python 脚本路径` 执行。
- 复制并改造 `reference_scripts` 里的参考脚本。

大型结果必须保存到 `artifacts/data_agent_workspace/data_files`，不要打印完整 DataFrame。终端只输出 JSON 摘要，包括输出路径、行数、字段、日期范围和少量预览。

## 回测数据格式

后续 Backtrader 回测最常用的是 OHLCV 数据：

```text
datetime, open, high, low, close, volume
```

建议额外保留：

```text
symbol, openinterest, amount, source
```

推荐最终字段：

```text
symbol, datetime, open, high, low, close, volume, openinterest, amount, source
```

说明：

- `datetime` 是交易日期或时间，日线可以是 `YYYYMMDD`。
- `open/high/low/close/volume` 是 Backtrader 最核心字段。
- `openinterest` 是 Backtrader 兼容字段，股票通常没有，填 `0` 即可。
- `amount` 不是 Backtrader 必需字段，但后续画像、流动性检查、复盘可能会用到。
- `source` 标记为 `miniqmt`，方便后续追踪数据来源。
- 如果用户要求特定字段名或文件格式，优先满足用户要求。

## 组合层 / 预算层多资产数据格式

如果任务要求为预算层或组合层生成多资产数据，优先生成两个文件：

```text
panel_ohlcv.parquet
returns_wide.parquet
```

`panel_ohlcv.parquet` 使用长表：

```text
symbol, datetime, open, high, low, close, volume, pctchange
```

`returns_wide.parquet` 使用宽表：

```text
index=datetime
columns=完整资产代码，例如 159819.SZ、512880.SH
values=日收益率
```

要求：
- `symbol` 和 `returns_wide` 的列名尽量使用完整代码，即 `代码.市场`。
- MiniQMT 或中间脚本如果返回 `159819`、`512880` 这类无后缀代码，应根据资产池或用户输入补齐后缀。
- 保存 `returns_wide.parquet` 前必须把 `datetime` 设为 index，而不是仅保存为普通列。
- 生成后检查 `returns_wide.index` 是否是 DatetimeIndex，且 `panel_ohlcv.symbol` 能与 `returns_wide.columns` 对齐。

## 参考脚本

参考脚本只作为模板，不是唯一入口。LLM 可以复制、改写，或自己写新的脚本。

用于回测 OHLCV 数据的参考脚本：

```text
src/strategy_lab/skills/data_agent/miniqmt/reference_scripts/fetch_ohlcv_for_backtest.py
```

参考命令：

```powershell
python src/strategy_lab/skills/data_agent/miniqmt/reference_scripts/fetch_ohlcv_for_backtest.py --symbol 600519.SH --period 1d --start 20240101 --end 20240110 --dividend-type front_ratio --output-path artifacts/data_agent_workspace/data_files/600519_ohlcv_20240101_20240110.parquet
```

如果要获取其他 xtquant 数据，不要被这个参考脚本限制。先查 `references/xtquant_docs.md`，确认函数签名，再写自己的脚本调用对应函数。

## 自写脚本模板

```python
from pathlib import Path
import json
import pandas as pd
from strategy_lab.config.loader import load_config_file

load_config_file("qmt")
from xtquant import xtdata
xtdata.enable_hello = False

project_root = Path.cwd()
output_path = project_root / "artifacts" / "data_agent_workspace" / "data_files" / "your_output.parquet"
output_path.parent.mkdir(parents=True, exist_ok=True)

# 在这里按 xtquant 文档调用目标函数
# raw = xtdata.some_function(...)
# df = ...

df.to_parquet(output_path, index=False)
print(json.dumps({
    "ok": True,
    "output_path": str(output_path),
    "row_count": len(df),
    "columns": list(df.columns),
}, ensure_ascii=False, default=str))
```

## 参考文档

完整 xtquant 文档位于：

```text
references/xtquant_docs.md
```

需要查接口时，先用 `grep` 搜索函数名、资产类型或关键词，再用 `read_file` 阅读相关片段。不要一次性读取全文。

常用接口线索：

- 历史 K 线下载：`download_history_data(stock_code, period, start_time, end_time, incrementally=True)`
- 批量历史 K 线下载：`download_history_data2(stock_list, period, start_time, end_time, callback=None, incrementally=True)`
- 历史行情读取：`get_market_data_ex([], [stock_code], period=period, start_time=start_time, end_time=end_time, count=-1, dividend_type=...)`
- 财务数据下载：`download_financial_data(stock_list, table_list=[])`
- 财务数据读取：`get_financial_data(stock_list, table_list=[], start_time='', end_time='', report_type='report_time')`
- 板块数据下载：`download_sector_data()`
- 板块列表：`get_sector_list()`
- 板块成分：`get_stock_list_in_sector(sector_name)`
- 交易日历：`get_trading_calendar(market, start_time='', end_time='')`

## 工作流程

1. 明确用户要什么数据、标的、时间范围、频率、字段和文件格式。
2. 如果是回测数据，优先生成 OHLCV 格式。
3. 需要其他数据时，先在 `references/xtquant_docs.md` 中定位函数。
4. 在 `artifacts/data_agent_workspace/scripts` 中写任务脚本，或复制参考脚本改造。
5. 运行脚本，结果保存到 `artifacts/data_agent_workspace/data_files`。
6. 检查最终文件是否存在、能否读取、行数、字段、日期范围、关键字段缺失情况。
7. 最终回答写明最终数据文件路径、数据来源、日期范围、行数、字段列表和必要说明。
