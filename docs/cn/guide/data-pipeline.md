# 数据管理

Assay 将 **MASSIVE** 美股数据集的**本地镜像**转换为点对点（PIT）的
parquet 存储。不会下载任何内容——数据管理从磁盘读取预先下载好的文件。
数据层是正确性的基石：每次读取都需要显式的 `as_of_date`，因此
前视偏差在结构上不可能发生。

## 本地数据源

`MASSIVE_DATA_DIR`（默认 `/data/massive_data`）是本地镜像的根目录，由
`downloader_*` 脚本在带外生成。预期的目录结构：

```
$MASSIVE_DATA_DIR/
├── us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet   # daily OHLCV bars
├── corporate_actions/splits/{TICKER}.jsonl                      # one split record per line
└── corporate_actions/dividends/{TICKER}.jsonl                   # one dividend record per line
```

每个日聚合 parquet 都带有 MASSIVE 平面文件列
`ticker, volume, open, close, high, low, window_start, transactions`（`window_start` 是
以 US/Eastern 计的交易日窗口起始的 Unix 纳秒时间）。

## 存储

位于 `ASSAY_DATA_DIR`（默认 `data/`）下：

```
<data_dir>/
├── price_raw/market=US/year=YYYY/month=MM/price_raw.parquet   # unadjusted OHLCV + transactions
├── adj_events/market=US/adj_events.parquet                    # splits & dividends (corp actions)
└── universe_snapshots/market=US/universe_snapshots.parquet    # PIT index membership
```

MASSIVE 日聚合数据源提供 **open/high/low/close/volume/transactions**——
**没有 `vwap`**，也不会导入 `market_cap`。

## 一键准备

```bash
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
```

按顺序运行三个阶段，写入配置的数据目录：

1. **universe**——NASDAQ-100 点对点（PIT）成分快照（无幸存者偏差的*并集*，
   即在该区间任何时点曾是成分股的每一个 ticker；由内置的 YAML 构建，
   不需要源文件）。
2. **corp-actions**——从本地 `corporate_actions/` JSONL 文件读取拆股与分红
   （尽力而为：此处的失败不再会中止价格传输）。
3. **prices**——来自本地日聚合 parquet 的每日 OHLCV，按股票池过滤，
   以月度分区写入。

标志：`--skip-universe`、`--skip-corp-actions`、`--skip-prices`。

## 数据管理（WebUI / API）

WebUI 的 **Data** 标签页（以及 `/v1/admin/*` API）无需 CLI 即可驱动整个数据层：

- **Keys & Dirs**——配置 MASSIVE S3 凭证（US）、Tushare token（CN）以及
  数据目录；**Test connection** 会列出你的 S3 key 能读取的数据集 / 校验 token。
  密钥以掩码形式存储在被 git 忽略的 `.assay.config.json` 中。
- **Data Setup**——按市场以 `mode` 运行任务：
  - `init`——完整历史（下载 + 导入）；
  - `update`——增量下载 + 导入（CN 按**交易日**抓取，每次调用返回所有代码，
    因此每日刷新只需少量 API 调用，然后追加到原始文件）；
  - `ingest`——**仅 RAW→ASSAY**，不下载（当原始镜像已就绪时）。
  - **自动更新计划**（按市场，每日一个时间）会自动排入 `update` 任务。
- **Data Status**——按市场的卡片，同时展示 **RAW**（数据源：最新日期、大小、目录）
  和 **ASSAY**（存储：最新日期、交易日数、大小、目录），以及落后天数与是否同步。

任务在后台逐个运行，带有实时进度条和日志。关于复权语义
（拆股/分红、点对点），参见 [数据与复权](data-and-adjustment.md)
（[English](../../en/guide/data-and-adjustment.md)）。

## 各个阶段

```bash
python -m assay.cli universe     --index NASDAQ100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli corp-actions --start 2025-01-01 --end 2026-06-09
python -m assay.cli prices       --start 2025-01-01 --end 2026-06-09
python -m assay.cli discover                         # show the local source layout (sanity check)
```

## 检查与验证

```bash
python -m assay.cli status                                       # row counts, date range, partitions
python -m assay.cli verify --start 2025-06-01 --end 2025-06-30 --adj split   # read a PIT panel
```

`--adj` 为 `none | split | total`（别名 `forward`）。复权仅使用截至查询日期
已知的公司行动。

## 分开的数据文件夹

`ASSAY_DATA_DIR` 选择当前生效的存储，因此你可以并列保留多个数据集，
加载器/引擎会自动建立一个全新的文件夹：

```bash
ASSAY_DATA_DIR=data_2025_2026h1 python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
ASSAY_DATA_DIR=data_2025_2026h1 python -m assay.cli serve-api          # engine uses that folder
```

在 `.env` 中永久设置（`ASSAY_DATA_DIR=data_2025_2026h1`）。匹配 `data*/` 的文件夹
会被 git 忽略。

## 已知的数据注意事项

- **没有 `adj_events` ⇒ 未复权拆股。** 如果跳过了 corp-actions 阶段，或本地
  `corporate_actions/` 目录树缺失，则该文件夹没有 `adj_events`，此时 `adj='split'`
  不产生任何作用，真实的拆股会表现为巨大的单日"收益"，污染因子/组合结果。
  回填：`python -m assay.cli corp-actions --start ... --end ...`。
- **`vwap` 不可用**——引用 `vwap`（以及 `market_cap`/`cap`）的因子表达式
  无法在已导入的数据上运行；Alpha-101 的 101 个因子中约有 52 个仅凭 OHLCV 即可运行。

数据层设计详见 [engineering.md](../design/engineering.md) §3。
