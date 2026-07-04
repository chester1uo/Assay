# Assay Operator Compatibility Table
## Alpha 101 · qlib · Assay Native — 三方对照

> **设计原则**：Assay 的 AST 后端是唯一的执行引擎。Alpha 101 风格和 qlib 风格都是前端语法糖，经由各自的 Parser 转换成相同的 AST 节点后执行。

---

## 一、时序算子（Time-Series Operators）

| Alpha 101 / WQ 写法 | qlib 写法 | Assay 原生写法 | 备注 |
|---|---|---|---|
| `delay(x, d)` | `Ref(x, d)` | `ts_delay(x, d)` | 取 d 日前的值 |
| `delta(x, d)` | `Delta(x, d)` | `ts_delta(x, d)` | `x - delay(x, d)` |
| `returns` *(field)* | `$close / Ref($close,1) - 1` | `ts_returns(close, 1)` | Alpha101 用 `returns` 作内建字段；Assay 显式计算 |
| `correlation(x, y, d)` | `Corr(x, y, d)` | `ts_corr(x, y, d)` | 滚动 Pearson |
| `covariance(x, y, d)` | `Cov(x, y, d)` | `ts_cov(x, y, d)` | 滚动协方差 |
| `stddev(x, d)` | `Std(x, d)` | `ts_std(x, d)` | 滚动标准差 |
| `sum(x, d)` | `Sum(x, d)` | `ts_sum(x, d)` | 滚动加总 |
| `mean(x, d)` *(非标准，部分实现有)* | `Mean(x, d)` | `ts_mean(x, d)` | 滚动均值 |
| `product(x, d)` | `Product(x, d)` | `ts_product(x, d)` | 滚动连乘 |
| `ts_min(x, d)` | `Min(x, d)` | `ts_min(x, d)` | 滚动最小值 |
| `ts_max(x, d)` | `Max(x, d)` | `ts_max(x, d)` | 滚动最大值 |
| `ts_argmin(x, d)` | `IdxMin(x, d)` | `ts_argmin(x, d)` | 最小值位置（距今天数） |
| `ts_argmax(x, d)` | `IdxMax(x, d)` | `ts_argmax(x, d)` | 最大值位置 |
| `Ts_Rank(x, d)` | `Rank(x, d)` *(ts版)* | `ts_rank(x, d)` | 当前值在过去 d 日中的分位 [0,1] |
| `decay_linear(x, d)` | `WMA(x, d)` | `ts_decay_linear(x, d)` | 线性衰减加权均值，近期权重更大 |
| `EMA(x, d)` 或 `LinearDecay` | `EMA(x, d)` | `ts_ema(x, d)` | 指数移动均值 |
| *(无标准写法)* | `DEMA(x, d)` | `ts_dema(x, d)` | 双指数移动均值 |
| `Ts_ArgMax(x, d)` | `IdxMax(x, d)` | `ts_argmax(x, d)` | 同 ts_argmax，Alpha101 大写风格 |
| `Ts_ArgMin(x, d)` | `IdxMin(x, d)` | `ts_argmin(x, d)` | 同 ts_argmin |

---

## 二、截面算子（Cross-Sectional Operators）

| Alpha 101 / WQ 写法 | qlib 写法 | Assay 原生写法 | 备注 |
|---|---|---|---|
| `rank(x)` | `CSRank(x)` | `cs_rank(x)` | 截面百分位排名 [0,1] |
| `scale(x, a=1)` | `CSScale(x)` | `cs_scale(x, a=1)` | 截面绝对值归一到 a |
| `IndNeutralize(x, IndClass.sector)` | `IndNeutralize(x, level)` | `cs_neutralize(x, 'sector')` | 行业中性化，去除 sector beta |
| `IndNeutralize(x, IndClass.industry)` | `IndNeutralize(x, 'industry')` | `cs_neutralize(x, 'industry')` | 行业分类更细一级 |
| `IndNeutralize(x, IndClass.subindustry)` | `IndNeutralize(x, 'subindustry')` | `cs_neutralize(x, 'subindustry')` | 最细粒度行业中性化 |
| *(无标准写法)* | `CSZScore(x)` | `cs_zscore(x)` | 截面 z-score |
| *(无标准写法)* | `CSDemean(x)` | `cs_demean(x)` | 截面去均值 |
| *(无标准写法)* | *(无)* | `cs_winsorize(x, p)` | 截面 p%/1-p% 截尾 |
| *(无标准写法)* | *(无)* | `cs_group_rank(x, g)` | 组内排名（如行业内排名） |
| *(无标准写法)* | *(无)* | `cs_group_mean(x, g)` | 组内均值 |

