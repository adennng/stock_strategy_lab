# Signal Strategy Library

本文件是 SignalAgent 生成策略时的精简模板库。目标是提供可落地的搜索空间，不鼓励一次性写过度复杂策略。

## 统一结构

```text
SignalStrategy =
    RegimeDetector 市场状态识别
  + RegimeAlphaPolicy 分状态多周期 Alpha
  + Filters 辅助过滤器
  + ExitPolicy 出场与风控
  + PositionMapper 仓位映射
  + StateRules 状态与交易纪律
```

输出必须是 `target_S ∈ [0, 1]`。

## 默认框架：RegimeSwitchingAlpha + MultiTimeframeAlpha

信号层默认优先使用 `RegimeSwitchingAlpha + MultiTimeframeAlpha`。它不是新的外部接口，而是 `RegimeAlphaPolicy` 的推荐实现方式。

```text
RegimeDetector:
  先判断当前市场状态，例如 uptrend / range / downtrend / high_vol。

RegimeAlphaPolicy:
  根据当前状态选择对应 Alpha。

MultiTimeframeAlpha:
  每个 Alpha 内部使用长 / 中 / 短三个周期窗口共同打分。

PositionMapper:
  把最终 alpha_score 映射为 target_S。
```

推荐约束：

```text
每个策略只允许 1 个 RegimeDetector。
核心 regime 建议 3 个，最多 4 个。
每个核心 regime 必须在 RegimeAlphaMap 中显式绑定 1 个 Alpha。
不同核心 regime 应使用不同 Alpha 逻辑。
每个 Alpha 最多使用 3 个周期窗口。
所有 Alpha 必须输出 0 到 1 的 alpha_score。
最终只允许输出一个 target_S。
```

## RegimeAlphaMap 强制要求

每个策略必须显式定义 `RegimeAlphaMap`。它说明“哪个市场状态调用哪个 Alpha”，是复盘和后续优化的核心材料。

示例：

```text
RegimeAlphaMap:
  uptrend -> alpha_uptrend_trend_momentum
  range -> alpha_range_mean_reversion
  downtrend -> alpha_downtrend_flat
  high_vol -> alpha_high_vol_defensive
```

硬性规则：

```text
1. 不允许所有 regime 共用同一个 alpha_score 后只调整仓位。
2. 不允许只用 regime 调整 PositionMapper，而 Alpha 逻辑完全相同。
3. 允许 downtrend / high_vol 的 Alpha 是 defensive / flat，但必须显式声明。
4. 如果某个 regime 不交易，也要写成 alpha_downtrend_flat 或 alpha_high_vol_defensive。
5. strategy_spec.md 和 strategy_meta.json 必须记录 RegimeAlphaMap。
```

推荐代码结构：

```python
REGIME_ALPHA_MAP = {
    "uptrend": "alpha_uptrend_trend_momentum",
    "range": "alpha_range_mean_reversion",
    "downtrend": "alpha_downtrend_flat",
    "high_vol": "alpha_high_vol_defensive",
}

def detect_regime(...):
    ...

def alpha_uptrend_trend_momentum(...):
    ...

def alpha_range_mean_reversion(...):
    ...

def alpha_downtrend_flat(...):
    ...

def alpha_high_vol_defensive(...):
    ...

def map_position(alpha_score, regime, ...):
    ...

def apply_state_rules(target_s, current_position_in_budget, ...):
    ...
```

当复盘显示某个 regime 表现差时，优先考虑替换该 regime 的 Alpha 或调整 RegimeDetector，而不是只调全局参数。

日频数据下的“跨周期”不要求分钟线或周线，可以用不同日线窗口模拟：

```text
short_window: 5~20 日，用于入场时机、过热、超跌、短期确认。
mid_window: 20~80 日，用于主信号、动量、区间判断。
long_window: 80~252 日，用于长期方向、趋势保护、风险状态。
```

典型结构：

```text
uptrend:
  long_window 判断长期方向。
  mid_window 判断趋势强度或突破。
  short_window 控制追高、回调再启动或短期确认。

range:
  long_window 确认没有明显下跌趋势。
  mid_window 判断区间或布林带位置。
  short_window 判断 RSI / ZScore 超买超卖。

downtrend:
  默认 target_S = 0，或只允许极低仓位防御型 Alpha。

high_vol:
  优先降仓、延迟入场、要求更多确认。
```

Regime 切换必须有防抖规则，避免每天来回切换：

