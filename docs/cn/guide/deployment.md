# 部署 Assay

公开入口为 **https://assay.qfinverse.com**。REST 与 MCP 在同一个 API 进程中运行，共用鉴权与持久研究状态。

| 接口 | 公开地址 |
|---|---|
| 研究工作台 | `https://assay.qfinverse.com/` |
| Streamable HTTP MCP | `https://assay.qfinverse.com/mcp/` |
| 研究 REST | `https://assay.qfinverse.com/v1/research` |
| OpenAPI／交互式 API | `https://assay.qfinverse.com/openapi.json`／`/docs` |
| 连接发现 | `https://assay.qfinverse.com/.well-known/assay.json` |
| 文档 | `https://assay.qfinverse.com/doc/` |
| 存活／鉴权后的就绪检查 | `/health`／`/ready` |

连接发现文档是 Assay 自己的元数据，包含接口版本、实际工具数量／schema 摘要与鉴权方式，不是 OAuth 发现协议。远程客户端需要支持 Bearer 或 `X-API-Key`；仅支持 OAuth 服务端的客户端还需要额外适配。

## 安装与配置

部署验收使用 Python 3.12 和 `deploy/constraints.txt` 中的版本。在发布目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -c deploy/constraints.txt '.[api,mcp,perf]'
python scripts/build_doc_hub.py
```

文档构建应使用已安装 `markdown-it-py` 的开发解释器，也可直接发布已生成的静态文档；该包不是运行时依赖。

部署模板包括[环境配置](../../../deploy/assay.env.example)、[systemd 服务](../../../deploy/assay.service)和 [Caddy 代理](../../../deploy/Caddyfile)。模板假设使用独立的 `assay` 用户，代码与虚拟环境位于 `/opt/assay`，配置文件为 `/etc/assay/assay.env`，可写状态目录为 `/var/lib/assay`，只读的 US 行情位于 `/srv/assay/market/us`。启动前需创建目录并将运行状态目录交给该用户；按目标主机调整路径。

关键设置：

```dotenv
ASSAY_DEPLOYMENT=production
ASSAY_PUBLIC_URL=https://assay.qfinverse.com
ASSAY_MCP_HTTP=1
ASSAY_ACCOUNTS=1
ASSAY_CORS_ORIGINS=https://assay.qfinverse.com
ASSAY_DATA_DIR=/var/lib/assay
ASSAY_STATE_DIR=/var/lib/assay/accounts
ASSAY_CONFIG_FILE=/var/lib/assay/config.json
ASSAY_DATA_DIR_US=/srv/assay/market/us
```

生产模式要求 HTTPS 公开地址、有效的鉴权配置和可用的 MCP HTTP 模块；缺失时拒绝启动。`ASSAY_PUBLIC_URL` 只填写 origin，不附带路径、查询参数或凭证。发现文档与 OpenAPI 使用此地址，不会被请求中的代理 Host 替换。

全新安装启用账户后，访问 `/login`，使用 `ASSAY_STATE_DIR/setup_token` 中的一次性初始化 token 创建管理员。升级现有站点应保留账户库和签名 secret。研究员通过邀请加入，在 Account 页面创建个人 API key。Assay 服务端不需要 LLM key，外部 Agent 自行连接模型。

首版使用一个 API worker。研究 SQLite 状态持久化，临时行情 session 与缓存计数仍在进程内，数据更新调度器也按进程运行。MCP HTTP 的无状态传输不代表这些组件已分布式化。需要 FactorDB／ADES 时，还要配置可写的 registry／queue 目录、只读 QFV panel 和专用 worker key；基础模板不启用它们。

## 反向代理与传输

保留外部 `Host`、`Authorization`、`X-API-Key`、`Accept` 和 MCP 协议头。`/mcp/` 与评估流应关闭响应缓冲。Caddy 模板已立即刷新；nginx 应配置 `proxy_buffering off`、HTTP/1.1 和足够覆盖一次实验的读取超时。只信任实际代理来源的 forwarded headers；systemd 模板限定为 loopback。

客户端统一使用 `/mcp/`，`/mcp` 返回相对路径的 308 跳转。MCP 校验 Host／Origin，包括显式默认端口的等价形式；CORS 暴露 MCP session／protocol 响应头。旧 SSE 仅供本地使用，公开入口使用 Streamable HTTP，并由已安装的 SDK 协商协议版本。

## 发布验收

通过密钥管理或 shell 设置 `ASSAY_MCP_API_KEY` 后运行：

```bash
PYTHONPATH=src .venv/bin/python scripts/check_deployment.py \
  --base-url https://assay.qfinverse.com \
  --output artifacts/deployment-release/check.json
```

脚本读取 health、发现文档和 OpenAPI，验证无凭证请求被拒绝，再检查带凭证的 readiness、数据集读取，以及真实 MCP 初始化、工具发现、lint 和数据集列表。它不创建数据集、session 或实验，不调用模型，也不把 key 写入报告。未提供 key 时，结果为 incomplete，退出码为 2，不会判为验收通过。

`/health` 只检查进程存活。`/ready` 检查研究数据库能否进入写事务及 MCP 是否可用，需要与 REST 相同的凭证；它不证明行情覆盖充分。接入 mining job 前，应单独检查后端、目标时间窗口的数据／日历，并用一条获准的小因子实验验证实际执行。lint 不会测试因子计算和组合记账。

切换流量前运行回归测试，并用 MCP／REST 验证新建研究 session。保存 request ID，在重启后确认仍返回同一实验。`tests/api/test_deployment.py` 使用真实 TCP MCP 连接；已有研究传输测试覆盖重启、预算与迁移。

## 持久状态与部署范围

备份 `/var/lib/assay`，包括研究库、账户库、`auth_secret`、配置和因子库；缓存可重建。数据库使用 SQLite backup API 或停服后复制，直接复制正在写入的主库文件不能保证一致。升级前保留受保护的快照，跨 schema 回滚时配合旧版本恢复。

当前部署面向**可信团队的共享研究工作区**。账户控制入口与管理员权限，已认证研究员之间共享研究 session 和因子历史。需要机构间私有研究记录时，应使用独立实例或增加 ownership／authorization 层。

计算复用对只读行情／原子发布的要求见[性能指南](performance.md)。完整实验结果可能较大，Agent 日常读取应使用紧凑 context，以及实验／历史的分页接口。

连接示例：[MCP](mcp-server.md)、[参考 Agent](reference-agent.md)、[REST](rest-api.md)。
