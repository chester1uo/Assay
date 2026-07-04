# CLI 参考

所有命令均以 `python -m assay.cli <subcommand>` 运行（在执行 `pip install -e .` 之后也可用
`assay <subcommand>` —— 控制台脚本为 `assay` 和 `assay-data`）。命令从环境 / `.env`
解析配置；只有触及数据的命令才需要 MASSIVE 凭证。

## 数据流水线

| 命令 | 用途 |
|---|---|
| `discover` | 列出 MASSIVE 平面文件（flat-file）前缀（连通性健全性检查） |
| `prepare-nasdaq100 --start S --end E` | 完整准备：股票池 → 公司行为 → 价格。标志：`--skip-universe/-corp-actions/-prices` |
| `universe --index NASDAQ100 --start S --end E` | 构建 `universe_snapshots` |
| `corp-actions --start S --end E [--symbols A,B]` | 获取拆股与分红 |
| `prices --start S --end E [--symbols A,B]` | 下载并归一化日聚合数据 |
| `status` | 显示已导入的行数、日期范围、分区 |
| `verify --start S --end E [--adj split]` | 读取一个 PIT 面板并打印摘要 |

## 因子引擎

| 命令 | 用途 |
|---|---|
| `parse '<expr>'` | 解析一个因子表达式 → 方言、AST、字段、算子（无需数据） |
| `eval '<expr>' --start S --end E [--as-of D] [--adj split] [--index NASDAQ100]` | 在 PIT 面板上评估因子矩阵；打印覆盖率 + 头部/尾部名称 |

## 回测与库（由 SDK 支撑）

| 命令 | 用途 |
|---|---|
| `run '<expr>' --start S --end E [...]` | 完整的打分 `FactorReport` 摘要（rank_ic、rank_icir、衰减、failure_mode、suggestion） |
| `batch <file|exprs...> --start S --end E [--output results.parquet]` | 批量评估（文件中每行一个表达式；`#` 注释被跳过），按 rank_icir 排序 |
| `report <factor_id>` | 漂亮地打印库中保存的一个因子 |
| `library list/get/delete/prune [...]` | 管理因子库（例如 `library list --sort rank_icir --limit 20`） |
| `portfolio '<expr>' --start S --end E [--rebalance monthly] [--weight-method signal_prop] [--market US] [--long-short] [--output pf.json]` | 运行一次组合回测；打印总/年化收益、夏普、最大回撤、换手率、成本拖累 |

## 服务器

| 命令 | 用途 |
|---|---|
| `serve-api [--host 0.0.0.0] [--port 8000]` | 运行 FastAPI REST API + WebUI（uvicorn） |
| `serve-mcp [--transport stdio|sse] [--port 8001]` | 为 LLM 智能体运行 MCP 服务器 |

## 示例

```bash
# prepare + inspect
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli status

# evaluate
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'
python -m assay.cli run   'cs_rank(ts_corr(close, volume, 20))' --start 2025-01-02 --end 2026-06-09

# batch from a file
printf 'ts_returns(close, 20)\nts_corr(close, volume, 20)\n' > factors.txt
python -m assay.cli batch factors.txt --start 2025-01-02 --end 2026-06-09 --output out.parquet

# portfolio
python -m assay.cli portfolio 'cs_rank(ts_returns(close, 20))' \
    --start 2025-01-02 --end 2026-06-09 --rebalance monthly --weight-method signal_prop

# serve
python -m assay.cli serve-api --port 8000        # http://localhost:8000
```

在任意子命令前加上 `-v` 可开启调试日志。