---

## 三、数学 / 逐元素算子（Mathematical Operators）

| Alpha 101 / WQ 写法 | qlib 写法 | Assay 原生写法 | 备注 |
|---|---|---|---|
| `abs(x)` | `Abs(x)` | `abs(x)` | 绝对值 |
| `log(x)` | `Log(x)` | `log(x)` | 自然对数，x≤0 → NaN |
| `sign(x)` | `Sign(x)` | `sign(x)` | +1 / 0 / -1 |
| `SignedPower(x, e)` | `Power(x, e)` | `signed_power(x, e)` | `sign(x) * abs(x)^e`，保留符号的幂次 |
| `power(x, e)` *(部分实现)* | `Power(x, e)` | `pow(x, e)` | 普通幂次，注意 SignedPower ≠ power |
| `sqrt(x)` | `Sqrt(x)` | `sqrt(x)` | 平方根 |
| `min(x, y)` | `Min(x, y)` *(element-wise)* | `elem_min(x, y)` | 逐元素最小值（注意和 ts_min 区分）|
| `max(x, y)` | `Max(x, y)` *(element-wise)* | `elem_max(x, y)` | 逐元素最大值 |
| `(cond ? a : b)` | `If(cond, a, b)` | `where(cond, a, b)` | 三元条件选择 |
| *(无)* | `Clip(x, lo, hi)` | `clip(x, lo, hi)` | 截断到 [lo, hi] |
| *(无)* | *(无)* | `safe_div(a, b, fill=0)` | 除零安全，b=0 时返回 fill |
| *(无)* | *(无)* | `fillna(x, method)` | NaN 填充：ffill / zero / median |
| *(无)* | `Sigmoid(x)` | `sigmoid(x)` | 1/(1+e^-x) |

---

## 四、特殊字段 / 预计算量（Built-in Fields）

| Alpha 101 / WQ 写法 | qlib 写法 | Assay 原生写法 | 备注 |
|---|---|---|---|
| `open` | `$open` | `open` | 当日开盘价 |
| `high` | `$high` | `high` | 当日最高价 |
| `low` | `$low` | `low` | 当日最低价 |
| `close` | `$close` | `close` | 当日收盘价（复权） |
| `volume` | `$volume` | `volume` | 当日成交量 |
| `vwap` | `$vwap` | `vwap` | 当日成交量加权均价 |
| `returns` | `$close/$close[-1]-1` | `ts_returns(close, 1)` | Alpha101 作内建字段，Assay 显式计算 |
| `adv{d}` | `Mean($volume, d)` | `ts_mean(volume, d)` | d 日平均成交量，如 `adv20` = `ts_mean(volume, 20)` |
| `cap` *(市值)* | `$market_cap` | `market_cap` | 总市值 |

---

## 五、特殊语法兼容说明

### 5.1  `adv{d}` 的处理

Alpha 101 里大量使用 `adv20`、`adv5`、`adv81` 这类写法（d 日平均成交额）。Assay 的 Parser 会把它识别为宏展开：

```
adv20  →  ts_mean(volume, 20)
adv5   →  ts_mean(volume, 5)
adv81  →  ts_mean(volume, 81)
```

### 5.2  `SignedPower` vs `pow`

Alpha 101 里 `SignedPower(x, e) = sign(x) * abs(x)^e`，保留原始符号。这和普通的 `pow(x, e)` 不同（负数的偶数次幂会变正）。Assay 两者都支持，名字不同，不能混用：

```python
signed_power(x, 2)  # = sign(x) * x^2，保留符号
pow(x, 2)           # = x^2，负数变正
```

### 5.3  `Ts_Rank` vs `rank`

Alpha 101 里 `Ts_Rank(x, d)` 是时序 rank（当前值在过去 d 日的分位），`rank(x)` 是截面 rank（当前值在当日所有股票中的分位）。两者完全不同，Parser 根据参数数量自动区分：

```python
Ts_Rank(x, 20)  →  ts_rank(x, 20)   # 1个数据字段 + 1个窗口参数 → 时序
rank(x)         →  cs_rank(x)        # 只有1个数据字段，无窗口 → 截面
```