```text
confirm_days: 2~5
min_hold_days: 2~10
max_daily_target_change: 0.2~0.5
cooldown_days: 0~10
```

如果策略已经很复杂，不要再堆叠过多 Filters。优先保证 RegimeDetector、Alpha 和 PositionMapper 清楚可解释。

## Alpha 主信号模板

### A1 单均线趋势

适用：趋势较清晰、噪音较低的宽基指数、债券、黄金、红利类资产。

参数：

```text
ma_type: SMA/EMA
n: 20~252
confirm_days: 1~5
band: 0%~3%
```

输出：

```text
close > MA(n) 时 raw_score = 1，否则 0。
```

### A2 双/三均线趋势

适用：趋势型 ETF、成长类指数、行业主升浪。

参数：

```text
fast: 3~60
mid: 10~120
slow: 20~252
min_gap: 0%~3%
```

### A3 MACD / PPO 动能

适用：趋势启动确认。

参数：

```text
fast: 5~20
slow: 15~60
signal: 5~20
hist_slope_window: 2~10
```

### A4 线性回归斜率

适用：趋势质量判断，减少均线滞后。

参数：

```text
window: 20~120
slope_threshold: 0~0.2
r2_min: 0.05~0.6
```

### A5 Donchian / 海龟突破

适用：趋势爆发、行业主升浪、黄金、有色等强趋势资产。

参数：

```text
entry_n: 10~120
exit_m: 5~60
atr_n: 10~60
stop_k: 1.0~5.0
cooldown: 0~20
```

### B1 绝对动量

适用：中长期趋势判断、宏观资产 ETF、宽基指数。

参数：

```text
lookback_n: 20~252
threshold: -3%~10%
skip_recent_k: 0~20
```

### B2 多窗口动量

适用：不同周期趋势共振。

参数：

```text
windows: [20, 60, 120] 或 [10, 20, 60]
weights: 0~1 后归一化
enter_threshold: 0.55~0.85
exit_threshold: 0.25~0.65
```

### B3 风险调整动量

适用：波动差异明显的资产。

参数：

```text
ret_n: 20~120
vol_n: 20~120
vol_floor: 0.5%~2%
penalty_power: 0.5~2.0
```

### C1 RSI 反转

适用：震荡市、宽幅波动、长期趋势未恶化的指数。

必须配趋势保护或最大仓位限制。

参数：

```text
rsi_n: 2~30
oversold: 10~45
overbought: 55~90
recover_threshold: 25~55
max_s: 0.2~0.8
```

### C2 Bollinger 回归

适用：区间震荡、波动收敛后的反弹。

必须配止损。

参数：

```text
bb_n: 10~120
bb_k: 1.0~3.5
stop_k: 1~3 ATR
max_s: 0.3~0.8
```

### C3 ZScore / BIAS 反转

适用：均值稳定、波动规律较强的资产。

参数：

```text
ma_n: 20~120
z_entry: -1.0~-3.0
z_exit: -0.5~0.5
max_s: 0.3~0.8
```

## Filters 模板

```text
ADX 趋势强度过滤:
  adx_n: 7~30
  adx_threshold: 18~35

大周期方向过滤:
  long_ma: 60~252
  slope_window: 20~120

波动率过滤:
  natr_n: 10~60
  high_vol_percentile: 60%~95%

成交量过滤:
  volume_ma_n: 5~60
  volume_ratio: 0.8~2.5

趋势保护:
  均值回归策略只能在 close > MA120 或 MA120 slope >= 0 时开仓。

异常 K 线冷却:
  gap_threshold: 1%~5%
  cooldown_days: 1~10
```

## ExitPolicy 模板

```text
反向信号退出:
  confirm_days: 1~5

通道退出:
  exit_m: 5~60

ATR 止损:
  atr_n: 10~60
  stop_k: 1.0~5.0

跟踪止损:
  trail_k: 1.5~6.0 ATR
  lookback_high: 5~60

利润锁定:
  profit_trigger: 3%~20%
  reduce_ratio: 0.2~0.7

时间止损:
  max_hold_days: 5~60
  min_return: -2%~5%
```

## PositionMapper 模板

```text
二元映射:
  S_on: 0.5~1.0
  S_off: 0~0.2

分段映射:
  threshold_low: 0.3~0.6
  threshold_high: 0.6~0.9
  S_low: 0.2~0.5
  S_mid: 0.5~0.8
  S_high: 0.8~1.0

连续映射:
  temperature: 0.5~5
  center: 0.3~0.7
  S_min: 0~0.3
  S_max: 0.5~1.0

波动率打折:
  natr_n: 10~60
  penalty: 0.5~3.0
  min_discount: 0.2~1.0

左侧小仓位:
  max_left_s: 0.2~0.5
  confirm_s: 0.4~0.8

迟滞阈值:
  enter_threshold: 0.55~0.85
  exit_threshold: 0.25~0.65，必须小于入场阈值。
```

