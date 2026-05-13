# XtQuant 官方文档整理版（XtData / XtTrade / 完整实例 / 常见问题）

> 来源：迅投知识库 XtQuant 文档。整理日期：2026-04-29。  
> 覆盖页面：
> - XtQuant.XtData 行情模块：`http://dict.thinktrader.net/nativeApi/xtdata.html`
> - XtQuant.Xttrade 交易模块：`http://dict.thinktrader.net/nativeApi/xttrader.html`
> - 完整实例：`http://dict.thinktrader.net/nativeApi/code_examples.html`
> - 常见问题：`http://dict.thinktrader.net/nativeApi/question_function.html`
>
> 说明：本文是对官网内容的结构化整理与转述，保留函数名、参数名、枚举名、字段名、返回类型等事实性技术信息；未逐字复刻官网全部长篇代码和长字段附录。需要完全逐字原文时，请以官网为准。

---

## 目录

1. [XtQuant.XtData 行情模块](#1-xtquantxtdata-行情模块)
2. [XtQuant.XtTrade 交易模块](#2-xtquantxttrade-交易模块)
3. [完整实例整理](#3-完整实例整理)
4. [常见问题整理](#4-常见问题整理)
5. [使用建议与注意事项](#5-使用建议与注意事项)

---

# 1. XtQuant.XtData 行情模块

## 1.1 模块定位

`xtdata` 是 `xtquant` 库中的行情数据模块，目标是以 Python 库形式提供精简、直接的数据能力，便于量化交易者在策略脚本中调用。主要覆盖：

- 行情数据：历史 K 线、实时 K 线、分笔数据、全推行情、Level2 数据等。
- 财务数据：资产负债表、利润表、现金流量表、股本、股东数、十大股东、每股指标等。
- 合约基础信息：合约详情、合约类型、交易日列表等。
- 板块与指数信息：板块列表、板块成分、指数成分权重等。
- 可转债、新股申购、ETF 申赎清单、节假日/交易日历等基础数据。

## 1.2 版本信息要点

| 日期 | 主要变化 |
|---|---|
| 2020-09-01 | 初稿。 |
| 2020-09-07 | 增加 `get_divid_factors`；完善合约信息、合约类型、交易日列表。 |
| 2020-09-13 | 增加财务数据接口；下载接口命名由 supply 调整为 download；修正 `volume` 拼写。 |
| 2020-11-23 | 合约基础信息字段类型调整；增加数据字典和 Level2 字段枚举。 |
| 2021-07-20 | 增加批量新版下载接口：`download_history_data2`、`download_financial_data2`。 |
| 2021-12-30 | 数据字典调整，补充撤单信息区分。 |
| 2022-06-27 | K线字段增加前收价、停牌标记。 |
| 2022-09-30 | 增加节假日、交易日历、交易时段相关接口。 |
| 2023-01-04 | 增加千档行情获取。 |
| 2023-01-31 | 增加可转债基础信息下载与获取。 |
| 2023-02-06 | 增加连接指定 IP/端口的 `reconnect`。 |
| 2023-02-07 | 支持 QMT 本地 Python 模式，优化多 QMT 连接场景。 |
| 2023-03-27 | 增加新股申购信息 `get_ipo_info`。 |
| 2023-04-13 | 本地 Python 模式支持运行 VBA 函数。 |
| 2023-08-21 | 支持投研版特色数据；`get_instrument_detail` 增加 `ExchangeCode`、`UniCode`；增加 `get_period_list`。 |
| 2023-10-11 | `get_market_data_ex` 支持 ETF 申赎清单；数据字典增加现金替代标志。 |
| 2023-11-09 | `download_history_data` 增加增量下载参数。 |
| 2023-11-22 | `get_trading_calendar` 不再支持 `tradetimes` 参数。 |
| 2023-11-27 | 增加 ETF 申赎清单下载与获取。 |
| 2023-11-28 | 增加节假日下载 `download_holiday_data`。 |
| 2023-12-27 | 板块成分接口支持北交所板块。 |
| 2024-01-19 | `get_market_data_ex` 支持期货历史主力合约、日线以上周期；`get_option_detail_data` 支持商品期权品种。 |
| 2024-01-22 | `get_trade_times` 改名为 `get_trading_time`。 |
| 2024-01-26 | `get_instrument_detail` 支持全部合约信息字段。 |
| 2024-05-15 | 增加 `get_full_kline`。 |
| 2024-05-27 | `get_stock_list_in_sector` 增加 `real_timetag` 参数。 |

## 1.3 运行逻辑

`xtdata` 本质上与 MiniQMT 建立连接，由 MiniQMT 处理行情请求并把结果返回 Python 层。行情服务器和可获取数据与 MiniQMT 端保持一致。使用前应注意：

1. 获取历史/本地数据前，需要确保 MiniQMT 已有对应数据；如不足，应先调用下载接口补充。
2. 订阅接口通过 callback 接收数据推送；订阅接收的数据通常会保存下来，同类数据无需额外补充。
3. 全推数据适合高订阅数量场景，单股订阅数量不宜过多。官网建议单股订阅数量不超过 50，超过时优先考虑全推。
4. 板块分类等静态信息更新频率低，一般按周或按日下载更新即可。

## 1.4 常用类型

### 1.4.1 合约代码 `stock_code`

格式：`code.market`，例如：

- `000001.SZ`
- `600000.SH`
- `000300.SH`

### 1.4.2 周期 `period`

Level1 数据：

| period | 含义 |
|---|---|
| `tick` | 分笔数据 |
| `1m` | 1分钟线 |
| `5m` | 5分钟线 |
| `15m` | 15分钟线 |
| `30m` | 30分钟线 |
| `1h` | 1小时线 |
| `1d` | 日线 |
| `1w` | 周线 |
| `1mon` | 月线 |
| `1q` | 季度线 |
| `1hy` | 半年线 |
| `1y` | 年线 |

投研版特色数据周期/数据类型：

| period | 含义 |
|---|---|
| `warehousereceipt` | 期货仓单 |
| `futureholderrank` | 期货席位 |
| `interactiveqa` | 互动问答 |
| `transactioncount1m` | 逐笔成交统计 1 分钟级 |
| `transactioncount1d` | 逐笔成交统计日级 |
| `delistchangebond` | 退市可转债信息 |
| `replacechangebond` | 待发可转债信息 |
| `specialtreatment` | ST 变更历史 |
| `northfinancechange1m` | 港股通资金流向 1 分钟级 |
| `northfinancechange1d` | 港股通资金流向日级 |
| `dividendplaninfo` | 红利分配方案信息 |
| `historycontract` | 过期合约列表 |
| `optionhistorycontract` | 期权历史信息 |
| `historymaincontract` | 历史主力合约 |
| `stoppricedata` | 涨跌停数据 |
| `snapshotindex` | 快照指标数据 |

### 1.4.3 时间范围参数

行情请求中的时间范围一般表示 `[start_time, end_time]` 闭区间，并在该区间内返回最后不多于 `count` 条数据：

| 参数 | 含义 |
|---|---|
| `start_time` | 起始时间；为空代表最早起始时间。 |
| `end_time` | 结束时间；为空代表最新结束时间。 |
| `count` | 数据条数；大于 0 限制返回数量，0 不返回，-1 返回全部。 |

常用完整范围写法：`start_time=''`、`end_time=''`、`count=-1`。实际使用时应避免过大范围导致返回变慢。

### 1.4.4 复权参数 `dividend_type`

| 值 | 含义 |
|---|---|
| `none` | 不复权 |
| `front` | 前复权 |
| `back` | 后复权 |
| `front_ratio` | 等比前复权 |
| `back_ratio` | 等比后复权 |

该参数主要用于 K 线复权，对 `tick` 等其他周期无效。

---

## 1.5 XtData 行情接口

### 1.5.1 订阅与回调

| 接口 | 签名 | 作用 | 主要参数 | 返回/备注 |
|---|---|---|---|---|
| 订阅单股行情 | `subscribe_quote(stock_code, period='1d', start_time='', end_time='', count=0, callback=None)` | 订阅单股行情，按 `period` 推送数据。 | `stock_code`、`period`、`start_time`、`end_time`、`count`、`callback`。 | 返回订阅号，成功大于 0，失败 -1。通常仅订阅时 `count=0`。 |
| 订阅全推行情 | `subscribe_whole_quote(code_list, callback=None)` | 订阅全推行情，数据类型为分笔数据。 | `code_list` 可传市场代码如 `['SH','SZ']` 或合约代码列表。 | 返回订阅号，成功大于 0，失败 -1。订阅后先返回当前最新全推数据。 |
| 反订阅行情 | `unsubscribe_quote(seq)` | 取消行情订阅。 | `seq` 为订阅号。 | 无返回。 |
| 阻塞接收回调 | `run()` | 阻塞当前线程，维持回调处理。 | 无。 | 通过循环 sleep 并检查连接状态实现。 |

callback 常见形式：

```python
def on_data(datas):
    for stock_code in datas:
        print(stock_code, datas[stock_code])
```

### 1.5.2 VBA/模型相关接口

| 接口 | 签名 | 作用 | 参数要点 | 返回/备注 |
|---|---|---|---|---|
| 订阅模型 | `subscribe_formula(formula_name, stock_code, period, start_time='', end_time='', count=-1, dividend_type=None, extend_param={}, callback=None)` | 订阅 VBA 模型运行结果。 | 模型名、主图代码、周期、起止时间、bar 数、复权方式、模型入参。 | 返回订阅 ID，失败 -1；需连接投研端并补充本地 K 线或分笔数据。 |
| 反订阅模型 | `unsubscribe_formula(subID)` | 取消模型订阅。 | `subID`。 | 成功 True，失败 False。 |
| 调用模型 | `call_formula(formula_name, stock_code, period, start_time='', end_time='', count=-1, dividend_type='none', extend_param={})` | 主动获取 VBA 模型结果。 | 可传模型参数；支持 `__basket` 作为组合模型股票池权重。 | 返回 dict，包含数据类型、时间列表、输出变量。 |
| 批量调用模型 | `call_formula_batch(formula_names, stock_codes, period, start_time='', end_time='', count=-1, dividend_type='none', extend_params=[])` | 批量运行模型。 | 模型名列表、股票列表、周期、参数列表等。 | 返回 list[dict]，包含 formula、stock、argument、result。 |
| 生成因子数据 | `generate_index_data(formula_name, formula_param={}, stock_list=[], period='1d', dividend_type='none', start_time='', end_time='', fill_mode='fixed', fill_value=float('nan'), result_path=None)` | 本地生成因子数据文件，格式 feather。 | 支持 `1m`、`5m`、`1d`；填充方式 `fixed` 或 `forward`；复权可选 `none/front_ratio/back_ratio`。 | 返回 None；需连接投研端，且模型存在于投研端。 |

### 1.5.3 主动获取行情数据

| 接口 | 签名 | 作用 | 返回 |
|---|---|---|---|
| 获取行情数据 | `get_market_data(field_list=[], stock_list=[], period='1d', start_time='', end_time='', count=-1, dividend_type='none', fill_data=True)` | 从缓存主动获取行情数据。 | K线周期返回 `{field: DataFrame}`；`tick` 返回 `{stock: ndarray}`。 |
| 获取本地行情数据 | `get_local_data(field_list=[], stock_list=[], period='1d', start_time='', end_time='', count=-1, dividend_type='none', fill_data=True, data_dir=data_dir)` | 直接读取本地数据文件，适合快速批量读历史行情。 | 返回结构与 `get_market_data` 类似；仅 Level1 数据。 |
| 获取全推数据 | `get_full_tick(code_list)` | 获取全推切面数据。 | `{stock: data}`。 |
| 获取除权数据 | `get_divid_factors(stock_code, start_time='', end_time='')` | 获取除权因子。 | `pd.DataFrame`。 |
| 获取最新交易日 K 线全推 | `get_full_kline(field_list=[], stock_list=[], period='1m', start_time='', end_time='', count=1, dividend_type='none', fill_data=True)` | 只支持最新一个交易日，不含历史值。 | `{field: DataFrame}`。 |

### 1.5.4 下载/补充数据

| 接口 | 签名 | 作用 | 备注 |
|---|---|---|---|
| 下载历史行情 | `download_history_data(stock_code, period, start_time='', end_time='', incrementally=None)` | 补充单个合约历史行情数据。 | 同步执行；`incrementally=None` 时由 `start_time` 控制是否增量，`start_time` 为空则从本地最后一条往后增量下载。 |
| 批量下载历史行情 | `download_history_data2(stock_list, period, start_time='', end_time='', callback=None, incrementally=None)` | 批量补充历史行情数据。 | callback 返回 `total`、`finished`、`stockcode`、`message` 等进度字段。 |
| 下载退市/过期合约信息 | `download_history_contracts()` | 下载过期/退市合约信息。 | 可通过过期板块配合 `get_stock_list_in_sector` 获取列表。 |
| 下载节假日数据 | `download_holiday_data()` | 下载节假日数据。 | 无返回。 |
| 下载可转债数据 | `download_cb_data()` | 下载全部可转债信息。 | 无返回。 |
| 下载 ETF 申赎清单 | `download_etf_info()` | 下载 ETF 申赎清单信息。 | 无返回。 |
| 下载财务数据 | `download_financial_data(stock_list, table_list=[])` | 下载财务数据。 | 同步执行。 |
| 批量下载财务数据 | `download_financial_data2(stock_list, table_list=[], start_time='', end_time='', callback=None)` | 批量下载财务数据，可按披露日期范围筛选。 | callback 返回 `total`、`finished`、`stockcode`、`message`。 |
| 下载板块分类信息 | `download_sector_data()` | 下载板块分类数据。 | 同步执行。 |
| 下载指数成分权重 | `download_index_weight()` | 下载指数成分权重。 | 同步执行。 |

### 1.5.5 日历、可转债、新股、ETF

| 接口 | 签名 | 作用 | 返回/备注 |
|---|---|---|---|
| 获取节假日 | `get_holidays()` | 获取截至当年的节假日日期。 | 返回 8 位日期字符串列表。 |
| 获取交易日历 | `get_trading_calendar(market, start_time='', end_time='')` | 获取指定市场交易日历。 | 返回交易日列表；如需未来交易日，需要先下载节假日数据。 |
| 获取可转债信息 | `get_cb_info(stockcode)` | 返回指定可转债代码的信息。 | 返回 dict；需先 `download_cb_data()`。 |
| 获取新股申购信息 | `get_ipo_info(start_time, end_time)` | 获取时间范围内新股申购信息。 | 返回 list[dict]，字段含证券代码、简称、市场、发行量、申购代码、申购上限、发行价、盈利标志、行业/发行后市盈率等。 |
| 获取可用周期 | `get_period_list()` | 获取可用周期列表。 | 返回 list。 |
| 获取 ETF 申赎清单 | `get_etf_info()` | 获取所有 ETF 申赎清单信息。 | 返回 dict。 |

### 1.5.6 财务数据接口

| 接口 | 签名 | 作用 | 参数/返回 |
|---|---|---|---|
| 获取财务数据 | `get_financial_data(stock_list, table_list=[], start_time='', end_time='', report_type='report_time')` | 获取财务报表数据。 | `table_list` 可选：`Balance`、`Income`、`CashFlow`、`Capital`、`Holdernum`、`Top10holder`、`Top10flowholder`、`Pershareindex`。`report_type` 可选 `report_time` 或 `announce_time`。返回 `{stock: {table: DataFrame}}`。 |

### 1.5.7 基础行情信息与板块接口

| 接口 | 签名 | 作用 | 返回/备注 |
|---|---|---|---|
| 获取合约基础信息 | `get_instrument_detail(stock_code, iscomplete)` | 获取合约详情。 | 返回 dict，找不到返回 None；`iscomplete=False` 返回基础字段，True 返回更多合约字段。 |
| 获取合约类型 | `get_instrument_type(stock_code)` | 判断合约类型。 | 返回 `{type: bool}`，常见 type：`index`、`stock`、`fund`、`etf`。 |
| 获取交易日列表 | `get_trading_dates(market, start_time='', end_time='', count=-1)` | 获取指定市场交易日列表。 | 返回时间戳列表。 |
| 获取板块列表 | `get_sector_list()` | 获取板块列表。 | 需要板块分类信息。 |
| 获取板块成分 | `get_stock_list_in_sector(sector_name)` | 获取板块成分股。 | 返回 list。 |
| 创建板块目录 | `create_sector_folder(parent_node, folder_name, overwrite)` | 创建自定义板块目录。 | 返回实际创建目录名。 |
| 创建板块 | `create_sector(parent_node, sector_name, overwrite)` | 创建板块。 | 返回实际创建板块名。 |
| 添加自定义板块 | `add_sector(sector_name, stock_list)` | 添加自定义板块。 | 无返回。 |
| 移除板块成分股 | `remove_stock_from_sector(sector_name, stock_list)` | 从板块移除股票。 | 成功 True，失败 False。 |
| 移除自定义板块 | `remove_sector(sector_name)` | 删除自定义板块。 | 无返回。 |
| 重置板块 | `reset_sector(sector_name, stock_list)` | 用新列表重置板块成分。 | 成功 True，失败 False。 |
| 获取指数成分权重 | `get_index_weight(index_code)` | 获取指数成分及权重。 | 返回 `{stock: weight}`；需先下载指数权重。 |

---

## 1.6 XtData 附录：字段与数据字典

### 1.6.1 行情字段

#### tick 分笔数据

常见字段：`time`、`lastPrice`、`open`、`high`、`low`、`lastClose`、`amount`、`volume`、`pvolume`、`stockStatus`、`openInt`、`lastSettlementPrice`、`askPrice`、`bidPrice`、`askVol`、`bidVol`、`transactionNum`。

#### K 线数据（1m / 5m / 1d 等）

常见字段：`time`、`open`、`high`、`low`、`close`、`volume`、`amount`、`settelementPrice`、`openInterest`、`preClose`、`suspendFlag`。其中 `suspendFlag`：0 正常，1 停牌，-1 当日起复牌。

#### 除权数据

字段：`interest`、`stockBonus`、`stockGift`、`allotNum`、`allotPrice`、`gugai`、`dr`。

#### Level2 实时行情快照 `l2quote`

字段：`time`、`lastPrice`、`open`、`high`、`low`、`amount`、`volume`、`pvolume`、`openInt`、`stockStatus`、`transactionNum`、`lastClose`、`lastSettlementPrice`、`settlementPrice`、`pe`、`askPrice`、`bidPrice`、`askVol`、`bidVol`。

#### Level2 逐笔委托 `l2order`

字段：`time`、`price`、`volume`、`entrustNo`、`entrustType`、`entrustDirection`。

#### Level2 逐笔成交 `l2transaction`

字段：`time`、`price`、`volume`、`amount`、`tradeIndex`、`buyNo`、`sellNo`、`tradeType`、`tradeFlag`。

#### Level2 行情补充 `l2quoteaux`

字段：`time`、`avgBidPrice`、`totalBidQuantity`、`avgOffPrice`、`totalOffQuantity`、`withdrawBidQuantity`、`withdrawBidAmount`、`withdrawOffQuantity`、`withdrawOffAmount`。

#### Level2 委买委卖一档委托队列 `l2orderqueue`

字段：`time`、`bidLevelPrice`、`bidLevelVolume`、`offerLevelPrice`、`offerLevelVolume`、`bidLevelNumber`、`offLevelNumber`。

### 1.6.2 行情数据字典

#### 证券状态

| 值 | 含义 |
|---|---|
| 0、10 | 未知/默认 |
| 11 | 开盘前 |
| 12 | 集合竞价 |
| 13 | 连续交易 |
| 14 | 休市 |
| 15 | 闭市 |
| 16 | 波动性中断 |
| 17 | 临时停牌 |
| 18 | 收盘集合竞价 |
| 19 | 盘中集合竞价 |
| 20 | 暂停交易至闭市 |
| 21 | 字段异常 |
| 22 | 盘后固定价格行情 |
| 23 | 盘后固定价格行情完毕 |

#### 委托类型 / 成交类型

适用于 Level2 逐笔委托 `entrustType` 和逐笔成交 `tradeType`。

| 值 | 含义 |
|---|---|
| 0 | 未知 |
| 1 | 正常交易业务 |
| 2 | 即时成交剩余撤销 |
| 3 | ETF 基金申报 |
| 4 | 最优五档即时成交剩余撤销 |
| 5 | 全额成交或撤销 |
| 6 | 本方最优价格 |
| 7 | 对手方最优价格 |

#### 委托方向

| 值 | 含义 |
|---|---|
| 1 | 买入 |
| 2 | 卖出 |
| 3 | 撤买（上交所） |
| 4 | 撤卖（上交所） |

#### 成交标志

| 值 | 含义 |
|---|---|
| 0 | 未知 |
| 1 | 外盘 |
| 2 | 内盘 |
| 3 | 撤单（深交所） |

#### ETF 现金替代标志

| 值 | 含义 |
|---|---|
| 0 | 禁止现金替代 |
| 1 | 允许现金替代 |
| 2 | 必须现金替代 |
| 3 | 非沪市退补现金替代 |
| 4 | 非沪市必须现金替代 |
| 5 | 非沪深退补现金替代 |
| 6 | 非沪深必须现金替代 |
| 7 | 港市退补现金替代 |
| 8 | 港市必须现金替代 |

### 1.6.3 财务数据字段列表说明

官网附录按以下报表列出大量字段：

- `Balance`：资产负债表，字段如 `m_anntime`、`m_timetag`、`cash_equivalents`、`tradable_fin_assets`、`tot_assets`、`shortterm_loan`、`total_current_liability` 等。
- `Income`：利润表，字段如 `m_anntime`、`m_timetag`、`oper_rev`、`oper_cost`、`oper_profit`、`tot_profit`、`net_profit_incl_min_int_inc`、`s_fa_eps_basic` 等。
- `CashFlow`：现金流量表，字段如 `m_anntime`、`m_timetag`、`goods_sale_and_service_render_cash`、`net_cash_flows_oper_act`、`net_cash_flows_inv_act`、`net_cash_flows_fnc_act`、`cash_cash_equ_end_period` 等。
- `PershareIndex`：主要指标。
- `Capital`：股本表。
- `Top10holder` / `Top10flowholder`：十大股东 / 十大流通股东。
- `Holdernum`：股东数。

> 使用时推荐通过 `get_financial_data` 返回的 DataFrame 字段实际检查，并以官网最新字段列表为准。

### 1.6.4 合约信息字段

`get_instrument_detail(..., iscomplete=True)` 可能返回大量合约字段，常见字段包括：

`ExchangeID`、`InstrumentID`、`InstrumentName`、`Abbreviation`、`ProductID`、`ProductName`、`UnderlyingCode`、`ExtendName`、`ExchangeCode`、`RzrkCode`、`UniCode`、`CreateDate`、`OpenDate`、`ExpireDate`、`PreClose`、`SettlementPrice`、`UpStopPrice`、`DownStopPrice`、`FloatVolume`、`TotalVolume`、`AccumulatedInterest`、`LongMarginRatio`、`ShortMarginRatio`、`PriceTick`、`VolumeMultiple`、`MainContract`、`MaxMarketOrderVolume`、`MinMarketOrderVolume`、`MaxLimitOrderVolume`、`MinLimitOrderVolume`、`LastVolume`、`InstrumentStatus`、`IsTrading`、`IsRecent`、`IsContinuous`、`secuCategory`、`secuAttri`、`HSGTFlag`、`BondParValue`、`OptUndlCode`、`OptUndlMarket`、`OptExercisePrice`、`ChargeType`、`ChargeOpen`、`ChargeClose`、`ChargeTodayOpen`、`ChargeTodayClose`、`OptionType`、`OpenInterestMultiple` 等。

---

# 2. XtQuant.XtTrade 交易模块

## 2.1 模块定位与运行逻辑

`xttrader` 封装策略交易所需的 Python API，能够与 MiniQMT 客户端交互，完成：

- 报单
- 撤单
- 查询资产
- 查询委托
- 查询成交
- 查询持仓
- 接收资金、委托、成交、持仓等变动主推消息

交易接口一般流程：

1. 创建 `XtQuantTrader(path, session_id)` 实例。
2. 创建账号对象 `StockAccount(account_id, account_type)`。
3. 编写并注册回调类 `XtQuantTraderCallback`。
4. 调用 `start()` 启动交易线程。
5. 调用 `connect()` 连接 MiniQMT。
6. 调用 `subscribe(account)` 订阅账号主推。
7. 使用报单、撤单、查询接口。
8. 使用 `run_forever()` 阻塞并等待回调。

## 2.2 版本信息要点

| 日期 | 主要变化 |
|---|---|
| 2020-09-01 | 初稿。 |
| 2020-10-14 | 持仓结构字段调整；投资备注修正。 |
| 2020-10-21 | 增加信用交易相关委托类型；更新运行依赖说明。 |
| 2020-11-13 | 增加信用交易结构、接口、异步撤单、下单失败/撤单失败主推、订阅与反订阅、系统接口等。 |
| 2020-11-19 | 增加账号状态主推、账号状态结构、账号状态枚举；补充异步回报推送。 |
| 2021-07-20 | 优化回调/主推机制，降低延迟波动；`run_forever()` 支持 Ctrl+C 跳出。 |
| 2022-06-27 | 委托查询支持仅查询可撤；增加新股申购额度、新股信息、账号信息查询。 |
| 2022-11-15 | 修复 `unsubscribe` 实现。 |
| 2022-11-17 | 交易数据字典格式调整。 |
| 2022-11-28 | 增加主动请求专用线程控制 `set_relaxed_response_order_enabled`。 |
| 2023-07-17 | `XtPosition` 成本价字段调整：`open_price`、`avg_price`。 |
| 2023-07-26 | 增加 `fund_transfer` 资金划拨。 |
| 2023-08-11 | 增加普通柜台资金/持仓查询。 |
| 2023-10-16 | 增加期货市价报价类型。 |
| 2023-10-20 | 委托、成交、持仓结构新增多空字段；委托与成交新增交易操作字段。 |
| 2023-11-03 | 增加券源行情、库存券约券、约券合约查询接口。 |
| 2024-01-02 | 委托类型增加 ETF 申赎。 |
| 2024-02-29 | 增加期货持仓统计查询。 |
| 2024-04-25 | 数据结构增加 `stock_code1` 以适配长代码。 |
| 2024-05-24 | 增加通用数据导出、通用数据查询。 |
| 2024-06-27 | 增加外部成交导入 `sync_transaction_from_external`。 |

---

## 2.3 XtTrade 数据字典

### 2.3.1 交易市场 `market`

| 市场 | 常量 |
|---|---|
| 上交所 | `xtconstant.SH_MARKET` |
| 深交所 | `xtconstant.SZ_MARKET` |
| 北交所 | `xtconstant.MARKET_ENUM_BEIJING` |
| 沪港通 | `xtconstant.MARKET_ENUM_SHANGHAI_HONGKONG_STOCK` |
| 深港通 | `xtconstant.MARKET_ENUM_SHENZHEN_HONGKONG_STOCK` |
| 上期所 | `xtconstant.MARKET_ENUM_SHANGHAI_FUTURE` |
| 大商所 | `xtconstant.MARKET_ENUM_DALIANG_FUTURE` |
| 郑商所 | `xtconstant.MARKET_ENUM_ZHENGZHOU_FUTURE` |
| 中金所 | `xtconstant.MARKET_ENUM_INDEX_FUTURE` |
| 能源中心 | `xtconstant.MARKET_ENUM_INTL_ENERGY_FUTURE` |
| 广期所 | `xtconstant.MARKET_ENUM_GUANGZHOU_FUTURE` |
| 上海期权 | `xtconstant.MARKET_ENUM_SHANGHAI_STOCK_OPTION` |
| 深圳期权 | `xtconstant.MARKET_ENUM_SHENZHEN_STOCK_OPTION` |

### 2.3.2 账号类型 `account_type`

| 类型 | 常量 |
|---|---|
| 期货 | `xtconstant.FUTURE_ACCOUNT` |
| 股票 | `xtconstant.SECURITY_ACCOUNT` |
| 信用 | `xtconstant.CREDIT_ACCOUNT` |
| 期货期权 | `xtconstant.FUTURE_OPTION_ACCOUNT` |
| 股票期权 | `xtconstant.STOCK_OPTION_ACCOUNT` |
| 沪港通 | `xtconstant.HUGANGTONG_ACCOUNT` |
| 深港通 | `xtconstant.SHENGANGTONG_ACCOUNT` |

### 2.3.3 委托类型 `order_type`

#### 股票

| 方向 | 常量 |
|---|---|
| 买入 | `xtconstant.STOCK_BUY` |
| 卖出 | `xtconstant.STOCK_SELL` |

#### 信用

| 操作 | 常量 |
|---|---|
| 担保品买入 | `xtconstant.CREDIT_BUY` |
| 担保品卖出 | `xtconstant.CREDIT_SELL` |
| 融资买入 | `xtconstant.CREDIT_FIN_BUY` |
| 融券卖出 | `xtconstant.CREDIT_SLO_SELL` |
| 买券还券 | `xtconstant.CREDIT_BUY_SECU_REPAY` |
| 直接还券 | `xtconstant.CREDIT_DIRECT_SECU_REPAY` |
| 卖券还款 | `xtconstant.CREDIT_SELL_SECU_REPAY` |
| 直接还款 | `xtconstant.CREDIT_DIRECT_CASH_REPAY` |
| 专项融资买入 | `xtconstant.CREDIT_FIN_BUY_SPECIAL` |
| 专项融券卖出 | `xtconstant.CREDIT_SLO_SELL_SPECIAL` |
| 专项买券还券 | `xtconstant.CREDIT_BUY_SECU_REPAY_SPECIAL` |
| 专项直接还券 | `xtconstant.CREDIT_DIRECT_SECU_REPAY_SPECIAL` |
| 专项卖券还款 | `xtconstant.CREDIT_SELL_SECU_REPAY_SPECIAL` |
| 专项直接还款 | `xtconstant.CREDIT_DIRECT_CASH_REPAY_SPECIAL` |

#### 期货六键风格

| 操作 | 常量 |
|---|---|
| 开多 | `xtconstant.FUTURE_OPEN_LONG` |
| 平昨多 | `xtconstant.FUTURE_CLOSE_LONG_HISTORY` |
| 平今多 | `xtconstant.FUTURE_CLOSE_LONG_TODAY` |
| 开空 | `xtconstant.FUTURE_OPEN_SHORT` |
| 平昨空 | `xtconstant.FUTURE_CLOSE_SHORT_HISTORY` |
| 平今空 | `xtconstant.FUTURE_CLOSE_SHORT_TODAY` |

#### 期货四键、两键、套利、展期与期权

- 四键风格：`FUTURE_CLOSE_LONG_TODAY_FIRST`、`FUTURE_CLOSE_LONG_HISTORY_FIRST`、`FUTURE_CLOSE_SHORT_TODAY_FIRST`、`FUTURE_CLOSE_SHORT_HISTORY_FIRST`。
- 两键风格：`FUTURE_CLOSE_LONG_TODAY_HISTORY_THEN_OPEN_SHORT`、`FUTURE_CLOSE_LONG_HISTORY_TODAY_THEN_OPEN_SHORT`、`FUTURE_CLOSE_SHORT_TODAY_HISTORY_THEN_OPEN_LONG`、`FUTURE_CLOSE_SHORT_HISTORY_TODAY_THEN_OPEN_LONG`、`FUTURE_OPEN`、`FUTURE_CLOSE`。
- 跨商品套利：`FUTURE_ARBITRAGE_OPEN`、`FUTURE_ARBITRAGE_CLOSE_HISTORY_FIRST`、`FUTURE_ARBITRAGE_CLOSE_TODAY_FIRST`。
- 期货展期：`FUTURE_RENEW_LONG_CLOSE_HISTORY_FIRST`、`FUTURE_RENEW_LONG_CLOSE_TODAY_FIRST`、`FUTURE_RENEW_SHORT_CLOSE_HISTORY_FIRST`、`FUTURE_RENEW_SHORT_CLOSE_TODAY_FIRST`。
- 股票期权：`STOCK_OPTION_BUY_OPEN`、`STOCK_OPTION_SELL_CLOSE`、`STOCK_OPTION_SELL_OPEN`、`STOCK_OPTION_BUY_CLOSE`、`STOCK_OPTION_COVERED_OPEN`、`STOCK_OPTION_COVERED_CLOSE`、`STOCK_OPTION_CALL_EXERCISE`、`STOCK_OPTION_PUT_EXERCISE`、`STOCK_OPTION_SECU_LOCK`、`STOCK_OPTION_SECU_UNLOCK`。
- 期货期权：`OPTION_FUTURE_OPTION_EXERCISE`。
- ETF 申赎：`ETF_PURCHASE`、`ETF_REDEMPTION`。

### 2.3.4 报价类型 `price_type`

> 官网提示：市价类型只在实盘环境生效，模拟环境不支持市价报单。

| 场景 | 常量/含义 |
|---|---|
| 通用 | `LATEST_PRICE` 最新价；`FIX_PRICE` 指定价。 |
| 郑商所期货 | `MARKET_BEST` 市价最优价。 |
| 大商所期货 | `MARKET_CANCEL` 市价即成剩撤；`MARKET_CANCEL_ALL` 市价全额成交或撤。 |
| 中金所期货 | `MARKET_CANCEL_1`、`MARKET_CANCEL_5`、`MARKET_CONVERT_1`、`MARKET_CONVERT_5`。 |
| 上交所/北交所股票 | `MARKET_SH_CONVERT_5_CANCEL`、`MARKET_SH_CONVERT_5_LIMIT`、`MARKET_PEER_PRICE_FIRST`、`MARKET_MINE_PRICE_FIRST`。 |
| 深交所股票/期权 | `MARKET_PEER_PRICE_FIRST`、`MARKET_MINE_PRICE_FIRST`、`MARKET_SZ_INSTBUSI_RESTCANCEL`、`MARKET_SZ_CONVERT_5_CANCEL`、`MARKET_SZ_FULL_OR_CANCEL`。 |

### 2.3.5 委托状态 `order_status`

| 常量 | 值 | 含义 |
|---|---:|---|
| `ORDER_UNREPORTED` | 48 | 未报 |
| `ORDER_WAIT_REPORTING` | 49 | 待报 |
| `ORDER_REPORTED` | 50 | 已报 |
| `ORDER_REPORTED_CANCEL` | 51 | 已报待撤 |
| `ORDER_PARTSUCC_CANCEL` | 52 | 部成待撤 |
| `ORDER_PART_CANCEL` | 53 | 部撤 |
| `ORDER_CANCELED` | 54 | 已撤 |
| `ORDER_PART_SUCC` | 55 | 部成 |
| `ORDER_SUCCEEDED` | 56 | 已成 |
| `ORDER_JUNK` | 57 | 废单 |
| `ORDER_UNKNOWN` | 255 | 未知 |

### 2.3.6 账号状态 `account_status`

| 常量 | 值 | 含义 |
|---|---:|---|
| `ACCOUNT_STATUS_INVALID` | -1 | 无效 |
| `ACCOUNT_STATUS_OK` | 0 | 正常 |
| `ACCOUNT_STATUS_WAITING_LOGIN` | 1 | 连接中 |
| `ACCOUNT_STATUSING` | 2 | 登录中 |
| `ACCOUNT_STATUS_FAIL` | 3 | 失败 |
| `ACCOUNT_STATUS_INITING` | 4 | 初始化中 |
| `ACCOUNT_STATUS_CORRECTING` | 5 | 数据刷新校正中 |
| `ACCOUNT_STATUS_CLOSED` | 6 | 收盘后 |
| `ACCOUNT_STATUS_ASSIS_FAIL` | 7 | 穿透副链接断开 |
| `ACCOUNT_STATUS_DISABLEBYSYS` | 8 | 系统停用 |
| `ACCOUNT_STATUS_DISABLEBYUSER` | 9 | 用户停用 |

### 2.3.7 资金划拨方向 `transfer_direction`

| 常量 | 值 | 含义 |
|---|---:|---|
| `FUNDS_TRANSFER_NORMAL_TO_SPEED` | 510 | 普通柜台到极速柜台 |
| `FUNDS_TRANSFER_SPEED_TO_NORMAL` | 511 | 极速柜台到普通柜台 |
| `NODE_FUNDS_TRANSFER_SH_TO_SZ` | 512 | 上海节点到深圳节点 |
| `NODE_FUNDS_TRANSFER_SZ_TO_SH` | 513 | 深圳节点到上海节点 |

### 2.3.8 多空方向与交易操作

| 字段 | 常量 | 值 | 含义 |
|---|---|---:|---|
| `direction` | `DIRECTION_FLAG_LONG` | 48 | 多 |
| `direction` | `DIRECTION_FLAG_SHORT` | 49 | 空 |
| `offset_flag` | `OFFSET_FLAG_OPEN` | 48 | 买入/开仓 |
| `offset_flag` | `OFFSET_FLAG_CLOSE` | 49 | 卖出/平仓 |
| `offset_flag` | `OFFSET_FLAG_FORCECLOSE` | 50 | 强平 |
| `offset_flag` | `OFFSET_FLAG_CLOSETODAY` | 51 | 平今 |
| `offset_flag` | `OFFSET_FLAG_ClOSEYESTERDAY` | 52 | 平昨 |
| `offset_flag` | `OFFSET_FLAG_FORCEOFF` | 53 | 强减 |
| `offset_flag` | `OFFSET_FLAG_LOCALFORCECLOSE` | 54 | 本地强平 |

---

## 2.4 XtTrade 数据结构

### 2.4.1 `XtAsset` 资产

| 属性 | 类型 | 含义 |
|---|---|---|
| `account_type` | int | 账号类型 |
| `account_id` | str | 资金账号 |
| `cash` | float | 可用金额 |
| `frozen_cash` | float | 冻结金额 |
| `market_value` | float | 持仓市值 |
| `total_asset` | float | 总资产 |

### 2.4.2 `XtOrder` 委托

| 属性 | 类型 | 含义 |
|---|---|---|
| `account_type` | int | 账号类型 |
| `account_id` | str | 资金账号 |
| `stock_code` | str | 证券代码 |
| `order_id` | int | 订单编号 |
| `order_sysid` | str | 柜台合同编号 |
| `order_time` | int | 报单时间 |
| `order_type` | int | 委托类型 |
| `order_volume` | int | 委托数量 |
| `price_type` | int | 报价类型，返回枚举与下单传入枚举不完全等价但功能一致。 |
| `price` | float | 委托价格 |
| `traded_volume` | int | 成交数量 |
| `traded_price` | float | 成交均价 |
| `order_status` | int | 委托状态 |
| `status_msg` | str | 委托状态描述/废单原因 |
| `strategy_name` | str | 策略名称 |
| `order_remark` | str | 委托备注，极简客户端最大约 24 个英文字符。 |
| `direction` | int | 多空方向，股票不适用。 |
| `offset_flag` | int | 交易操作，用于区分股票买卖、期货开平仓、期权买卖等。 |

### 2.4.3 `XtTrade` 成交

字段：`account_type`、`account_id`、`stock_code`、`order_type`、`traded_id`、`traded_time`、`traded_price`、`traded_volume`、`traded_amount`、`order_id`、`order_sysid`、`strategy_name`、`order_remark`、`direction`、`offset_flag`。

### 2.4.4 `XtPosition` 持仓

字段：`account_type`、`account_id`、`stock_code`、`volume`、`can_use_volume`、`open_price`、`market_value`、`frozen_volume`、`on_road_volume`、`yesterday_volume`、`avg_price`、`direction`。

### 2.4.5 `XtPositionStatistics` 期货持仓统计

主要字段：账户、市场、品种、合约、方向、投保类型、持仓数量、昨仓/今仓、可平量、持仓成本、均价、持仓盈亏、浮动盈亏、保证金、手续费、合约价值、最新价、当日涨幅、资产占比、保证金占比、平仓盈亏等。

### 2.4.6 异步与错误反馈结构

| 结构 | 主要字段 |
|---|---|
| `XtOrderResponse` | `account_type`、`account_id`、`order_id`、`strategy_name`、`order_remark`、`seq`。 |
| `XtCancelOrderResponse` | `account_type`、`account_id`、`order_id`、`order_sysid`、`cancel_result`、`seq`。 |
| `XtOrderError` | `account_type`、`account_id`、`order_id`、`error_id`、`error_msg`、`strategy_name`、`order_remark`。 |
| `XtCancelError` | `account_type`、`account_id`、`order_id`、`market`、`order_sysid`、`error_id`、`error_msg`。 |

### 2.4.7 信用与约券相关结构

| 结构 | 主要内容 |
|---|---|
| `XtCreditDetail` | 信用账户状态、更新时间、总资产、可用金额、持仓盈亏、总市值、可取金额、股票/基金市值、总负债、可用保证金、维持担保比例、融资/融券授信与已用额度等。 |
| `StkCompacts` | 负债合约类型、头寸来源、市场、开仓日期、合约证券数量、未还数量、到期日、合约金额/息费、已还金额/息费、证券代码、合约编号、定位串。 |
| `CreditSubjects` | 融券状态、融资状态、市场、融券/融资保证金比例、证券代码。 |
| `CreditSloCode` | 头寸来源、市场、可融券数量、证券代码。 |
| `CreditAssure` | 是否可做担保、市场、折算比例、证券代码。 |
| `XtAccountStatus` | 账号类型、资金账号、账号状态。 |
| `XtAccountInfo` | 账号类型、资金账号、平台号、账号分类、登录状态等。 |
| `XtSmtAppointmentResponse` | 异步请求序号、申请是否成功、反馈信息、资券申请编号。 |

---

## 2.5 XtTrade API 说明

### 2.5.1 系统设置接口

| 接口 | 签名 | 作用 | 返回/备注 |
|---|---|---|---|
| 创建 API 实例 | `XtQuantTrader(path, session_id)` | 创建 XtQuant API 实例。 | `path` 为 MiniQMT 的 `userdata_mini` 完整路径；`session_id` 需避免重复。 |
| 注册回调类 | `register_callback(callback)` | 注册回调类实例。 | 无返回。 |
| 准备 API 环境 | `start()` | 启动交易线程。 | 无返回。 |
| 创建连接 | `connect()` | 连接 MiniQMT。 | 成功返回 0，失败返回非 0；断开后不会自动重连。 |
| 停止运行 | `stop()` | 停止 API。 | 无返回。 |
| 阻塞等待 | `run_forever()` | 阻塞当前线程，直到 `stop()` 被调用。 | 无返回。 |
| 主动请求专用线程 | `set_relaxed_response_order_enabled(enabled)` | 控制主动请求接口是否由额外线程返回。 | 在推送回调中同步查询可避免卡住，但数据时序可能不确定；通常推荐优先使用异步查询。 |

### 2.5.2 操作接口

| 接口 | 签名 | 作用 | 返回/备注 |
|---|---|---|---|
| 订阅账号信息 | `subscribe(account)` | 订阅资金、委托、成交、持仓等主推。 | 成功 0，失败 -1。 |
| 反订阅账号信息 | `unsubscribe(account)` | 取消账号订阅。 | 成功 0，失败 -1。 |
| 股票同步报单 | `order_stock(account, stock_code, order_type, order_volume, price_type, price, strategy_name, order_remark)` | 下单。 | 成功返回大于 0 的订单编号，失败 -1。 |
| 股票异步报单 | `order_stock_async(account, stock_code, order_type, order_volume, price_type, price, strategy_name, order_remark)` | 异步下单。 | 成功返回大于 0 的 seq，失败 -1；正常返回后可在 `on_order_stock_async_response` 收到反馈。 |
| 按订单号同步撤单 | `cancel_order_stock(account, order_id)` | 根据订单编号撤单。 | 成功 0，失败 -1；期货场景中 `order_id` 对应 `order_sysid`。 |
| 按柜台合同号同步撤单 | `cancel_order_stock_sysid(account, market, order_sysid)` | 根据券商柜台合同编号撤单。 | 成功 0，失败 -1。 |
| 按订单号异步撤单 | `cancel_order_stock_async(account, order_id)` | 异步撤单。 | 成功返回大于 0 的 seq，失败 -1。 |
| 按柜台合同号异步撤单 | `cancel_order_stock_sysid_async(account, market, order_sysid)` | 根据柜台合同编号异步撤单。 | 成功返回大于 0 的 seq，失败 -1。 |
| 资金划拨 | `fund_transfer(account, transfer_direction, price)` | 资金划拨。 | 返回 `(success, msg)`。 |
| 外部交易数据录入 | `sync_transaction_from_external(operation, data_type, account, deal_list)` | 外部成交导入/维护。 | `operation` 支持 `UPDATE`、`REPLACE`、`ADD`、`DELETE`；`data_type` 支持 `DEAL`。返回 dict。 |

### 2.5.3 股票查询接口

| 接口 | 签名 | 作用 | 返回 |
|---|---|---|---|
| 资产查询 | `query_stock_asset(account)` | 查询账号资产。 | `XtAsset` 或 None。 |
| 委托查询 | `query_stock_orders(account, cancelable_only=False)` | 查询当日委托，可只查可撤。 | `list[XtOrder]` 或 None。 |
| 成交查询 | `query_stock_trades(account)` | 查询当日成交。 | `list[XtTrade]` 或 None。 |
| 持仓查询 | `query_stock_positions(account)` | 查询最新持仓。 | `list[XtPosition]` 或 None。 |
| 期货持仓统计 | `query_position_statistics(account)` | 查询期货账号持仓统计。 | `list[XtPositionStatistics]` 或 None。 |

### 2.5.4 信用查询接口

| 接口 | 签名 | 作用 | 返回 |
|---|---|---|---|
| 信用资产查询 | `query_credit_detail(account)` | 查询信用账号资产。 | `list[XtCreditDetail]` 或 None。 |
| 负债合约查询 | `query_stk_compacts(account)` | 查询负债合约。 | `list[StkCompacts]` 或 None。 |
| 融资融券标的查询 | `query_credit_subjects(account)` | 查询融资融券标的。 | `list[CreditSubjects]` 或 None。 |
| 可融券数据查询 | `query_credit_slo_code(account)` | 查询可融券数据。 | `list[CreditSloCode]` 或 None。 |
| 标的担保品查询 | `query_credit_assure(account)` | 查询标的担保品。 | `list[CreditAssure]` 或 None。 |

### 2.5.5 其他查询接口

| 接口 | 签名 | 作用 | 返回/字段 |
|---|---|---|---|
| 新股申购额度 | `query_new_purchase_limit(account)` | 查询新股申购额度。 | `{type: number}`；`KCB` 科创板、`SH` 上海、`SZ` 深圳；债券申购额度固定 10000 张。 |
| 当日新股新债信息 | `query_ipo_data()` | 查询当日新股新债。 | `{stock: info}`；字段含 `name`、`type`、`minPurchaseNum`、`maxPurchaseNum`、`purchaseDate`、`issuePrice`。 |
| 账号信息查询 | `query_account_infos()` | 查询全部资金账号。 | `list[XtAccountInfo]`。 |
| 账号状态查询 | `query_account_status()` | 查询全部账号状态。 | `list[XtAccountStatus]`。 |
| 普通柜台资金查询 | `query_com_fund(account)` | 划拨业务查询普通柜台资金。 | dict，含 `success`、`error/erro`、`currentBalance`、`enableBalance`、`fetchBalance`、`assetBalance`、`marketValue`、`debt` 等。 |
| 普通柜台持仓查询 | `query_com_position(account)` | 划拨业务查询普通柜台持仓。 | list[dict]，含证券代码、名称、总量、可用量、最新价、成本价、盈亏、市值等。 |
| 通用数据导出 | `export_data(account, result_path, data_type, start_time=None, end_time=None, user_param={})` | 导出通用数据到 CSV。 | dict 结果反馈。 |
| 通用数据查询 | `query_data(account, result_path, data_type, start_time=None, end_time=None, user_param={})` | 调用导出接口后读取数据并删除文件。 | dict 或表格型数据。 |

### 2.5.6 约券相关接口

| 接口 | 签名 | 作用 | 返回/字段 |
|---|---|---|---|
| 券源行情查询 | `smt_query_quoter(account)` | 查询券源信息。 | list[dict]，字段含金融品种、证券类型、期限、证券代码/名称、市场、占用利率、罚息利率、提前归还利率、使用利率、可融券数量等。 |
| 库存券约券申请 | `smt_negotiate_order_async(account, src_group_id, order_code, date, amount, apply_rate, dict_param={})` | 异步库存券约券申请。 | 返回 seq；`dict_param` 可含 `subFareRate`、`fineRate`。 |
| 约券合约查询 | `smt_query_compact(account)` | 查询约券合约。 | list[dict]，字段含创建日期、合约编号、申请编号、来源组、品种、市场、代码、期限、合约数量/金额、利息、罚息、状态、展期信息等。 |

### 2.5.7 回调类接口

| 回调 | 签名 | 触发含义 | 参数 |
|---|---|---|---|
| 连接断开 | `on_disconnected()` | 失去连接。 | 无。 |
| 账号状态推送 | `on_account_status(data)` | 账号状态变动。 | `XtAccountStatus`。 |
| 委托信息推送 | `on_stock_order(data)` | 委托状态变化、成交数量变化等。 | `XtOrder`。 |
| 成交信息推送 | `on_stock_trade(data)` | 成交信息变动。 | `XtTrade`。 |
| 下单失败推送 | `on_order_error(data)` | 下单失败。 | `XtOrderError`。 |
| 撤单失败推送 | `on_cancel_error(data)` | 撤单失败。 | `XtCancelError`。 |
| 异步下单回报 | `on_order_stock_async_response(data)` | 异步下单反馈。 | `XtOrderResponse`。 |
| 约券异步回报 | `on_smt_appointment_async_response(data)` | 约券异步接口反馈。 | `XtSmtAppointmentResponse`。 |

---

# 3. 完整实例整理

> 本节按官网“完整实例”页面整理示例目的、关键流程和需要调整的参数。官网给出了完整代码；本文为结构化摘要，避免逐字复制长代码。

## 3.1 行情示例

### 3.1.1 获取行情示例

核心流程：

1. 导入 `from xtquant import xtdata`。
2. 设置标的列表，例如 `['000001.SZ']`。
3. 设置周期，例如 `period='1d'`。
4. 下载行情、财务、板块数据：
   - `xtdata.download_history_data(code, period=period, incrementally=True)`
   - `xtdata.download_financial_data(code_list)`
   - `xtdata.download_sector_data()`
5. 读取本地历史行情：`xtdata.get_market_data_ex([], code_list, period=period, count=-1)`。
6. 订阅实时行情：`xtdata.subscribe_quote(code, period=period, count=-1)`。
7. 循环读取订阅后的行情。
8. 如需回调，在 `subscribe_quote(..., callback=f)` 中传入回调函数，并使用 `xtdata.run()` 阻塞程序。

### 3.1.2 连接 VIP 服务器

关键流程：

1. 导入 `xtquant.xtdatacenter as xtdc` 与 `xtdata`。
2. 通过 `xtdc.set_token(...)` 设置 token；token 可在投研用户中心获取。
3. 用 `xtdc.set_allow_optmize_address(addr_list)` 设置优选服务器连接池。
4. 如需 K 线全推，调用 `xtdc.set_kline_mirror_enabled(True)`。
5. 调用 `xtdc.init()` 初始化。
6. 用 `xtdc.listen(port=...)` 指定或分配监听端口。
7. 使用 `xtdata.connect(port=port)` 连接。
8. 可通过 `xtdata.get_quote_server_status()` 查看服务器状态。

### 3.1.3 连接指定服务器

关键流程：

1. 设置服务器 `ip`、`port`、用户名和密码；token 方式可不填账号密码。
2. 通过 `xtdata.watch_quote_server_status(func)` 注册连接状态回调。
3. 创建 `xtdata.QuoteServer(info)` 并调用 `connect()`。
4. 使用 `xtdata.get_quote_server_status()` 获取当前连接站点。
5. 等待状态回调，判断是否连接成功。

### 3.1.4 指定初始化行情连接范围

关键流程：

1. 使用 `xtdc.set_data_home_dir(...)` 设置数据目录。
2. 设置 token。
3. 使用 `xtdc.set_allow_optmize_address(opt_list)` 限定优选行情站点范围。
4. 使用 `xtdc.set_kline_mirror_markets(['SH','SZ','BJ'])` 开启指定市场 K 线全推。
5. 使用 `xtdc.set_init_markets(init_markets)` 指定初始化市场列表。
6. `xtdc.init(start_local_service=False)` 初始化。
7. `xtdc.listen(port=(58620, 58650))` 指定端口范围。
8. `xtdata.connect(port=listen_port)` 连接。

### 3.1.5 订阅全推数据 / 下载历史数据

示例逻辑：

1. 调用 `xtdata.get_full_tick([code])` 获取全推快照。
2. 调用 `xtdata.download_history_data(code, period='1m', start_time='20230701')` 下载历史数据。
3. 定义回调函数，并用 `xtdata.subscribe_quote(code, period='1m', count=-1, callback=callback_func)` 订阅实时行情。
4. 使用 `xtdata.get_market_data(...)` 一次性取数据。
5. 使用 `xtdata.run()` 阻塞。

### 3.1.6 获取对手价

示例说明以卖出为例：

- 通过 `xtdata.get_full_tick(code_list)` 获取 tick 数据。
- 卖出时通常取买一价作为对手价。
- 若买一价为 0，可用最新价兜底。

### 3.1.7 复权计算方式

官网示例展示了四类复权计算方式：

- 等比前复权
- 等比后复权
- 前复权
- 后复权

关键数据来源：

- 除权数据：`xtdata.get_divid_factors(stock_code)`
- 原始价格：`xtdata.get_market_data(field_list, [stock], '1d', dividend_type='none')`

### 3.1.8 商品期权代码映射期货合约

示例实现 `get_option_underline_code(code)`：

- 不适用于股指期货期权与 ETF 期权。
- 根据期权交易所后缀判断交易所映射。
- 通过 `xtdata.get_option_detail_data(code)` 获取期权详情。
- 组合 `OptUndlCode` 与 `OptUndlMarket` 得到底层期货合约代码。

### 3.1.9 指数代码映射金融期货合约

示例实现 `get_financial_futures_code_from_index(index_code)`：

- 从 `xtdata.get_stock_list_in_sector('中金所')` 获取中金所合约列表。
- 用正则筛选期货代码。
- 通过合约详情中的 `OptUndlCode` 与 `OptUndlMarket` 判断对应指数。
- 返回对应期货合约列表。

### 3.1.10 高频因子数据共享

官网示例分为上传和获取：

- 使用 `xtquant.invadv.InvAdv()`。
- 设置远程服务地址、用户、密码并连接。
- 查询可用板块列表。
- 创建板块并上传“代码|权重”形式的内容。
- 获取端循环检查板块是否过期，若有更新则拉取板块数据。

## 3.2 交易示例

### 3.2.1 简单买卖各一笔示例

需要调整的参数：

- `path`：本地客户端路径。券商端指向 `{安装目录}\userdata_mini`，投研端指向 `{安装目录}\userdata`。
- 资金账号：改为自己的账号。

核心流程：

1. 导入 `xtdata`、`XtQuantTrader`、`XtQuantTraderCallback`、`StockAccount`、`xtconstant`。
2. 定义状态容器，记录已买入列表。
3. 下载板块数据，获取沪深 A 股列表。
4. 定义交易回调类，覆盖断连、委托、成交、下单失败、撤单失败、异步下单、账号状态等回调。
5. 创建 `XtQuantTrader(path, session_id)`。
6. 创建 `StockAccount(account_id, 'STOCK')`。
7. 注册回调、启动、连接、订阅账号。
8. 查询资产、委托、成交、持仓等。
9. 发起异步买入/卖出示例，并阻塞等待回调。

### 3.2.2 单股订阅实盘示例

需要调整：

- `path` 本地客户端路径。
- 资金账号。

核心逻辑：

1. 下载板块数据并获取沪深 A 股列表。
2. 定义行情回调 `f(data)`。
3. 对订阅标的逐个计算涨幅：`当前价 / 前收价 - 1`。
4. 当涨幅超过阈值且未买入过时，用异步接口买入 100 股。
5. 通过 `xtdata.subscribe_quote(code, '1d', callback=f)` 订阅标的。
6. 使用交易接口阻塞运行。

### 3.2.3 全推订阅实盘示例

核心逻辑与单股订阅类似，但使用 `xtdata.subscribe_whole_quote(['SH','SZ'], callback=f)` 订阅全市场全推。官网示例中全推回调注册处于注释状态，强调需确认理解效果后再开启，以避免误触实盘下单。

### 3.2.4 定时判断实盘示例

核心流程：

1. 订阅标的并下载历史 K 线。
2. 在交易时间内循环。
3. 每轮用 `xtdata.get_market_data_ex(['close'], code_list, period='1d', start_time='20240101')` 获取数据。
4. 调用交易判断函数。
5. 每轮 sleep 三秒。
6. 非交易时间退出循环，再调用 `run_forever()` 保持回调。

### 3.2.5 交易接口重连

官网示例用于演示交易连接断开时如何重连，并提示：

- 示例不是线程安全的，仅演示重连写法。
- 不能无限制 `while True` 创建连接，因为每次连接都会用 session_id 创建对接文件，可能占满硬盘。
- 应控制 session_id 在有限范围内尝试，如示例中设定一个 session_id 列表并随机遍历。
- 所有 session_id 尝试失败后，应抛出异常或通知人工处理。

示例策略用均线逻辑演示：

- 订阅 5 分钟行情。
- 每隔数秒获取价格数据。
- 计算 MA5、MA10。
- 根据信号调用异步下单。

### 3.2.6 指定 session id 范围连接交易

示例展示：

1. 封装 `connect(path, session)`。
2. 构造 session_id 列表，例如 100 到 199。
3. 随机打乱后逐个尝试连接。
4. 连接成功返回 trader；全部失败则抛出异常。
5. 主程序进入循环保持运行。

### 3.2.7 信用账号执行还款

示例用于信用账号还款：

- 账号类型使用 `CREDIT`。
- 还款金额参数 `repay_money` 以元为单位。
- 通过 `order_stock` 调用 `xtconstant.CREDIT_DIRECT_CASH_REPAY` 发起直接还款。
- 示例强调策略仅供参考，实盘损失需自行承担。

### 3.2.8 下单后通过回调撤单

示例展示异步下单到回调撤单的流程：

1. `order_stock_async` 发出委托。
2. `on_order_stock_async_response` 收到异步下单反馈。
3. `on_stock_order` 收到委托信息。
4. 在委托状态满足条件时调用 `cancel_order_stock_sysid_async` 发起异步撤单。
5. `on_cancel_order_stock_async_response` 收到撤单反馈。
6. 再次通过 `on_stock_order` 收到委托状态变化。

示例中强调要读取 `XtOrder` 的核心字段，如账号类型、资金账号、证券代码、订单编号、柜台合同编号、报单时间、委托类型、委托数量、报价类型、价格、成交数量、成交均价、委托状态、状态描述、策略名称、委托备注、多空方向、交易操作等。

---

# 4. 常见问题整理

## 4.1 导入 xtquant 提示 `NO module named 'xtquant.IPythonAPiClient'`

原因与处理：

- 当前 xtquant 支持 64 位 Python 3.6 到 3.11。
- 如出现该错误，优先检查 Python 版本与位数，换用支持版本重试。

## 4.2 连接 xtquant 失败，返回 -1

排查顺序：

1. 客户端是否以极简模式登录；登录 QMT 时需要勾选极简模式。
2. 检查路径是否正确：
   - MiniQMT：指定到安装目录下 `\userdata_mini`。
   - 投研端：指定到安装目录下 `\userdata`。
3. 如果客户端安装在 C 盘，可能存在权限问题，需要管理员权限运行策略；官网不建议安装在 C 盘。
4. 可通过在目标路径写入测试文件检查是否有写权限；若出现 `PermissionError`，说明存在文件权限问题。
5. 路径正确但仍失败时，更换 `session`（任意整数即可）。同一个 session 的两次 Python 进程 `connect` 之间必须超过 3 秒。
6. 如果 MiniQMT 开启后，`userdata_mini` 下没有 `up_queue_xtquant` 文件，说明当前用户可能没有对应函数下单权限，需要联系券商开通。

## 4.3 执行 `xtdatacenter.init` 时提示监听 58609 端口失败

原因：58609 端口被其他程序占用，常见于启动了两个 xtdc 服务。

处理方式：

1. 使用 `xtdc.init(False)` 后，通过 `xtdc.listen(port)` 指定自定义端口。
2. 关闭所有 Python 程序或重启电脑后，再执行 `xtdc.init`。

## 4.4 下单后查询委托，投资备注只有前半部分

原因：极简客户端的 `order_remark` 字段有长度限制，最大约 24 个英文字符；一个中文通常占 3 个字符，超出部分会被丢弃。大 QMT 没有该限制。

## 4.5 `userdata_mini` 目录下生成大量 `down_queue` 文件

原因：这些文件由 xttrade 指定新的 session 产生。

处理：

- 可参考“指定 session id 范围连接交易”示例控制 session 范围，避免大量文件产生。
- 已产生的文件可以删除。

---

# 5. 使用建议与注意事项

## 5.1 行情模块建议

1. 历史数据先下载，再读取：`download_history_data` / `download_history_data2` → `get_market_data` / `get_market_data_ex`。
2. 实时行情使用订阅：单标的少量订阅可用 `subscribe_quote`；高订阅数量优先使用 `subscribe_whole_quote`。
3. 订阅后需要 `xtdata.run()` 或其他阻塞机制维持进程。
4. 大范围历史数据请求容易变慢，建议控制 `start_time`、`end_time` 和 `count`。
5. Level2 数据需要相应权限；Level2 实时数据通常不具备跨交易日历史存储。
6. 静态信息如板块分类、指数权重、节假日、可转债数据无需频繁下载，按需定期更新即可。

## 5.2 交易模块建议

1. `session_id` 要避免重复，不同策略使用不同 session。
2. 连接失败时优先检查路径、极简模式、管理员权限、券商权限、session 间隔。
3. 实盘下单前先确认 `account_id`、`account_type`、`stock_code`、`order_type`、`price_type`、`price`、`order_volume`。
4. 市价委托只在实盘环境中生效，模拟环境不支持市价报单。
5. `order_remark` 在极简客户端有长度限制，不要依赖过长中文备注做交易追踪。
6. 在回调中调用同步查询接口可能导致时序/阻塞问题；如需使用，评估 `set_relaxed_response_order_enabled(True)` 或改用异步查询。
7. 重连逻辑不要无限创建 session；应限制 session 范围并配套人工告警。
8. 官网交易示例仅供写法参考，不能直接视为实盘策略。

---

## 附：最小交易连接骨架（改写示例）

```python
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant

class Callback(XtQuantTraderCallback):
    def on_disconnected(self):
        print('connection lost')

    def on_stock_order(self, order):
        print('order:', order.stock_code, order.order_status, order.order_id)

    def on_stock_trade(self, trade):
        print('trade:', trade.stock_code, trade.traded_volume, trade.traded_price)

path = r'D:\qmt\userdata_mini'
session_id = 100001
account = StockAccount('你的资金账号', 'STOCK')

trader = XtQuantTrader(path, session_id)
trader.register_callback(Callback())
trader.start()

if trader.connect() != 0:
    raise RuntimeError('连接 MiniQMT 失败')

if trader.subscribe(account) != 0:
    raise RuntimeError('账号订阅失败')

order_id = trader.order_stock(
    account,
    '600000.SH',
    xtconstant.STOCK_BUY,
    100,
    xtconstant.FIX_PRICE,
    10.50,
    'strategy_name',
    'remark'
)
print('order_id:', order_id)
trader.run_forever()
```

## 附：最小行情读取骨架（改写示例）

```python
from xtquant import xtdata

code_list = ['000001.SZ']
period = '1d'

for code in code_list:
    xtdata.download_history_data(code, period=period, incrementally=True)

data = xtdata.get_market_data_ex([], code_list, period=period, count=-1)
print(data)

def on_quote(datas):
    print(datas)

for code in code_list:
    xtdata.subscribe_quote(code, period=period, count=-1, callback=on_quote)

xtdata.run()
```