### 5.4  `IndNeutralize` 的 `IndClass` 参数

Alpha 101 里用 `IndClass.sector`、`IndClass.industry`、`IndClass.subindustry` 三级。Assay 把它映射成字符串参数：

```python
# Alpha 101 写法
IndNeutralize(vwap, IndClass.sector)

# Assay 等价写法
cs_neutralize(vwap, 'sector')
```

实际的行业分类数据来源在 DataStore 里配置（GICS、SIC、中信等），与算子本身解耦。

### 5.5  qlib `$` 前缀字段

qlib 用 `$close`、`$volume` 表示字段引用。Assay Parser 自动去掉 `$` 前缀并映射到 Assay 的字段名：

```python
$close  →  close
$vwap   →  vwap
$volume →  volume
```

### 5.6  中缀算子 `^` 与 `||`

论文把 `^`（幂）和 `||`（逻辑或）列为标准算子。Assay Parser 支持它们：

```python
(high * low)^0.5      →  pow(mul(high, low), 0.5)     # ^ 是普通幂，可负数底数→NaN
rank(x)^rank(y)       →  pow(cs_rank(x), cs_rank(y))  # 指数可以是表达式（矩阵）
(a < b) || (a == b)   →  or(lt(a,b), eq(a,b))         # 逻辑或，NaN 视为 false
```

注意 `^`（普通幂 `pow`）与 `SignedPower`（保号幂 `signed_power = sign(x)*abs(x)^a`）不同，见 5.2。

---

## 六、Alpha 101 覆盖情况验证

用上述算子集能实现 Alpha 101 全部 101 个因子。以下是几个典型例子验证对照表的完整性：

### Alpha#1
```
WQ:    rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5
Assay: cs_rank(ts_argmax(signed_power(where(ts_returns(close,1) < 0, ts_std(ts_returns(close,1), 20), close), 2), 5)) - 0.5
```

### Alpha#7
```
WQ:    (adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1 * 1)
Assay: where(ts_mean(volume,20) < volume, (-1 * ts_rank(abs(ts_delta(close,7)), 60)) * sign(ts_delta(close,7)), -1)
```

### Alpha#58  （窗口按论文 floor 取整：3.92795→3, 7.89291→7, 5.50322→5）
```
WQ:    -1 * Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndClass.sector), volume, 3.92795), 7.89291), 5.50322)
Assay: -1 * ts_rank(ts_decay_linear(ts_corr(cs_neutralize(vwap,'sector'), volume, 3), 7), 5)
```

### Alpha#98  （floor：4.58418→4, 20.8187→20, 8.62571→8, 6.95668→6）
```
WQ:    rank(decay_linear(correlation(vwap, sum(adv5, 26.4719), 4.58418), 7.18088)) -
       rank(decay_linear(Ts_Rank(Ts_ArgMin(correlation(rank(open), rank(adv15), 20.8187), 8.62571), 6.95668), 8.07206))
Assay: cs_rank(ts_decay_linear(ts_corr(vwap, ts_sum(ts_mean(volume,5),26), 4), 7)) -
       cs_rank(ts_decay_linear(ts_rank(ts_argmin(ts_corr(cs_rank(open), cs_rank(ts_mean(volume,15)), 20), 8), 6), 8))
```

---

## 七、不支持 / 有歧义的情况

| 情况 | 说明 | Assay 处理方式 |
|---|---|---|
| `adv{d}` 中 d 为浮点数（如 `adv5.85`） | Alpha101 原文有部分非整数窗口 | **向下取整 floor**（遵循论文 “non-integer d → floor(d)”），`adv5.85 → ts_mean(volume, 5)` |
| `decay_linear` / 任意 `ts_*` 的浮点窗口（如 `decay_linear(x, 7.89)`）| Alpha101 原文广泛使用 | **向下取整 floor**，`7.89 → 7` |
| `IndClass` 的具体分类与数据源绑定 | GICS vs SIC vs 中信一级 | 在 DataStore 配置层处理，算子层只传字符串 |
| Alpha101 原文的 `cap` 字段 | 部分实现包含市值 | Assay 作为 `market_cap` 字段，需 DataStore 提供 |
| Alpha101 某些 alpha 用 `open` 同时作为字段和函数名 | 歧义 | Parser 根据上下文（有无括号）区分：`open`=字段，`open(...)`=不存在的函数 |