## 推荐组合（仅供参考）

### 模板 1：海龟增强趋势

```text
Alpha: Donchian N 日突破
Filters: ADX + 大周期方向 + 成交量确认
Exit: M 日低点退出 + ATR 止损
Mapper: 初始仓位 + 分段加仓
StateRules: 止损后 cooldown
```

### 模板 2：趋势回调再启动

```text
Alpha: 长期均线向上，短期回调后重新站上中期均线
Filters: 成交量恢复 + 大周期方向
Exit: 跌破中期均线或 ATR 跟踪止损
Mapper: 初始 0.5，突破前高后提高到 1.0
```

### 模板 3：Bollinger / RSI 均值回归

```text
Alpha: 触及布林下轨且 RSI 超卖后上穿恢复阈值
Filters: 大周期不能明显下跌 + 极端高波禁入
Exit: 回到中轨减仓 + 触上轨清仓 + ATR 止损
Mapper: 左侧小仓位，最大 S 受趋势状态限制
```

### 模板 4：多窗口动量

```text
Alpha: 20/60/120 日动量加权
Filters: close > MA120 + 波动率不过高
Exit: 综合动量转负或回撤超过阈值
Mapper: 分段或连续映射
```

### 模板 5：RegimeSwitching 多周期状态切换

```text
Regime:
  ADX 高 + 斜率向上 -> 趋势模板
  ADX 低 + 价格在区间内 -> 均值回归模板
  高波急跌 -> 防守或空仓
Exit: 独立 ATR 止损
Mapper: 每个状态使用不同最大 S
```

这是当前推荐的标准高级框架。第一轮也可以使用模板 5，但要控制复杂度：最多 3 个核心 regime、每个 regime 一个 Alpha、每个 Alpha 最多 3 个周期窗口。

## 第一轮候选方向选择

第一次探索某个指数时，应根据 market_profile 自主选择最多 4 个 attempt，可以少于 4 个。下面是候选方向菜单，仅供参考，不是必选项；不要机械固定生成同一组策略。

```text
趋势主导 RegimeAlpha：uptrend 用均线/动量，range 降仓，downtrend 空仓。
突破主导 RegimeAlpha：uptrend 用 Donchian/通道突破，high_vol 降仓。
震荡回归 RegimeAlpha：range 用 RSI/Bollinger/ZScore，downtrend 禁入。
防御稳健 RegimeAlpha：强调长期方向、波动率打折和缓慢仓位变化。
回调再启动 RegimeAlpha：趋势中等待短期回调修复后入场。
波动率收缩突破 RegimeAlpha：低波收敛后突破才提高仓位。
高波动防御 RegimeAlpha：high_vol 下优先降仓，只保留极少量确认信号。
低频确认 RegimeAlpha：减少交易次数，用长周期确认主方向。
短反弹捕捉 RegimeAlpha：下跌或震荡环境中只捕捉短期修复。
空仓优先 / 机会过滤 RegimeAlpha：默认不交易，只在高置信状态短暂参与。
```

选择规则：

```text
1. 强趋势、回撤可控：优先趋势、突破、回调再启动、低频确认。
2. 长期震荡、均值稳定：优先震荡回归、短反弹捕捉、防御稳健。
3. 长期下行、偶有急涨：优先空仓机会过滤、短反弹捕捉、防御稳健；少生成纯趋势策略。
4. 高波动、阶段切换频繁：优先高波动防御、低频确认、波动率收缩突破。
5. 如果画像不支持某类方向，不要为了凑数生成。
6. 允许多个候选属于同一大类，但 RegimeAlphaMap、窗口、入场逻辑、退出逻辑或风控结构必须有实质差异。
```

每个 attempt 都应采用 RegimeSwitchingAlpha + MultiTimeframeAlpha，但第一轮要控制复杂度，先找对状态划分和 Alpha 方向，再进入结构增强。

## 指标计算参考

策略脚本运行在 `attempt-evaluation` 的回测进程里，输入是截至当前 K 线的 `history` DataFrame。常见字段：

```text
datetime
open
high
low
close
volume
```

