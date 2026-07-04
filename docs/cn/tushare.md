# Tushare 数据源(中国 A 股 + 香港)

中国 A 股与香港股票的原始市场数据镜像,从 [Tushare Pro](https://tushare.pro/) HTTP API
下载。这是 `massive`(Polygon,美股)数据源的中国/香港对应物:它将**原始**
供应商数据落地到磁盘,Assay 的导入器随后可将其规范化为
标准的 `price_raw` / `adj_events` / `universe_snapshots` parquet 存储
(`market=CN` / `market=HK`)。

## 范围

| 市场 | 股票池 | 成分股历史 |
|--------|----------|--------------------|
| A 股 | **CSI300**(`000300.SH`)、**CSI500**(`000905.SH`)、**CSI1000**(`000852.SH`)成分股的并集 | ✅ 点对点、按月(`index_weight`) |
| 香港 | **HSI** + **恒生科技**成分股 | ⚠️ 仅当前快照(见限制说明) |

默认日期范围:**2010-01-01 → 今天**。

> **CSI300 成分股**由两个供应商代码拼接而成:`000300.SH` 仅从 2016-01 起
> 才带权重,因此深交所镜像 `399300.SZ` 补充了
> 2010-2015 的历史。二者合并(重叠时以标准代码为准)为
> 单个 `cn/index_weight/CSI300.parquet`。CSI500/CSI1000 各只需一个代码
>(CSI1000 的序列从其 2014-10 的成立日开始)。

## 运行

```bash
# from the repo root, with the project venv
TUSHARE_TOKEN=xxxx PYTHONPATH=src \
  /data/haoluo/_assay_venv/bin/python scripts/download_tushare.py
```

该任务**可续跑**:每个按标的 / 按指数的 parquet 若已存在则跳过,
因此重新运行同一命令会继续被中断的回填。
传入 `--force` 可覆盖。常用参数:

> **增量日度刷新。** 由于续跑会跳过已存在的按标的文件,这个
> 独立脚本只做*回填* — 它不会用新日期扩展某个标的的文件。
> 若要增量更新,请使用 WebUI 的 **Data → Update** 任务(或 `orchestrate.run(...,
> mode="update")`):它**按交易日**(每次 API 调用取所有标的)
> 经由 `run_cn_by_date` 拉取新窗口,并**追加**到原始文件 — 只需少量调用,而非每个
> 标的一次。参见 [数据管线 → 数据管理器](guide/data-pipeline.md)。

- `--markets cn,hk` — 选择市场(或用 `--steps` 精细控制)
- `--steps cn_universe,cn_prices,...` — 显式步骤列表(见 `ALL_STEPS`)
- `--start 20100101 --end 20260627` — 日期窗口
- `--rate 380 --workers 8` — API 调用/分钟预算与并发数
- `--limit-symbols N` — 限制股票池规模以快速冒烟测试

## 磁盘布局(`$TUSHARE_DATA_DIR`,默认 `/data/tushare_data`)

```
_manifest.json                       run config, row counts, failures, history
meta/
  stock_basic.parquet                A-share listings (L + D + P) — names, list/delist dates
  hk_basic.parquet                   HK listings
  trade_cal.parquet                  SSE trading calendar
cn/
  index_weight/CSI300.parquet        monthly constituent + weight history ...
  index_weight/CSI500.parquet
  index_weight/CSI1000.parquet
  universe.parquet                   deduped union (ts_code, indices, first_date, last_date)
  daily/{ts_code}.parquet            raw OHLCV (unadjusted): open/high/low/close/pre_close/vol/amount
  adj_factor/{ts_code}.parquet       split/merge adjustment factor (for qfq/hfq)
  daily_basic/{ts_code}.parquet      PE/PB/PS/turnover/total_mv/circ_mv/total_share/float_share/...
  dividend/{ts_code}.parquet         cash + stock dividends and split ratios
hk/
  index_global/HSI.parquet           index value series
  index_global/HSTECH.parquet
  constituents.parquet               current-snapshot HSI/HSTECH membership (ts_code, index, name)
  daily/{ts_code}.parquet            raw HK OHLCV (unadjusted)
logs/
  failures_*.txt                     per-step symbol failures (if any)
```

文件为 zstd 压缩的 parquet(项目约定)。各列是**原始
Tushare 字段**,未经修改,以保证保真度 — 复权/规范化在下游
Assay 导入层进行,而非在此处。

## 复权(拆股 / 分红 / 合并)

`cn/daily` 中的 A 股价格是**未复权的**。要构建前复权/后复权
序列,请将其与 `cn/adj_factor`(Tushare 的累积因子)结合,或从
`cn/dividend` 派生事件(拆股/送股用 `stk_div`、`stk_bo_rate`、`stk_co_rate`,
现金分红用 `cash_div`)。这与 Assay 的 `adj_events` 模型一致,
后者从原始事件重新计算累积因子,而非信任
供应商的累积数值。

## 已知限制

1. **HK 个股价格在此 token 下实际上无法下载。**
   `hk_daily` 接口被配额限制为每天几次调用(Tushare 返回代码
   40203 `频率超限 ... 5次/天`),因此批量拉取约 95 只 HK 成分股不
   可行 — `hk/daily` 留空,该步骤干净地中止。因此 HK 的
   交付物是**指数点位序列**(`hk/index_global`)加上
   **成分股表**。个股 HK 历史需要更高的 Tushare
   积分等级(或另一个 HK 数据源)。相关地,`hk_adjfactor` /
   `hk_daily_adj` 也**未授权**,因此任何 HK 价格无论如何都将是
   未复权的。

2. **HK 指数成分是当前快照,而非历史。** Tushare **没有**
   为 HSI 或恒生科技暴露任何指数成分接口
   (`index_member` / `index_basic market=HK` 均返回空)。因此 HK 股票池
   是内置于
   `src/assay/data/tushare/constituents.py` 的单个**当前**成分列表(捕获于 2026-06-27)。使用此列表对
   HK 股票池进行的回测存在**幸存者偏差**。若日后有历史数据源可用,
   请将这些列表替换为带日期的成分 —
   A 股路径(`index_weight`)已经是点对点正确的。

3. **恒生科技指数代码。** 在 `index_global` 上,恒生科技的点位
   序列是 `HKTECH`(而非 `HSTECH`,后者返回空)。友好名称
   `HSTECH` 用于磁盘文件和成分列表。