---

## 八、实现状态（Implementation Status）

本对照表已在 `src/assay/engine/` 中实现并测试通过：

| 组件 | 文件 | 说明 |
|---|---|---|
| 统一 AST | [src/assay/engine/ast.py](../../../src/assay/engine/ast.py) | `FieldNode` / `LitNode` / `OpNode`，含 `struct_hash` |
| 算子注册表 + numpy kernel | [src/assay/engine/operators/](../../../src/assay/engine/operators/) | 按类别分文件（`time_series` / `cross_sectional` / `math_ops` / `arithmetic`）+ 共享 [`registry`](../../../src/assay/engine/operators/registry.py)；上表全部算子 + 机读 `OPERATOR_SCHEMA` |
| **两种前端 Parser** | [src/assay/engine/parsing.py](../../../src/assay/engine/parsing.py) | qlib 语法 与 函数式（Assay 原生 + Alpha 101/WQ 别名）共享一套文法 |
| 执行引擎 | [src/assay/engine/engine.py](../../../src/assay/engine/engine.py) | 面板 → `(T×N)` 矩阵 → 求值 |
| **Alpha 101 因子库** | [src/assay/factors/alpha101.py](../../../src/assay/factors/alpha101.py) | 论文（arXiv:1601.00991）全部 101 个因子，逐字录入为 Assay 表达式 |
| 测试 | [tests/engine/](../../../tests/engine/) · [tests/factors/test_alpha101.py](../../../tests/factors/test_alpha101.py) | 方言等价、算子数值、诊断、**101 因子全部解析+求值**、端到端 |

**两种语法（two types）** 经各自 Parser 转换为**同一个** AST 与算子后端，因此三列写法
（Alpha 101 · qlib · Assay 原生）只是前端语法糖：

```python
from assay.engine import parse
# qlib 与 函数式 产生完全相同的 AST
assert parse("Corr($close, $volume, 20)").struct_hash() \
    == parse("ts_corr(close, volume, 20)").struct_hash()
```

歧义消解（与上文一致）：`rank(x)`→截面 / `rank(x, d)`→时序；`min(x, 5)`→滚动 /
`min(x, y)`→逐元素；`SignedPower`（保号幂）≠ `^`/`pow`（普通幂）；浮点窗口（如 `7.89`）与
`adv5.85` 一律**向下取整 floor**（遵循论文）。行业中性化等组内算子需通过
`FactorEngine(panel, group_data=...)` 提供分类标签，Phase-1 数据层暂未内置 sector 数据，
缺失时会显式报错。

用 [src/assay/factors/alpha101.py](../../../src/assay/factors/alpha101.py) 评估论文因子：

```python
from assay.factors.alpha101 import ALPHA_101
from assay.engine import FactorEngine
eng = FactorEngine(panel, group_data={"sector": ..., "industry": ..., "subindustry": ...})
report = eng.evaluate(ALPHA_101[58])   # indneutralize 因子需要 group_data
```

> **adv{d} 口径**：论文定义为 d 日平均**成交额**（dollar volume）；Assay 按项目约定映射为
> `ts_mean(volume, d)`（平均**成交量**）。如需严格成交额口径，可改用 vwap×volume 字段。

### 自定义算子（Custom Operators）

用户可注册自己的算子，Parser 会自动识别已注册的名字，注册后即可在表达式中使用：

```python
import numpy as np
from assay.engine import operators as ops, FactorEngine

@ops.op("ts_zscore", 2, 2, category="custom", output_range="(-inf, inf)")
def ts_zscore(x, d):                       # x 是 (T, N) 矩阵，d 是字面量参数
    return (x - ops.ts_mean(x, d)) / ops.ts_std(x, d)

FactorEngine(panel).evaluate("cs_rank(ts_zscore(close, 20))")   # 直接可用
```

kernel 收到的数组参数是 `(T, N)` 矩阵、字面量参数是 python 标量。`needs_ctx=True` 时额外
收到求值上下文 `ctx`（组内算子用 `ctx.require_groups(name)` 取分类标签）。`register(...)`
是非装饰器形式，`unregister(name)` 注销，`operator_schema()` 返回含自定义算子的机读 schema。
内置 `ts_*` 的浮点窗口会在解析时 floor，自定义算子不会，故请传整数窗口。

---

*— Assay · Operator Compatibility Reference —*
