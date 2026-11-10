# 美股收盘后日报与外部读取

本说明适用于 GitHub Actions 的 .github/workflows/00-daily-analysis.yml。分析和依赖安装在 GitHub runner 上完成，无需在个人电脑部署项目。本地 / Docker 的调度和应用默认值不随此工作流改变。

## 时间与启用

工作流使用 GitHub 原生带时区调度：

~~~yaml
on:
  schedule:
    - cron: '30 18 * * 1-5'
      timezone: "America/New_York"
~~~

美国东部时间周一至周五 18:30；香港时间为次日 06:30（夏令时 EDT）或 07:30（冬令时 EST）。交易日过滤继续使用原有交易所日历，节假日跳过。提前收盘日仍在 18:30 分析。18:30 是计划时间，GitHub 排队可能延迟；当天更晚发布的公告不保证已收入报告。

调度变更必须合入默认分支才会自动生效，独立 PR 分支不会自动定时运行。进入仓库 Actions → 每日股票分析：只有页面确实显示禁用提示时，才点击 Enable workflow / I understand my workflows, go ahead and enable them。公开仓库长期无活动可能被 GitHub 暂停定时工作流，不能仅凭运行记录有空档就认定发生了禁用；同时检查最新提交、工作流状态、失败记录、配额与 GitHub 服务状态。

