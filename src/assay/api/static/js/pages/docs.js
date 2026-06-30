// pages/docs.js — in-app bilingual (EN / 简体中文) documentation.
//
// Content switches with the global language toggle: app.js re-renders the active
// route on every setLang(), so render() simply reads the current language and
// paints the matching content. Keep this page self-contained (no API calls).

import { getLang } from "../i18n.js";

const STYLE_ID = "docs-page-style";

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
.docs { max-width: 860px; }
.docs .doc-section { margin-bottom: var(--sp-6); }
.docs h2.section-title { margin-bottom: var(--sp-2); }
.docs p { color: var(--text); line-height: 1.7; margin: var(--sp-2) 0; }
.docs ul { margin: var(--sp-2) 0 var(--sp-2) var(--sp-5); line-height: 1.7; }
.docs li { margin: 2px 0; }
.docs code { font-family: var(--font-mono); font-size: 12.5px; background: var(--gray-1);
  border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; }
.docs pre { background: var(--gray-1); border: 1px solid var(--border); border-radius: var(--radius-card);
  padding: var(--sp-3); overflow-x: auto; margin: var(--sp-2) 0; }
.docs pre code { background: none; border: none; padding: 0; white-space: pre; }
.docs .doc-toc { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-5); }
.docs .doc-toc a { font-size: 12px; color: var(--text-muted); border: 1px solid var(--border);
  border-radius: var(--radius-badge); padding: 2px 10px; text-decoration: none; }
