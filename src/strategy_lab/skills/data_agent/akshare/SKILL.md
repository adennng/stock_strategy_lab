---
name: akshare
description: "使用 AKShare 获取公开金融和经济数据。适用于股票、指数、基金、期货、债券、期权、外汇、宏观、利率、能源、另类数据等公开数据获取任务，尤其是用户要求 AKShare 或公开接口时。"
license: Proprietary project skill
---

# AKShare 数据 Skill

## 适用场景

- 用户明确提到 AKShare。
- 用户需要公开股票、指数、基金、宏观、债券、期货、期权、外汇、利率、能源或另类数据。
- MiniQMT 不适合或不可用，但公开数据源可以满足任务。

## 参考文档

AKShare 文档目录位于：

```text
references/akshare_docs/
```

文档大类包括：股票、期货、债券、期权、外汇、货币、现货、利率、私募基金、公募基金、指数、宏观、加密货币、银行、波动率、多因子、政策不确定性、能源、迁徙、高频、自然语言处理、QDII、另类数据、工具箱。

## 文档阅读规则

1. 先用 `glob` 查看分类文件。
2. 再用 `grep` 搜索函数名、资产类型或关键词。
3. 最后用 `read_file` 阅读必要片段。
4. 不要一次性读取股票、指数、宏观等大型文档全文。

## 常用场景和函数

- A 股实时行情：`stock_zh_a_spot_em()`
- A 股历史行情：先搜索 `stock_zh_a_hist`
- 个股基本信息：`stock_individual_info_em(symbol='000001')`
- 指数历史行情：先搜索 `index_zh_a_hist`
- 股票资金流、估值、财务、行业板块：先 grep `资金流`、`估值`、`财务`、`行业`、`hist`
- 公募基金：查看 `10_公募基金数据.md`
- 宏观数据：查看 `12_宏观数据.md`，按指标关键词搜索

## 代码和输出要求

- 可以写 Python 脚本并用 `execute` 运行。
- 不要打印完整 DataFrame。
- 大型结果必须保存为 parquet/csv/json。
- 终端只输出函数名、参数、保存路径、行数、列名、日期范围、错误摘要。
- AKShare 股票代码常见格式是 `600519`；如果用户输入 `600519.SH`，脚本中要按接口要求转换。
- AKShare 日期参数常见格式是 `YYYYMMDD`，但必须以文档为准。

## 组合层 / 预算层多资产数据格式

如果任务要求为预算层或组合层生成多资产数据，优先生成：

```text
panel_ohlcv.parquet
returns_wide.parquet
```

`panel_ohlcv.parquet` 是长表，至少包含：

```text
symbol, datetime, open, high, low, close, volume, pctchange
```

`returns_wide.parquet` 是宽表，必须使用 `datetime` 作为 index，列名使用完整资产代码，例如 `159819.SZ`、`512880.SH`，值为日收益率。

AKShare 很多接口返回无市场后缀代码，保存到项目文件前应根据用户资产池、任务说明或已知市场规则补齐后缀；如果无法确定，必须在 manifest 的 warnings 或 notes 中说明。保存后要检查 `returns_wide.index` 是否为 DatetimeIndex，且 `panel_ohlcv.symbol` 能与 `returns_wide.columns` 对齐。

## 任务完成要求

如果生成了可复用数据文件：

1. 做必要数据检查，例如文件是否存在、能否读取、行数、字段列表、日期范围、关键字段缺失情况。
2. 最终回答写明最终数据文件路径、数据来源、日期范围、行数、字段列表和必要说明。