参考：[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 配置与手动执行

Actions 未显式配置时使用 REPORT_TYPE=full、MARKET_REVIEW_REGION=us、REPORT_LANGUAGE=zh。每一项仍为 Variables → Secrets → 工作流默认值的优先级；现有显式值不会被默认值覆盖。

STOCK_LIST 继续按原有顺序读取 Variables / Secrets，再读取同名运行环境值，最后才使用原有最小默认值。实际部署应在 Settings → Secrets and variables → Actions → Variables 中维护完整股票池，勿依赖示例列表。报告、计数和覆盖率校验都读取该配置，不在生产代码固定数量或代码清单。模型、数据源、凭据、并发数和通知渠道不在本变更中替换。普通分析和 Agent 路径均携带报价元数据；完整率只统计真正成功的分析，不用虚构分析填满失败股票。

手动执行一次：Actions → 每日股票分析 → Run workflow → 选择分支和模式 → Run workflow。

- full：股票池与大盘复盘。
- stocks-only：仅股票池，不要求生成大盘文件。
- market-only：仅大盘，不要求生成个股文件。
- force_run：保留原有跳过交易日检查功能；默认关闭。

调试 PR 分支会使用该分支代码和既有通知配置。离线 CI 不调用真实模型或发送分析通知；人工完整分析可能消耗现有模型额度，不自动更换 Key、付费档位或模型。

## 下载与固定文件路径

打开具体运行的 Summary → Artifacts → analysis-reports-<run_number> 下载 ZIP，保存期仍为 30 天，原有 reports/ 与 logs/ 输出均保留。附件过期后应读取较新的运行，不能把旧报告当成当日报告。

日期文件保持原路径：

- reports/report_YYYYMMDD.md：原有 Markdown 个股报告，增加覆盖统计与失败清单。
- reports/report_YYYYMMDD.json：公开结构化摘要，schema_version=1。
- reports/market_review_YYYYMMDD.md：原有大盘复盘。

同一运行附件内另外提供固定别名：

- reports/latest/run.json：运行产物验收状态、生成时间、运行 ID、预期股票池、计数和问题代码。
- reports/latest/report.md：本轮个股报告。
- reports/latest/summary.json：本轮个股结构化摘要。
- reports/latest/market_review.md：本轮大盘复盘。

固定路径是每个运行 ZIP 内的路径，不是无需身份验证的永久公网下载 URL。单独的股票或大盘模式只生成其适用文件；非交易日只生成状态为 skipped 的 run.json。旧固定别名在整理前清除，日期报告仍保留。

## 外部读取契约与数据质量

现有 ChatGPT 定时监控可以通过有权限的 GitHub 连接读取最新运行及其附件，不需要另建 Codex 提醒系统。读取顺序：

1. 确认工作流路径、运行 ID、事件类型、完成时间、结论与附件名；打开 latest/run.json。不要只看绿色图标。
2. 检查 status、workflow_run_id / workflow_run_attempt、generated_at、expected_codes 与 counts，再核对 summary.json 的 requested_codes、analyzed_codes、failed_codes。不要只取第一只或前几只股票。
3. 按每只股票的 analysis_time、quote.date、provider_timestamp、quote_session、currency、source 和 data_quality 判断时效；对行情和重要事件另行核实。
4. 仅在 ChatGPT 的私人上下文中结合真实持仓、成本与可用现金。公共报告中的通用持仓者建议不代表已确认的账户状态，也不表示订单已执行。

summary.json 的 stocks 包含代码、完成状态、评分、技术信号、风险、催化因素、观察触发条件、价格区间、市场阶段和报价质量。失败股票只有失败类别与空评分，不生成买卖信号。公开摘要使用字段白名单，排除账户上下文、原始模型响应、完整 provider 错误及凭据。

complete 表示预期股票分析与所需文件齐全，不表示价格、财报事实或模型建议已获独立认证。不要求每次达到全部股票成功：供应商失败造成的 partial 只要运行身份、逐股状态、失败分类、计数和所需文件自洽，就作为有效部分结果保留，工作流可正常完成；没有任何成功分析、文件缺失、运行身份错配或计数不一致仍验收失败。读取方必须检查 JSON 状态，不能只看绿色图标。上传步骤仍尝试保存可用报告，通知按原有成功结果继续发送。硬取消或 job 超时可能无法完成上传，不保证这种情况下存在附件。

报价规则：

- Yahoo Finance 标记为 yfinance；来源名称和数据质量分别展示，成功取数不再一概标成 fallback。
- 常规时段价格与昨收使用同一口径；表内涨跌额及涨跌幅从同一对价格计算。缺少基准时显示 N/A，不混用另一条历史日线填补。
- 盘前 / 盘后价格及其真实时间单独列示，不代替常规时段 OHLC；盘中价格明确标记未收盘。常规收盘报价仍是 provider 报价，不是交易所官方结算认证。
- 获取时间不是行情时间。缺少真实行情时间时明确显示未知及不能确认实时性，日线兜底不得冒充实时行情。
- 大盘复盘明确展示当前 market_phase。开盘前、盘中和收盘前只使用交易所日历确认的最近已完成交易日日线；常规交易结束后才允许使用当日日线，支持提前收盘。日历不可用时阶段为 unknown，不把无法确认的行情当作已完成收盘。
- 美股大盘指数的日线日期与分析生成时间分别保留。指数日期一致时用其作为复盘标题日期；缺失或混合日期时说明标题仅为分析日期。日线只有交易日期时，真实行情时间标为未知，不推断盘中或盘后实时价格。日期文件名仍沿用生成日期，因此盘前手动执行的文件日期可能比标题中的指数日线日期晚一天。
- 美股价格标注 USD；仅修正价格字段中未注明币种的“元”，保留明确标为人民币的财务事实。缺失字段、估算日线及陈旧标记继续披露。收盘后常规报价时间早于抓取时间是正常现象，应结合交易时段判断，不能只凭 stale 标记推断取数失败。

429 / rate_limited 表示需要检查现有模型项目的实际额度和限流；503 / provider_unavailable 表示服务不可用。不得仅凭错误码推断具体免费额度、恢复时刻或承诺重试一定成功。 明确的 429/503 不立即对同一模型再发非流式请求，按去重后的现有模型列表有界回退；格式解析失败保持 failed，不再进入字段补全重试。有效报告的字段补全仍遵循现有配置上限，未新增重试循环、模型或凭据。

## 隐私、验证与回滚

只向公开工作流传股票代码和公共分析配置；不要上传账户截图、持仓数量、平均成本、余额、交易流水或私人策略备注，不把它们写入变量、报告、Prompt 或日志。API Key 留在 Secrets 中。该流水线不读取私人账户快照，也不执行交易。

确定性检查在现有 CI 中运行；打开运行 Summary 的“美股日报契约验收：合成报告”部分，可查看 complete / partial 的 run.json 和可展开的 Markdown 报告。所有价格和事件均为测试数据，不需要修改 CI 工作流或添加新的上传步骤。测试时可通过 REPORT_EVIDENCE_DIR 指定完整 Markdown / JSON 证据输出目录（仅供测试，不是应用配置）。复现相关测试：

~~~bash
python -m pytest tests/test_us_daily_report_contract.py -q
~~~

测试覆盖原有变量顺序、三个手动模式、节假日 / force_run、夏令时边界、Yahoo 股票与指数报价、缺少时间 / 昨收、默认与 Jinja 报告、15 只完整 / 部分成功 / 全败、隐私字段、运行 ID 和稳定文件路径。真实模型分析、通知送达及新的定时调度须在合入后的正常运行中核验；离线合成测试不能替代它们。

## 身份、文件与状态校验

`full` 必须有 `reports/latest/run.json`、`summary.json`、`report.md` 和 `market_review.md`；`stocks-only` 不要求大盘文件，`market-only` 不要求个股文件。大盘另附 `market_review.json`，用于验证实际行情日期、结构化指数与来源。仅有 Markdown 或绿色图标不足以确认契约通过。

复用本轮大盘上下文时，沿用已经生成的结构化行情、日期和新闻数据，同时保存 Markdown 与同目录同名 JSON（仅扩展名不同），包括复盘锁未取得后的本轮缓存复用路径。整理步骤再将两者复制到 `reports/latest/`；不会为补摘要重新调用模型或搜索。上下文的结构化载荷只用于报告导出，不加入个股提示词的安全摘要。

`market_summary_missing` 表示已找到大盘 Markdown，但其同名 JSON 摘要实际缺失；四个基本文件存在不代表结构化验收通过。摘要缺失、为空、无法解析、身份或交易日不一致时仍报错，Markdown 空文件或仅含空白也不能通过。不得根据正文反推或伪造缺少的结构化行情。

公开 JSON 使用 `schema_version=1`，记录 workflow 路径、run ID、attempt、`source_branch`、`commit_sha`、`workflow_ref`、带时区的生成时间、冻结参考时间和 `trading_session_date`。校验器逐项比对身份、日期、配置清单、去重后的逐股状态、成功/失败计数、文件存在性，以及价格/昨收复算涨跌幅（容差 0.1 个百分点）。异常文件、身份错配或零成功均为 `failed`；真实部分成功为 `partial`，不虚构失败股票的评分。

正常定时仅有一个 America/New_York 工作日 18:30 cron，沿用并发控制；不启用第二个调度器。定时分析前用 XNYS 日历检查交易日及实际收盘，节假日写入 `skipped` 清单，日历不可用则关闭本次分析并报告失败。此检查不受关闭普通交易日过滤的配置影响；手动模式与 `force_run` 的原行为保留。若 GitHub 把执行延迟至下一个开盘前，本轮会标记未到常规收盘，而不会虚构新收盘行情。

## 收盘新鲜度、数值和新闻证据

`stale_seconds` 仍显示真实报价年龄；美股 `is_stale` 改按交易时段判断。最新已完成交易日的常规收盘价可在当晚、周末、节假日及下次开盘前继续有效，不能仅因超过600秒而标为过期。下一常规时段已开盘、早于应有交易日、未来时间或无法确认收盘的报价不会被认证为新鲜。`freshness`、`quote_session_date`、来源降级和缺失字段单独展示。成交额、筹码或技术指标缺失仍保留 `partial` 警告，新鲜度通过不等于行情独立认证。

美股股票指数均值只使用可用的 SPX、IXIC、DJI，不把 VIX 涨跌幅混入收益均值。均值口径、公式、纳入/排除代码、逐指数价格算术和已更正的叙述数字记录在大盘 JSON 中；只更正能由结构化行复算的明确指标。其他指标需要自行注明名称和来源，不能借此取得认证。

新闻继续采用原供应商配置、优先级、轮换、缓存及有界回退，不补打搜索来填充重复结果。每条证据保留实际搜索供应商、原始 URL、原始发布时间和检索时间；同一来源的重叠结果合并，已知带日期的一手披露优先。搜索摘要或 AI 文本始终不算独立事实核验。

财报、指引、并购、SEC 披露、共识和估值缺乏原文核验时会标为 `unverified`。已取得的同一发行人较新披露会关联到旧披露；这表示“材料中存在更新”，不自动证明已穷尽所有公告，也不证明旧指引已被正式撤回。模型不得把旧材料称为最新指引。未知发行人、缺日期、缺 URL 或只有二手报道时明确保留限制；不为此新增付费来源或额外 API 请求。

## 同步上游与离线修复

同步上游应使用合并或新 PR，并检查自有提交仍是 main 的祖先。不要通过丢弃本地定制或 force push 把 main 直接替换成上游历史；合并前确认定时、发布脚本、契约测试和本说明仍存在。

若任务要求完全不触发 Actions，修复分支提交可使用 GitHub 支持的 `[skip ci]`，避免 `push` / `pull_request` 自动检查；这不关闭合并后默认分支的正常定时任务。它不适用于 `pull_request_target` 等其他事件，提交前需检查现有工作流触发器。相关检查可能显示 Pending，不能把未运行的 CI 称为通过。[GitHub 跳过运行说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs)。

回滚本 PR 的代码提交即可恢复原工作流和报告行为，既有 Secrets / Variables 不需要重设，已生成的日期报告不受影响。回滚后调度与默认值也回到旧版本，需要另行检查是否仍适合美股。
