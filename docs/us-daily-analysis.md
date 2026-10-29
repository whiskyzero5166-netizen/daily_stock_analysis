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

STOCK_LIST 继续按原有顺序读取 Variables / Secrets，再读取同名运行环境值，最后才使用原有最小默认值。实际部署应在 Settings → Secrets and variables → Actions → Variables 中维护完整股票池，勿依赖示例列表。模型、数据源、凭据、并发数和通知渠道不在本变更中替换。普通分析和 Agent 路径均携带报价元数据；完整率只统计真正成功的分析，不用虚构分析填满失败股票。

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

complete 表示预期股票分析与所需文件齐全，不表示价格、财报事实或模型建议已获独立认证。部分失败或文件缺失会让报告验收步骤失败，后续上传步骤仍尝试保存已有报告；通知按原有成功结果继续发送。硬取消或 job 超时可能无法完成上传，不保证这种情况下存在附件。

报价规则：

- Yahoo Finance 标记为 yfinance；来源名称和数据质量分别展示，成功取数不再一概标成 fallback。
- 常规时段价格与昨收使用同一口径；表内涨跌额及涨跌幅从同一对价格计算。缺少基准时显示 N/A，不混用另一条历史日线填补。
- 盘前 / 盘后价格及其真实时间单独列示，不代替常规时段 OHLC；盘中价格明确标记未收盘。常规收盘报价仍是 provider 报价，不是交易所官方结算认证。
- 获取时间不是行情时间。缺少真实行情时间时明确显示未知及不能确认实时性，日线兜底不得冒充实时行情。
- 美股大盘指数的日线日期与分析生成时间分别保留。指数日期一致时用其作为复盘标题日期；缺失或混合日期时说明标题仅为分析日期。日线只有交易日期时，真实行情时间标为未知，不推断盘中或盘后实时价格。日期文件名仍沿用生成日期，因此盘前手动执行的文件日期可能比标题中的指数日线日期晚一天。
- 美股价格标注 USD；仅修正价格字段中未注明币种的“元”，保留明确标为人民币的财务事实。缺失字段、估算日线及陈旧标记继续披露。收盘后常规报价时间早于抓取时间是正常现象，应结合交易时段判断，不能只凭 stale 标记推断取数失败。

429 / rate_limited 表示需要检查现有模型项目的实际额度和限流；503 / provider_unavailable 表示服务不可用。不得仅凭错误码推断具体免费额度、恢复时刻或承诺重试一定成功。

## 隐私、验证与回滚

只向公开工作流传股票代码和公共分析配置；不要上传账户截图、持仓数量、平均成本、余额、交易流水或私人策略备注，不把它们写入变量、报告、Prompt 或日志。API Key 留在 Secrets 中。该流水线不读取私人账户快照，也不执行交易。

确定性检查在现有 CI 中运行；打开运行 Summary 的“美股日报契约验收：合成报告”部分，可查看 complete / partial 的 run.json 和可展开的 Markdown 报告。所有价格和事件均为测试数据，不需要修改 CI 工作流或添加新的上传步骤。测试时可通过 REPORT_EVIDENCE_DIR 指定完整 Markdown / JSON 证据输出目录（仅供测试，不是应用配置）。复现相关测试：

~~~bash
python -m pytest tests/test_us_daily_report_contract.py -q
~~~

测试覆盖原有变量顺序、三个手动模式、节假日 / force_run、夏令时边界、Yahoo 股票与指数报价、缺少时间 / 昨收、默认与 Jinja 报告、36 只完整 / 5 只成功 / 全败、隐私字段、运行 ID 和稳定文件路径。真实 36 只模型分析、真实通知送达及新的定时调度须在合入后核验；离线合成测试不能替代它们。

回滚本 PR 的代码提交即可恢复原工作流和报告行为，既有 Secrets / Variables 不需要重设，已生成的日期报告不受影响。回滚后调度与默认值也回到旧版本，需要另行检查是否仍适合美股。
