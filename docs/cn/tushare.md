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
| 香港 | **全市场**(所有有过成交的标的),排名筛出 **HK100** | ✅ 点对点、按月(按成交额排名) |
| 香港 | **H 股**(内地注册的发行人),排名筛出 **HSHARE100** | ✅ 点对点、按月(ISIN `CNE` + 成交额) |
| 香港 | **HSI** + **恒生科技**成分股 | ⚠️ 仅当前快照(见限制说明) |

默认日期范围:**2010-01-01 → 今天**。

> **CSI300 成分股**由两个供应商代码拼接而成:`000300.SH` 仅从 2016-01 起
> 才带权重,因此深交所镜像 `399300.SZ` 补充了
> 2010-2015 的历史。二者合并(重叠时以标准代码为准)为
> 单个 `cn/index_weight/CSI300.parquet`。CSI500/CSI1000 各只需一个代码
>(CSI1000 的序列从其 2014-10 的成立日开始)。

## 接口地址与凭证

默认情况下,token 用于访问 `https://api.tushare.pro`。设置
`TUSHARE_API_URL`(或在 WebUI 的 **Data Settings → Tushare → API Endpoint**)可将
同样的调用路由到一个说同一套 `{api_name, params, fields}` 协议的中继/缓存网关。
网关通过 `X-API-Key` 请求头而非请求体中的 `token` 进行鉴权;客户端两种方式都会发送,
因此同一份配置对两类接口地址都适用。

这一点很重要,因为**HK 价格接口在普通 Tushare token 下没有权限** —
`hk_daily`、`hk_adjfactor` 和 `hk_daily_adj` 都需要一个有权限的接口地址。
没有它,只有 HK 指数点位序列可以取到。

## 运行

```bash
# from the repo root, with the project venv
TUSHARE_TOKEN=xxxx TUSHARE_API_URL=https://your-gateway/v1/tushare/query PYTHONPATH=src \
  /data/haoluo/_assay_venv/bin/python scripts/download_tushare.py --markets hk
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
- `--api-url URL` — 覆盖接口地址(默认取 `$TUSHARE_API_URL`)
- `--limit-symbols N` — 限制股票池规模以快速冒烟测试

请把 `--rate` 设为接口真实的每分钟上限。返回 HTTP 429 的网关就是在告诉你
预算设得太高了;客户端会等待 `Retry-After` 而不是指数退避,
但长期超预算的速率仍会损耗吞吐。

## 磁盘布局(`$TUSHARE_DATA_DIR`,默认 `/data/tushare_data`)

```
_manifest.json                       run config, row counts, failures, history
meta/
  stock_basic.parquet                A-share listings (L + D + P) — names, list/delist dates
  hk_basic.parquet                   HK listings (L + D — delisted included, no survivorship bias)
  trade_cal.parquet                  SSE trading calendar
  hk_trade_cal.parquet               HKEX trading calendar
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
  daily/{YYYY}/{YYYYMMDD}.parquet       raw HK OHLCV (unadjusted), whole market per date
  adj_factor/{YYYY}/{YYYYMMDD}.parquet  cum_adjfactor + close_price, whole market per date
  daily_adj/{YYYY}/{YYYYMMDD}.parquet   total_mv/free_mv/total_share/turnover_ratio per date
logs/
  failures_*.txt                     per-step symbol failures (if any)