.docs .doc-toc a:hover { color: var(--text); border-color: var(--border-strong); }
`;
  document.head.appendChild(style);
}

// Each section: { id, title, blocks }. A block is one of:
//   ["p", text] | ["ul", [..items]] | ["code", text] | ["h3", text]
const CONTENT = {
  en: {
    pageTitle: "Documentation",
    pageSub: "How to use the Assay factor-research WebUI.",
    sections: [
      {
        id: "overview", title: "Overview",
        blocks: [
          ["p", "Assay is a high-performance factor backtesting engine for equities. The WebUI is a thin client over the REST API: write a factor expression, evaluate it point-in-time against an ingested price panel, and inspect its predictive quality (IC, decay, turnover) and how it relates to the rest of your factor library."],
          ["p", "Use the top bar to set the global Universe and Period, and the 中文/EN button to switch language. Your language and context persist across reloads."],
        ],
      },
      {
        id: "workspaces", title: "The three workspaces",
        blocks: [
          ["ul", [
            "Dashboard — system status, KPI cards, the factor leaderboard, top factors and a data-coverage calendar.",
            "Factor Library — browse, search, sort, compare and prune every saved factor; inspect a factor's full report and correlation matrix.",
            "Single Factor Test — write/lint a factor, evaluate it live, and read its IC time series, decay, quintile returns and diagnostics; save good ones to the library.",
          ]],
        ],
      },
      {
        id: "writing", title: "Writing factors",
        blocks: [
          ["p", "Two equivalent syntaxes parse to the same engine AST — use whichever you prefer:"],
          ["ul", [
            "qlib dialect — $-prefixed fields and CamelCase operators, e.g. Corr($close, $volume, 20).",
            "Assay-Python — bare fields and ts_*/cs_* names, e.g. ts_corr(close, volume, 20).",
          ]],
          ["p", "Examples:"],
          ["code", "cs_rank(ts_corr(close, volume, 20))\nts_mean(close, 5) / ts_mean(close, 60) - 1\nDiv($close, EMA($close, 200))"],
          ["p", "Fields: open, high, low, close, volume (vwap/market_cap are not available on the bundled US data). Use ⇄ Convert in the tester to translate between the two dialects in-browser."],
        ],
      },
      {
        id: "combination", title: "Factor Combination",
        blocks: [
          ["p", "The Factor Combination workspace blends several factors into one composite alpha and scores it honestly out-of-sample. Pick constituents (library ids, Alpha101/Alpha158 catalog numbers, or raw expressions — one per line), choose three date windows, and click Combine."],
          ["p", "The pipeline mirrors how production factor systems combine signals:"],
          ["ul", [
            "Standardize — each factor is z-scored or ranked cross-sectionally per day so different scales blend sanely.",
            "Orient — each factor is flipped to point at positive train IC (the sign is reported).",
            "Fit on TRAIN only — combination weights / a model are learned on the train window.",
            "Select on VALIDATION — with method 'auto', every candidate is fit on train and the best validation ICIR wins.",
            "Score on TEST — the frozen composite's IC / RankIC / ICIR is reported on the untouched test window.",
            "Embargo — the last few days of train/val are purged (default = max horizon) so overlapping forward labels never leak across a split.",
          ]],
          ["h3", "Methods (qlib-style)"],
          ["ul", [
            "Analytic / optimization (always available): equal, IC-weighted, ICIR-weighted, OLS, ridge, NNLS (non-negative, long-only optimization), and max-ICIR (Grinold's Σ⁻¹·IC̄ optimal blend).",
            "Linear models: Lasso, ElasticNet, plain linear.",
            "Tree ensembles: Random Forest, Extra Trees.",
            "Gradient boosting: scikit-learn GBRT / HistGBRT, LightGBM, XGBoost.",
            "Neural: a small MLP regressor.",
          ]],
          ["p", "Learned models predict the forward return from the oriented factors, so the per-factor numbers shown are feature importances rather than linear weights. Models appear in the dropdown only when their library (scikit-learn / lightgbm / xgboost) is installed; otherwise install it: pip install scikit-learn lightgbm xgboost."],
          ["p", "REST / SDK equivalent:"],
          ["code", "curl -X POST localhost:8000/v1/combination -H 'content-type: application/json' -d '{\n  \"factors\": [\"rank(close)\", \"alpha101:1\", \"lib:<id>\"],\n  \"train\": [\"2025-01-02\",\"2025-10-31\"],\n  \"val\":   [\"2025-11-01\",\"2026-01-31\"],\n  \"test\":  [\"2026-02-01\",\"2026-06-09\"],\n  \"universe\": \"NASDAQ100\", \"method\": \"auto\"\n}'\n# methods list: GET /v1/combination/methods"],
        ],
      },
      {
        id: "metrics", title: "Metrics glossary",
        blocks: [
          ["ul", [
            "IC — Pearson correlation between the factor and forward return, per day, averaged.",
            "RankIC — Spearman (rank) IC; robust to outliers and the primary quality metric.",
            "ICIR / RankICIR — IC divided by its volatility (information ratio of the signal); higher = more consistent.",
            "Decay half-life — how many days until predictive power halves; small = fast-decaying signal.",
            "Turnover — fraction of the position that changes between rebalances; high turnover means costs dominate.",
            "Redundancy — signed-rank similarity to the nearest library factor; high = the factor adds little new information.",
          ]],
        ],
      },
      {
        id: "cli", title: "CLI quickstart",
        blocks: [
          ["p", "Ingest data, then evaluate from the command line (run from source with PYTHONPATH=src, or after pip install -e .):"],
          ["code", "python -m assay.cli prepare-nasdaq100 --start 2016-06-24 --end 2026-06-09\npython -m assay.cli run 'cs_rank(ts_corr(close, volume, 20))' --start 2020-01-02 --end 2024-12-31\npython -m assay.cli serve-api   # REST API + this WebUI at http://localhost:8000"],
        ],
      },
      {
        id: "api", title: "API & SDK",
        blocks: [
          ["p", "Everything the WebUI does is a REST call under /v1 (interactive docs at /docs). For programmatic use, the Python SDK mirrors it:"],
          ["code", "import assay\nassay.init()\nreport = assay.backtest('cs_rank(ts_corr(close, volume, 20))',\n                        universe='NASDAQ100', period=('2020-01-01','2024-12-31'))\nprint(report.rank_ic, report.rank_icir)"],
        ],
      },
    ],
  },
  zh: {
    pageTitle: "文档",
    pageSub: "Assay 因子研究 WebUI 使用说明。",
    sections: [
      {
        id: "overview", title: "概览",
        blocks: [
          ["p", "Assay 是一个面向股票的高性能因子回测引擎。本 WebUI 是 REST API 之上的轻量客户端：编写因子表达式，对已导入的价格面板做点对点（PIT）评估，查看其预测质量（IC、衰减、换手率），以及它与因子库中其他因子的关系。"],
          ["p", "用顶部栏设置全局「股票池」和「区间」，用 中文/EN 按钮切换语言。语言与上下文会在刷新后保留。"],
        ],
      },
      {
        id: "workspaces", title: "三个工作区",
        blocks: [
          ["ul", [
            "仪表盘 —— 系统状态、KPI 卡片、因子排行榜、顶尖因子与数据覆盖日历。",
            "因子库 —— 浏览、搜索、排序、对比并剪枝所有已保存的因子；查看单个因子的完整报告与相关性矩阵。",
            "单因子测试 —— 编写/检查因子并实时评估，查看其 IC 时间序列、衰减、分位收益与诊断；将优质因子保存到库。",
          ]],
        ],
      },
      {
        id: "writing", title: "编写因子",
        blocks: [
          ["p", "两种等价语法解析为同一引擎 AST，任选其一："],
          ["ul", [
            "qlib 语法 —— 以 $ 前缀的字段 + 驼峰算子，例如 Corr($close, $volume, 20)。",
            "Assay-Python —— 裸字段 + ts_*/cs_* 名称，例如 ts_corr(close, volume, 20)。",
          ]],
          ["p", "示例："],
          ["code", "cs_rank(ts_corr(close, volume, 20))\nts_mean(close, 5) / ts_mean(close, 60) - 1\nDiv($close, EMA($close, 200))"],
          ["p", "可用字段：open、high、low、close、volume（捆绑的美股数据没有 vwap/market_cap）。在测试页用 ⇄ 转换 可在浏览器内于两种语法间互转。"],
        ],
      },
      {
        id: "combination", title: "因子合成",
        blocks: [
          ["p", "「因子合成」工作区把多个因子合成为一个复合因子,并做诚实的样本外评估。选择成分(因子库 id、Alpha101/Alpha158 编号,或直接写表达式,每行一个),设定三个日期区间,点「合成」。"],
          ["p", "流程与业界因子系统一致:"],
          ["ul", [
            "标准化 —— 每个因子按日做横截面 z-score 或排名,使不同量纲可以合理混合。",
            "定向 —— 把每个因子翻转到训练集 IC 为正的方向(方向会展示)。",
            "仅在「训练集」拟合 —— 合成权重 / 模型只在训练窗口学习。",
            "在「验证集」选择 —— method 选 auto 时,所有候选都在训练集拟合,取验证集 ICIR 最高者。",
            "在「测试集」评分 —— 冻结的复合因子在未被触碰的测试窗口报告 IC / RankIC / ICIR。",
            "隔离期 —— 训练/验证末尾若干天被剔除(默认=最大持有期),避免重叠的前瞻标签跨区间泄漏。",
          ]],
          ["h3", "合成方法(qlib 风格)"],
          ["ul", [
            "解析 / 优化(始终可用):等权、IC 加权、ICIR 加权、OLS、岭回归、NNLS(非负、长仓优化)、最大 ICIR(Grinold 的 Σ⁻¹·IC̄ 最优组合)。",
            "线性模型:Lasso、ElasticNet、普通线性。",
            "树集成:随机森林、极端随机树。",
            "梯度提升:scikit-learn GBRT / HistGBRT、LightGBM、XGBoost。",
            "神经网络:小型 MLP 回归。",
          ]],
          ["p", "学习类模型用定向后的因子预测未来收益,因此每个因子显示的是「特征重要度」而非线性权重。模型只有在其依赖库(scikit-learn / lightgbm / xgboost)已安装时才出现在下拉框中;否则请安装:pip install scikit-learn lightgbm xgboost。"],
          ["p", "REST / SDK 等价调用:"],
          ["code", "curl -X POST localhost:8000/v1/combination -H 'content-type: application/json' -d '{\n  \"factors\": [\"rank(close)\", \"alpha101:1\", \"lib:<id>\"],\n  \"train\": [\"2025-01-02\",\"2025-10-31\"],\n  \"val\":   [\"2025-11-01\",\"2026-01-31\"],\n  \"test\":  [\"2026-02-01\",\"2026-06-09\"],\n  \"universe\": \"NASDAQ100\", \"method\": \"auto\"\n}'\n# 方法列表: GET /v1/combination/methods"],
        ],
      },
      {
        id: "metrics", title: "指标词汇",
        blocks: [
          ["ul", [
            "IC —— 因子与前向收益的皮尔逊相关系数，逐日计算后取均值。",
            "RankIC —— 斯皮尔曼（秩）相关；对异常值稳健，是首要质量指标。",
            "ICIR / RankICIR —— IC 除以其波动（信号的信息比率）；越高越稳定。",
            "衰减半衰期 —— 预测力衰减到一半所需的天数；越小说明信号衰减越快。",
            "换手率 —— 两次再平衡之间仓位变动的比例；换手率高则交易成本占主导。",
            "冗余度 —— 与库中最相近因子的带符号秩相似度；越高说明该因子带来的新信息越少。",
          ]],
        ],
      },
      {
        id: "cli", title: "命令行快速开始",
        blocks: [
          ["p", "先导入数据，再从命令行评估（以 PYTHONPATH=src 从源码运行，或 pip install -e . 之后）："],
          ["code", "python -m assay.cli prepare-nasdaq100 --start 2016-06-24 --end 2026-06-09\npython -m assay.cli run 'cs_rank(ts_corr(close, volume, 20))' --start 2020-01-02 --end 2024-12-31\npython -m assay.cli serve-api   # REST API + 本 WebUI，地址 http://localhost:8000"],
        ],
      },
      {
        id: "api", title: "接口与 SDK",
        blocks: [
          ["p", "WebUI 的所有操作都是 /v1 下的 REST 调用（交互式文档在 /docs）。如需编程使用，Python SDK 与之对应："],
          ["code", "import assay\nassay.init()\nreport = assay.backtest('cs_rank(ts_corr(close, volume, 20))',\n                        universe='NASDAQ100', period=('2020-01-01','2024-12-31'))\nprint(report.rank_ic, report.rank_icir)"],
        ],
      },
    ],
  },
};

function renderBlock(el, block) {
  const [kind, payload] = block;
  if (kind === "p") return el("p", {}, payload);
  if (kind === "h3") return el("h3", { className: "section-title", style: { fontSize: "14px" } }, payload);
  if (kind === "code") return el("pre", {}, el("code", {}, payload));
  if (kind === "ul") return el("ul", {}, ...payload.map((it) => el("li", {}, it)));
  return el("p", {}, String(payload));
}

export function render(root, ctx) {
  injectStyle();
  const { el } = ctx;
  const lang = getLang();
  const doc = CONTENT[lang] || CONTENT.en;

  const toc = el(
    "nav",
    { className: "doc-toc", "aria-label": "Contents" },
    ...doc.sections.map((s) => el("a", { href: `#/docs` , onClick: (e) => {
      e.preventDefault();
      const node = document.getElementById(`doc-${s.id}`);
      if (node) node.scrollIntoView({ behavior: "smooth", block: "start" });
    } }, s.title))
  );

  const sections = doc.sections.map((s) =>
    el(
      "section",
      { className: "card doc-section", id: `doc-${s.id}` },
      el("h2", { className: "section-title" }, s.title),
      ...s.blocks.map((b) => renderBlock(el, b))
    )
  );

  const page = el(
    "div",
    { className: "docs" },
    el(
      "div",
      { className: "page-header" },
      el("h1", { className: "page-title" }, doc.pageTitle),
      el("span", { className: "page-subtitle" }, doc.pageSub)
    ),
    toc,
    ...sections
  );
  root.replaceChildren(page);
}