推荐优先使用 `pandas` 实现简单指标；复杂技术指标可使用 TA-Lib。Backtrader 当前作为回测引擎使用，生成的 `strategy.py` 不建议直接依赖 Backtrader indicator 对象，因为信号策略接口是 `suggest(history, current_position_in_budget)`，输入是 pandas DataFrame，不是 Backtrader data feed。

### 通用写法建议

```python
import numpy as np
import pandas as pd

try:
    import talib
except Exception:
    talib = None
```

```python
close = pd.to_numeric(history["close"], errors="coerce").dropna()
high = pd.to_numeric(history["high"], errors="coerce").reindex(close.index)
low = pd.to_numeric(history["low"], errors="coerce").reindex(close.index)
volume = pd.to_numeric(history.get("volume", 0), errors="coerce")
```

规则：

```text
1. 只使用 history，不读取未来数据。
2. 指标计算前检查数据长度，样本不足时返回当前仓位或 0。
3. TA-Lib 计算失败时可回退到 pandas 实现。
4. 输出最终 target_S 前必须 clamp 到 [0, 1]。
```

### 均线 / 趋势

SMA：

```python
sma = talib.SMA(close.to_numpy(dtype=float), timeperiod=n)[-1] if talib else close.rolling(n).mean().iloc[-1]
```

EMA：

```python
ema = talib.EMA(close.to_numpy(dtype=float), timeperiod=n)[-1] if talib else close.ewm(span=n, adjust=False).mean().iloc[-1]
```

KAMA：

```python
kama = talib.KAMA(close.to_numpy(dtype=float), timeperiod=n)[-1] if talib else close.ewm(span=n, adjust=False).mean().iloc[-1]
```

均线斜率：

```python
ma = close.rolling(n).mean()
slope = ma.diff(slope_window).iloc[-1] / max(abs(ma.iloc[-slope_window - 1]), 1e-12)
```

### 动量 / 收益

绝对动量：

```python
momentum = close.iloc[-1] / close.iloc[-lookback_n] - 1.0
```

跳过最近 K 天的动量：

```python
momentum = close.iloc[-1 - skip_recent_k] / close.iloc[-lookback_n - skip_recent_k] - 1.0
```

ROC / MOM：

```python
roc = talib.ROC(close.to_numpy(dtype=float), timeperiod=n)[-1] / 100.0 if talib else close.pct_change(n).iloc[-1]
mom = talib.MOM(close.to_numpy(dtype=float), timeperiod=n)[-1] if talib else close.diff(n).iloc[-1]
```

多窗口动量：

```python
score = 0.0
for window, weight in zip(windows, weights):
    score += weight * (close.iloc[-1] / close.iloc[-window] - 1.0)
```

风险调整动量：

```python
ret = close.iloc[-1] / close.iloc[-ret_n] - 1.0
vol = close.pct_change().rolling(vol_n).std().iloc[-1]
score = ret / max(vol, vol_floor) ** penalty_power
```

### MACD / PPO

MACD：

```python
if talib:
    macd, macd_signal, macd_hist = talib.MACD(close.to_numpy(dtype=float), fastperiod=fast, slowperiod=slow, signalperiod=signal)
    hist = macd_hist[-1]
else:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
    hist = (macd_line - macd_signal).iloc[-1]
```

PPO：

```python
ppo = talib.PPO(close.to_numpy(dtype=float), fastperiod=fast, slowperiod=slow, matype=0)[-1] if talib else ((ema_fast / ema_slow - 1.0) * 100).iloc[-1]
```

### 线性回归斜率 / R2

TA-Lib 斜率：

```python
slope = talib.LINEARREG_SLOPE(close.to_numpy(dtype=float), timeperiod=window)[-1] if talib else None
```

pandas / numpy 斜率与 R2：

```python
y = close.iloc[-window:].to_numpy(dtype=float)
x = np.arange(len(y), dtype=float)
coef = np.polyfit(x, y, 1)
fit = coef[0] * x + coef[1]
slope = coef[0] / max(abs(y[-1]), 1e-12)
ss_res = float(((y - fit) ** 2).sum())
ss_tot = float(((y - y.mean()) ** 2).sum())
r2 = 1.0 - ss_res / max(ss_tot, 1e-12)
```

### Donchian / 通道

Donchian 入场通道：

```python
entry_high = high.rolling(entry_n).max().iloc[-2]
breakout = close.iloc[-1] > entry_high * (1.0 + breakout_band)
```

通道退出：

```python
exit_low = low.rolling(exit_m).min().iloc[-2]
exit_signal = close.iloc[-1] < exit_low
```