```

文件为 zstd 压缩的 parquet(项目约定)。各列是**原始
Tushare 字段**,未经修改,以保证保真度 — 复权/规范化在下游
Assay 导入层进行,而非在此处。

### 为什么 CN 按标的分片而 HK 按日期分片

CN 的成分股是事先已知的(`index_weight`),因此下载器可以遍历一份标的列表。
HK **完全没有成分股接口**,也就没有可遍历的列表:
可交易的股票池只能从全市场的按日期响应中发现。
把这些响应原样存下来,代价是每个接口每个交易日一次 API 调用
(2010→今天约 4,100 天 x 3 = 约 12,300 次调用),换来的镜像
**在构造上就没有幸存者偏差** — 2014 年退市的标的,在其最后一个交易日之前的
每个文件里都仍然在。

这也让续跑和增量更新走同一条代码路径:已存在文件的日期会被跳过,
因此重新运行只拉取新增的尾部。

## 复权(拆股 / 分红 / 合并)

`cn/daily` 中的 A 股价格是**未复权的**。要构建前复权/后复权
序列,请将其与 `cn/adj_factor`(Tushare 的累积因子)结合,或从
`cn/dividend` 派生事件(拆股/送股用 `stk_div`、`stk_bo_rate`、`stk_co_rate`,
现金分红用 `cash_div`)。这与 Assay 的 `adj_events` 模型一致,
后者从原始事件重新计算累积因子,而非信任
供应商的累积数值。

## HK 公司行为

Tushare 对 HK **不提供任何分红或拆股记录** — 只有一个累积的
`hk_adjfactor`。因此导入层从它的跳变中重建事件:
当因子从 `f_prev` 变为 `f` 时,总的价格比率为
`r = f / f_prev`,该日期之前的价格全部乘以 `1 / r`。

有两种供应商数据的瑕疵会让对该因子的朴素解读出错,因此每一次跳变都会与
`hk_daily.pre_close` 交叉验证(在公司行为发生日,它会按行为后的口径重述),
两者相差超过 1% 的跳变会被丢弃:

* 该因子偶尔会在**没有任何公司行为**的日期上被**重新锚定**,
  否则就会凭空造出一笔两位数的分红;
* `pre_close` 自身在 1 港元以下的低价股上会相对前收盘**漂移一个跳动价位**,
  因此它只作为佐证,绝不用于确定幅度。

留存下来的跳变,在因子下降时、`hk/daily_adj` 的股本数发生实质性变动时,
**或该股本数不可得时**,归因为股本变动(`split_ratio = r`);只有股本数已知
且保持不变的跳变,才会成为一笔 `close_prev * (1 - 1/r)` 的现金分红。

默认归因为股本事件,是一种更安全的犯错方式。两种归因下全收益序列都是精确的,
但 `adj="split"` 是*默认*的读取口径(`AssayConfig.default_adj`),而把一次
2 拆 1 误读为分红,会把一半股价记成现金,并在仅含拆股的面板中留下未经处理的
除权缺口 — 届时每个因子都会读到一笔凭空捏造的 -50% 收益。反方向的错误则只是
让仅含拆股的视图多复权了一笔分红而已。导入器会记录一条警告,统计它无法根据
股本数据完成归因的跳变数量。

由于 `adj_events` 是一个只做 upsert 的存储 — 错误的事件无法被后续运行撤回,
只能通过删除该存储来清除 — 由此引出两条硬性前提:

* `hk/daily` **必须**存在,否则根本不会写入任何事件(没有 `pre_close` 就没有
  可供佐证的对象,而供应商的一次重新锚定就会变成一笔永久的两位数幻影分红);
* 不完整的 `hk/daily_adj` 只会削弱拆股与全收益的区分,但绝不会凭空造出一笔分红。

注意 `hk_daily_adj` 自带的 `close` / `adj_factor` 列*并不是*可用的
复权序列:它们在合股处无法正确衔接。请使用
`hk_adjfactor`,其 `close_price` 与 `hk_daily.close` 精确对得上。

合股通常是在该股**停牌**期间执行的,因此比率最大的那些跳变,恰恰就是没有
`hk_daily` K 线可以为其作证的跳变。这些跳变改为与因子数据源自身重述过的
`close_price` 相互印证,并且只有当比率极端到(任一方向 ≥2 倍)足以明确判定
为股本变动时才被接受 — 非交易日上一个接近 1 的比率无法与一次重新锚定区分开,
而漏掉一笔小额分红只是 2% 的误差,漏掉一次合股却是 40 倍的误差。

### 实测精度

以交易所自己的日收益率(`close / pre_close`)为基准,覆盖曾经进入过 HK100 的
487 只标的,2010-2026 年,共 145 万根 K 线:

| 偏差 | K 线数 | 占比 |
|---|---|---|
| > 0.1% | 1,892 | 0.13% |
| > 1% | 1,117 | 0.077% |
| > 20% | 23 | 0.0016% |

若只看最近一个月内成交最活跃的 300 只标的,则为 **0 处不符**(最大 6e-05)。

最差的那 23 根 K 线大致均分于两个原因,两者都源自供应商数据本身,
而非重建过程:

* **`pre_close` 本身就是错的**,出现在一些交投清淡的日子(例如 `01063.HK`
  对一只成交价 0.22 港元的股票报出了 0.01 港元的前收盘价)。此时重建出的
  序列才是正确的那一个,而*基准*才是坏数据。
* **`cum_adjfactor` 漏掉了一次 `pre_close` 确实记录到的公司行为** — 尤其是
  分拆(例如 `00004.HK` 在 2017 年 11 月的分拆)。由于事件是从因子跳变中提取的,
  因子数据源里没有的公司行为,在 `adj_events` 里同样没有,于是留下了未经处理的
  除权缺口。同时从 `pre_close` 的重述中提取事件可以补上这一块,
  这也是显而易见的下一步改进。

这两类残差对一个做过排序的因子都不构成实质影响(默认的 `signal_transform`
就是 `rank`),但对任何直接消费原始收益率的东西都会有影响。

## 已知限制

1. **HK 指数成分是当前快照,而非历史。** Tushare **没有**
   为 HSI 或恒生科技暴露任何指数成分接口
   (`index_member` / `index_basic market=HK` 均返回空)。因此这两个股票池
   是内置于 `src/assay/data/tushare/constituents.py` 的单个**当前**成分列表
   (捕获于 2026-06-27),基于它们的回测存在**幸存者偏差**。
   建议优先使用 **HK100**,它由导入层根据实际成交额点对点地推导得出(参见
   [多市场](multi_market.md))。

2. **没有 HK 行业分类。** `hk_basic` 不带行业字段,Tushare 也没有
   A 股行业分类的 HK 对应接口,因此 `market=HK` 不会写出
   `security_groups` 存储。行业中性化在那里是诚实地不可用,
   而不是悄悄变成一个空操作。

3. **恒生科技指数代码。** 在 `index_global` 上,恒生科技的点位
   序列是 `HKTECH`(而非 `HSTECH`,后者返回空)。友好名称
   `HSTECH` 用于磁盘文件和成分列表。

4. **HK 价格只有港元计价。** 不存在汇率账簿,因此 HK 回测的净值以
   港元计,不经 Assay 之外的换算无法与美股或 A 股的运行结果对比。