注意使用 `iloc[-2]` 避免把当前 K 线高低点直接用于当前突破阈值。

### RSI / Bollinger / ZScore / BIAS

RSI：

```python
rsi = talib.RSI(close.to_numpy(dtype=float), timeperiod=rsi_n)[-1] if talib else None
```

pandas RSI fallback：

```python
delta = close.diff()
gain = delta.clip(lower=0).rolling(rsi_n).mean()
loss = (-delta.clip(upper=0)).rolling(rsi_n).mean()
rsi = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1]
```

Bollinger：

```python
if talib:
    upper, mid, lower = talib.BBANDS(close.to_numpy(dtype=float), timeperiod=bb_n, nbdevup=bb_k, nbdevdn=bb_k)
    upper_v, mid_v, lower_v = upper[-1], mid[-1], lower[-1]
else:
    mid_s = close.rolling(bb_n).mean()
    std_s = close.rolling(bb_n).std()
    mid_v = mid_s.iloc[-1]
    upper_v = mid_v + bb_k * std_s.iloc[-1]
    lower_v = mid_v - bb_k * std_s.iloc[-1]
```

ZScore：

```python
mean = close.rolling(ma_n).mean().iloc[-1]
std = close.rolling(ma_n).std().iloc[-1]
z = (close.iloc[-1] - mean) / max(std, 1e-12)
```

BIAS：

```python
ma = close.rolling(ma_n).mean().iloc[-1]
bias = close.iloc[-1] / max(ma, 1e-12) - 1.0
```

### ATR / NATR / ADX / 波动率

ATR：

```python
if talib:
    atr = talib.ATR(high.to_numpy(dtype=float), low.to_numpy(dtype=float), close.to_numpy(dtype=float), timeperiod=atr_n)[-1]
else:
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_n).mean().iloc[-1]
```

NATR：

```python
natr = talib.NATR(high.to_numpy(dtype=float), low.to_numpy(dtype=float), close.to_numpy(dtype=float), timeperiod=natr_n)[-1] / 100.0 if talib else atr / max(close.iloc[-1], 1e-12)
```

ADX：

```python
adx = talib.ADX(high.to_numpy(dtype=float), low.to_numpy(dtype=float), close.to_numpy(dtype=float), timeperiod=adx_n)[-1] if talib else None
```

实现波动率：

```python
realized_vol = close.pct_change().rolling(vol_n).std().iloc[-1]
annual_vol = realized_vol * np.sqrt(252)
```

### 成交量过滤

成交量均线比：

```python
vol_ma = volume.rolling(volume_ma_n).mean().iloc[-1]
volume_ratio = volume.iloc[-1] / max(vol_ma, 1e-12)
```

量能恢复：

```python
volume_recover = volume_ratio > threshold
```

### StateRules / 交易纪律

由于 `suggest()` 当前只接收 `history` 和 `current_position_in_budget`，复杂状态规则应尽量用历史信号可重算逻辑或价格状态近似，不要依赖外部文件。

cooldown 可用最近异常 K 线或最近突破失败事件近似：

```python
recent_gap = close.pct_change().abs().iloc[-cooldown_days:].max()
in_cooldown = recent_gap > gap_threshold
```

min_hold_days 如果需要严格持仓天数，需要策略类维护内存状态。但回测时策略实例是连续运行的，可以维护简单字段：

```python
def __init__(self, params=None):
    super().__init__(params)
    self.hold_days = 0

def suggest(...):
    if current_position_in_budget > 0:
        self.hold_days += 1
    else:
        self.hold_days = 0
```

注意：状态变量会影响回测路径，逻辑必须简单、可解释，不得读取未来数据。

### Backtrader 使用边界

本项目已经用 Backtrader 作为底层回测引擎，SignalAgent 生成的 `strategy.py` 不需要也不应该继承 `bt.Strategy`。

原因：

```text
1. 生成策略的统一接口是 BaseSignalStrategy.suggest(history, current_position_in_budget)。
2. 回测服务会把该信号策略包进内部 Backtrader 策略执行。
3. 如果生成的 strategy.py 直接写 bt.Strategy，会与 attempt-evaluation 的加载接口不兼容。
```

Backtrader 可作为概念参考，例如：

```text
SMA / EMA / RSI / MACD / ATR / BollingerBands / Highest / Lowest / CrossOver
```

但在生成的 `strategy.py` 中，应使用 pandas 或 TA-Lib 计算这些指标。
