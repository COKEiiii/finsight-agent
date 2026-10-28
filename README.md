# FinSight A-Share

A 股多因子选股、基于证据的中文 AI 研究、历史研究演示和人工模拟投资日志。研究模型默认使用本地 **Ollama**，以后可通过配置切换 DeepSeek。

```text
数据快照 → 固定规则筛选 → Top 10 → AI 研究卡 → 模拟组合计划 → 人工交易与复盘
```

## 安装与启动

Python 3.11 或 3.12，在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[dev]'
cp .env.example .env  # 已有 .env 时，请补充新配置，不要覆盖
finsight demo
finsight dashboard
```

`finsight demo` 使用固定随机种子的虚构数据及离线模板，不联网、不调用模型，生成 10 个候选、研究卡和 2025 年回测演示。在界面左侧选择对应演示快照后运行选股。数据、SQLite 和导出文件默认位于 `data/finsight/`，已排除在 Git 之外。

开发时可使用 `pip install -e '.[dev]'`；如果本机忽略了 editable `.pth` 文件，使用非 editable 安装，或以 `PYTHONPATH=src python -m finsight ...` 运行源码。

## 用本地模型生成研究卡

```bash
ollama serve                         # 已运行时跳过
ollama pull qwen3:8b                 # 已下载时跳过
finsight screen                     # 复制输出中的 run_id
finsight research --run RUN_ID --provider ollama --model qwen3:8b --limit 1
```

Dashboard 的“研究卡”页面也可选择本地模型。默认先生成一只股票，按需扩大到 Top 10。模型仅能读取当前候选股的快照证据；不会调整量化排名。每只最多 3 次模型请求、6 次工具执行、每次输出上限 1500 tokens；失败与格式修正计入预算。研究卡会核验引用与数字，明显不受证据支持的数字会被拒绝。页面刷新不触发调用，相同输入、提示词、模型和端点复用缓存。离线模板会明确标为 `demo`。

默认使用本地 `qwen3:8b`，可通过模型输入框或 `FINSIGHT_MODEL` 更换。两个本地模型在旧版 30 案例中均存在披露和风险遗漏，新版生成质量尚未完成整批比较。常规解释未通过时，最后一次请求使用**模板辅助修正**：模型从已核验的事实措辞及证据配对中选择要点，程序填写数值与单位，并依据实际字段生成信息缺口。卡片会明确标注该模式；模型接口失败仍返回失败状态。首次生成可能需要数分钟，已通过的同版本结果会复用缓存。

当前完成检查还要求：缺失或不可用的 ROE、营收增长及年报报告期在 `information_gaps` 明确说明“无法判断”；已经发生的营收负增长、超过 550 天的年报在 `risks` 中说明并引用对应字段。只写入选理由、未来假设或一般风险不算完成。遗漏会进入原有三次请求预算内修正，最后的模板辅助也必须选中已知风险；预算用尽仍遗漏则生成失败。离线演示同样展示这些已知风险，但始终标为 `demo`。旧版已保存卡保留原状态，重新生成才使用当前版本的检查。

字段级研究卡由程序列出数值、单位、日期与来源，模型只提供解释和风险。模型输入使用字段占位符、正负信号与指标定义，避免小模型抄错数值；精确原值完整保存在研究卡的证据表与 JSON 中。报告期作为时间背景不会被误判为独立的日期断言，但明确日期、指标引用和单位仍须匹配；已缓存的解释也会重新通过当前事实校验。可先从真实选股记录准备固定的 30 个评估案例（10 个原始候选、20 个标明为人工变造的缺失、陈旧、负增长和单位案例），再在本地比较两个模型：

```bash
finsight evaluate --run SCREEN_RUN_ID
finsight evaluate --run SCREEN_RUN_ID --models qwen3.5:4b,qwen3:8b
finsight evaluate --status SUITE_ID
finsight evaluate --resume SUITE_ID --models qwen3.5:4b,qwen3:8b
finsight evaluate --audit SUITE_ID       # 只复核已保存响应，不调用模型
finsight evaluate --review-template SUITE_ID  # 导出绑定原响应的审阅 CSV 和原卡
finsight evaluate --review SUITE_ID --review-file /path/to/review.csv
finsight evaluate --review SUITE_ID      # 查看人工审阅进度和比较结果
```

模型可在解释中使用 `{{financials.roe}}` 这样的字段占位符，程序校验它与引用的指标一致后填入数值和单位。报告同时保留模型模板与最终内容；空白数字句、单位错配和未提供的市值等事实会被拒绝。

评估逐案例保存 `state.json`、模型明细和汇总，`--status` 只读进度；`--resume` 保留已完成案例。新建时可用 `--limit` 做小批验证，恢复时不改变案例上限与模型顺序。仅准备案例时不调用模型，随后可指定两个本地模型恢复执行。旧版只有案例文件的评估需重新准备新套件，旧文件保留。

评估固定提示词、校验代码、Ollama 地址及模型权重摘要，模型或评估代码变化时停止，避免混用版本。每个新套件使用独立缓存范围，日常研究卡缓存不参与首次质量／耗时对比。报告分开列出自由生成、模板辅助、缓存、场景检查和新生成耗时；耗时含失败尝试，缓存原始用量不重复计入。若进程在某案例请求途中中断，该案例标为用量未知且不自动重跑，保守保留三次请求预算，其余案例可恢复。连续三个案例出现接口故障时停止，修复 Ollama 后再恢复。

网页“研究卡 → 研究卡人工验收”可下载原卡和绑定审阅表、导入填写结果。CLI 的 `--review-template` 提供相同流程：每行保存套件、引擎、模型、案例及响应指纹，身份列保持原样，填写审阅人、严重事实错误数、证据是否完整（true/false）、实际修订次数和审阅秒数；问题需在 notes 说明。未审阅行留空，每次导入保存整份进度，不自动合并其他文件，可下载已保存 CSV 继续填写。

人工结果单独追加保存，原始 `review_template.csv`、模型响应和生成分数不会覆盖；`--status` 保留原基线汇总，`--review` 查看人工结果。按模型分别统计审阅覆盖、严重事实错误、证据缺口、修订量、人工耗时和原生成耗时。完整审阅且无严重错误、成功卡证据完整，并通过原 30 案例自动门槛后，才允许该**套件原版本**通过研究质量验收；失败卡单列且需说明原因，演示数据不能通过正式验收。审阅旧版本不代表当前版本或策略有效性验收。

`evaluate` 整批运行只调用本地 Ollama，用量独立保存在评估汇总中；人工审阅不调用模型。`usage` 汇总日常研究和已保存的小规模 API 联调实验，单列实验卡数量；实验卡不出现在正式候选研究报告中。

`--audit` 为已结束的评估生成独立的 `audit.md`、`audit.json` 和逐案例 CSV。它按当前校验器重查原始模板、字段与证据绑定，区分“原生成完成”和“保存的响应现在可验证”，不会覆盖原分数、原响应或人工表，也不会把旧失败记录改成新成功。场景检查单独核对缺失 ROE 的明确提示、年报过时警告，以及负增长是否进入风险部分；只在入选理由里提到负增长仍算风险覆盖不足。所有人工质量结论继续保持待审核。

配置示例：

```dotenv
FINSIGHT_DATA_DIR=data/finsight
FINSIGHT_PROVIDER=ollama
FINSIGHT_MODEL=qwen3:8b
OLLAMA_HOST=http://localhost:11434
# 以后切换：FINSIGHT_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

也可显式执行 `finsight research --run RUN_ID --provider deepseek`。API Key 只从环境读取，不保存到报告或数据库。

若通过兼容平台调用 DeepSeek，在 `.env` 中使用该平台提供的 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。网页选择“DeepSeek · API”后读取这些配置；默认仍使用本地 Ollama。建议先验证一只，再扩大到少量候选；已有的三次请求、输出长度和缓存限制继续生效。

查看本项目已保存研究记录的 token 用量（不会调用模型）：

```bash
finsight usage --provider deepseek
finsight usage --provider deepseek --model MODEL_NAME
```

统计包含失败研究记录中已返回的输入与输出用量；缓存卡的原始生成用量不会再次累加。网页也显示逐卡用量与所选模型的累计记录。连接测试、其他应用及未返回 `usage` 的请求不包含在此计数中，因此它不是平台剩余额度。平台自身的免费额度与用完即停设置需在平台管理。

## v0.2 全主板采集与发布

本机首份全主板榜单已于 2026-10-28 完成发布，截止交易日为 **2026-10-27**：3,194 只全部处理，210 只已确认规则排除；应判定 2,984 只，其中 2,941 只可完整判定，覆盖率 **98.56%**，43 只保留为数据缺口，1,809 只符合首版规则。连续全主板达标目前 **1/5 个交易日**，仍保留 18:15 试运行任务。此结果只验收当前选股数据，历史回测验证继续未通过。

因子行缓存分批查询、在同一事务中保存未命中行，继续兼容原缓存键。整批缓存校验内容指纹，逐股缓存与当前计算值核对；发现损坏时重新计算，百分位和排名仍在当日股票池中生成。数据分区和原始快照不随缓存修复改写。

可用以下命令复现离线缓存基准（合成数据，不调用行情或模型）：

```bash
PYTHONPATH=src python scripts/benchmark_screening.py --stocks 3000 --output data/cache-benchmark.json
```

本机一次对照中，3,000 只合成股票、各 183,000 行行情和估值的因子／缓存／排名冷启动从 3.56 秒降至 0.50 秒，连接从 6,002 次降至 3 次、事务提交从 3,001 次降至 2 次，输出指纹相同。缓存命中约 0.10 秒。该范围不含采集、快照加载和文件导出，不能当作全主板采集耗时或通用速度保证。

首份真实全主板快照重复选股两次，候选、各因子得分、剔除项完全一致；本机加载到导出分别为 0.87、0.52 秒，已有因子行缓存被复用。该测量不包含行情采集，不能视为全新缓存冷启动。

可用 `finsight diagnose --run SCREEN_RUN_ID`，或网页“候选排行榜 → 选股诊断”查看六因子加权分数贡献、合格股票池的百分位相关性及行业分布，导出 JSON、Markdown 和 CSV。诊断重新校验原快照、配置与排名，和选股共享评分函数；不会重新抓取、调用模型、调参或写入交易日志。分数贡献不是收益归因，相关性不代表因果关系；行业目标权重只是固定规则参考，实际持仓与交易成本仍须单独复盘。

建议先完成验证批次，再启动全市场任务。采集在后台运行；中断后从已保存的请求继续。默认由 4 个常驻工作进程处理，所有进程合计每秒最多发起 1 个新请求；单股超时 30 秒，批量超时至少 120 秒，失败最多重试 2 次（等待 5、30 秒）。

```bash
finsight sync --provider akshare --universe mainboard --limit 100 --background
finsight jobs list
finsight jobs show JOB_ID
finsight jobs profile JOB_ID          # 导出采集验收 JSON、Markdown 与接口性能 CSV
finsight jobs resume JOB_ID
finsight sync --provider akshare --universe mainboard --limit 500 --background
finsight sync --provider akshare --universe mainboard --background
```

`--limit` 批次使用独立的股票池定义与成员清单，不能算作全主板榜单。完成任务后会自动校验并选股。已确认的 ST、新股等规则排除单列；接口失败、未知交易状态、缺失财报计入应判定分母。可完整判定的比例达到 98% 才发布当期榜单；低于门槛的结果只能用 `finsight screen --snapshot SNAPSHOT_ID --preview` 查阅。界面展示全市场与行业覆盖率、缺口、请求次数、缓存命中和恢复入口。历史回测可信度仍单独验收。预览榜单不能生成正式模拟调仓记录。

界面分别显示“股票处理完成”和“榜单可发布”。若今天首次取得的股票状态无法用于昨天的截止日期，旧任务会明确提示等待新交易日收盘后创建增量任务；不会通过恢复旧检查点来补造历史状态。已完成任务的耗时在结束时固定。源请求失败或中断的任务仍可恢复，已保存的历史底座会被新交易日的增量任务复用。

收盘前重复更新同一截止日时，已完成且因状态时间被拦截的初始化会被跳过，定时检查返回 `waiting_new_session`。如需另行扩展历史数据窗口，可显式提供 `--start`，历史研究资格仍独立判定。

任务进程退出后，网页和采集报告显示“已中断”，不会仅凭数据库残留的 `running` 字样表示仍在采集。CLI 保留原 `status`，另提供 `observed_status`、`process_alive` 和 `interrupted`；观察状态不会修改旧记录或自动创建新任务。中断期间的显示历时截至最后活动记录，退出时刻未被观测到时不会补造。可用 `jobs resume JOB_ID` 复用原请求检查点。

每次采集结束会自动保存接口性能报告；也可用 `jobs profile` 导出当前报告。报告区分任务历时、最近一次尝试耗时、累计请求耗时、缓存读取、实际网络请求、HTTP 字节与返回行数。累计请求耗时存在并发重叠；HTTP 字节不包含未计量的 BaoStock TCP 字节。旧记录未采集的行数显示为空，不用零冒充。

新快照引用不可变的 Parquet 分区；旧版快照和运行记录继续可读。日常更新拉取缺失区间并重取最近 10 个交易日。东方财富估值接口目前返回单股历史，程序只在本地截取和缓存，没有宣称服务端增量；行情备用路径 BaoStock 支持按区间取得原始价格、PE-TTM/PB 和复权收盘价。财务保持原有年报加权 ROE 口径，批量接口失败后改用单股财务接口；BaoStock 平均 ROE 不替代该字段，旧快照中的这种数据会被标记为口径不符。来源、公告日期、首次抓取时间与缓存读取时间分开保存；修订值不会被提前用于选股。财务每日复核，同一天已验证的记录可复用。

当估值缓存缺少目标交易日时，程序会跳过缓存重新请求一次，防止晚上继续使用早上尚未包含当日收盘数据的历史估值。新响应追加保存，旧原始文件与首次抓取时间仍可追溯。

4 个工作进程共享网络请求启动额度，接口内部的 HTTP 翻页也受每秒一次约束。BaoStock 请求单独串行调度；连续三次超时或连接失败会停止批次并保存断点。任务记录同时显示 API 尝试次数、可观测的实际网络请求数、Parquet 返回大小及 HTTP 响应体大小；强制超时被终止的请求无法取得完整计数，会单列。

全主板第一次完成后，macOS 增设 18:15 试运行任务。只有连续 5 个真实交易日的完整股票池、覆盖率达标且榜单已发布，才把原 18:05 正式任务切换到全主板。休眠或上游异常会使连续天数重新计数；系统不会补造历史快照。

首次扩展到新股票池时，程序会从最近的本地真实快照中复用逐股行情、估值与财务记录，再补齐缺口。目标股票池成员、名称和风险状态重新获取，不能沿用其他批次的成员清单。任务配置和新快照会记录父快照编号；合成数据、未来抓取记录与晚于目标日期的快照不能作为数据底座。

已有真实快照通过校验后，可在网页左侧点击“查看最新可用榜单”打开候选表。页面明确标注数据日期和验证批次范围；历史回测验证与当日选股资格分别显示。

## 数据采集

```bash
finsight sync --provider akshare                     # 20 只跨行业样本
finsight sync --provider akshare --universe mainboard --background # 完整当前沪深主板，可能跨多次任务完成
finsight sync --provider akshare --symbols 600036,000333
finsight screen --snapshot SNAPSHOT_ID
finsight status
```

真实数据收盘后北京时间 18:00 起更新。默认历史预热窗口为 430 天，可用 `--start 2022-01-01` 扩大。使用交易所股票列表、日线、年报业绩、单股历史 PE-TTM/PB 和沪深 300 指数，不用动态 PE 冒充 PE-TTM。各接口有进程级超时、有限重试、原始结果缓存和抓取时间；上游失效会显示错误，不替换成演示数据。

快照为追加保存的 Parquet 分区清单和元数据，并核验文件哈希。财报保存报告期、公告日、首次可见日及版本。日期精度的公告保守地从下一交易日使用，新抓取的修订数据不会回填到其首次观测之前。今日股票状态不冒充历史状态。

持续采集：

```bash
finsight watch --once                # 检查最近已完成交易日；适合系统调度
finsight watch                       # 常驻检查，补采行情历史
finsight schedule install            # macOS：安装每天18:05与登录时采集
finsight schedule status             # 查看本机定时任务状态
```

可在 cron/launchd 中每个工作日 18:05 运行 `watch --once`，也可每天登录时执行一次。休眠期间错过的行情可以补采，未保存的当日财务/股票状态版本不可事后重建；质量报告列出缺失快照日期。程序不会把补采值伪装成当日已知信息。安装命令不会调度模型调用或下单。系统调度的配置与卸载方式见 [运行说明](docs/operations.md)。

## strategy_v0.1

- 沪深主板、上市满两年，排除 ST、退市整理、停牌及缺失必要数据。
- 近 20 交易日平均成交额至少 5000 万元；要求连续 61 个交易日用于趋势计算。
- 最新可用年报的 ROE、年营收同比增长各 20%；60 日动量、MA20/MA60−1 各 20%；正 PE-TTM、正 PB 各 10%。年报报告期距选股日不超过 550 天。
- 在合格股票池内进行百分位排名；并列取平均名次，估值反向排序，总分并列按代码排序。
- Top 10 中按排名构造最多 8 只、每只 10%、同一行业最多 3 只的参考组合，剩余留现金。
- 月末收盘生成调仓计划，下一个交易日执行；单股目标上限 15%，股票总目标上限 80%。

所有默认参数见 [策略配置](config/strategy_v0.1.json)。同一数据目录中，同一策略版本不可换参数；调整时更换版本后用 `finsight --config NEW_CONFIG.json ...`。负增长保留，缺失因子不填零。估值第一版在全股票池排名，保留行业暴露供复盘。

## 回测与研究验收

```bash
finsight backtest --start 2023-01-01 --end 2024-12-31 # 开发区间
finsight backtest --start 2025-01-01 --end 2025-12-31 # 独立验证区间
```

回测采用现金与持仓账本、收盘信号次日开盘执行、T+1、100 股买入、费用、滑点和未成交记录。未核验涨跌停价时不假设成交。分红送转使用公司行动表进入股数、现金及应收账本；默认分红采用税前金额。默认佣金双边万三、最低 5 元，税费按生效日期处理，滑点单边 5bp。

对照包含相同准入股票池的纯 60 日动量组合、沪深 300 价格指数、80% 指数加 20% 现金对照。保存净值、回撤、风险、换手、费用、行业暴露、信号和执行明细。短样本的年化指标不能证明策略有效性。

**产品验收与研究验收独立。** 演示数据永远不通过研究验收。AKShare 最新列表/财报不自动证明历史可得性；历史股票池、ST 状态、退市、财务修订、估值、复权和公司行动任一未核验，报告均显示“研究演示：历史验证未通过”。当前适配器不自动授予通过状态。详细限制见 [架构与验收](docs/architecture.md)。

## 人工模拟日志

每次选股导出 `journal_template.csv` 和 `portfolio_plan.json`。在同花顺人工操作后，通过 Dashboard 或 CSV 记录计划、委托、成交、撤单、拒单与费用：

```bash
finsight journal import path/to/journal.csv
finsight journal export --account paper
finsight journal report --account paper --capital 100000
finsight journal report --capital 100000 --actual-cash 80000 --actual-holdings holdings.json
finsight journal plan --run SCREEN_RUN_ID --account paper --capital 100000
```

`holdings.json` 格式为 `{"600036.SH": 100}`。成交事件的 `quantity` 为**本次增量成交量**；不能既导入两笔部分成交，再重复导入包含它们的累计成交量。`event_id` 支持幂等导入；同 ID 内容冲突时整批回滚。每条事件关联已有选股记录和策略版本。同一交易日的事件应按实际执行先后导入。首次录入前，账户应为空仓；导入交易应从账户起点完整记录。

同一笔订单用 `order_id` 关联，按“账户＋交易日＋委托编号”区分，允许券商编号跨日复用。`planned/submitted` 填委托总量，`partial/filled` 填本笔增量成交量；撤单保留已成交部分，剩余数量不再计为待处理。没有委托编号的旧事件保持独立，不猜测对应关系。网页重复提交保留事件编号；录入下一条事件前点击“使用新的事件编号”。

周报导出 `weekly_review.md`、`portfolio_review.json`、`portfolio_nav.csv`、`orders.csv` 和 `executions.csv`，展示待处理委托、最近五个交易日成交、实际费用及相对计划价的不利价差。买入更贵或卖出更便宜均为正价差；缺少计划价时标为未知，费用另计。`orders.csv` 是订单汇总，**不可作为成交日志再次导入**；原始 CSV 事件请用网页的“导出日志 CSV”（CLI `journal export` 输出 JSON）。未提供实际现金／持仓时，报告不会宣称已对账。

`journal plan` 和网页的“生成账户调仓计划”使用同一服务。`--capital` 填账户起点的初始资金，当前净值由完整日志和信号日收盘价计算；目标股数扣除已有持仓后才形成买卖差额。计划绑定原始快照、选股配置、账户和日志指纹。月末生成次交易日的人工提案，非月末只给目标参考。未处理委托、较晚日期的日志或缺少当日持仓价格时阻止生成指令。

账户调仓导出 `rebalance_plan.json` 与 `rebalance_orders.csv`。先列卖出，再按候选排名分配买入；按整手、可用现金、估计费用以及费用后净值检查仓位。已标明哪些买单依赖卖出所得，应收分红不可提前花费。收盘参考价格和估计成交不构成实际委托，卖出失败或次日开盘变化时必须重新核对。提案不会自动写入日志或下单；策略仍为冻结的 `strategy_v0.1`。

## 检查与项目结构

```bash
python -m pytest -q
python -m ruff check src/finsight tests/finsight
```

`src/finsight/` 包含数据、选股、研究、回测、组合、服务、CLI 和 Dashboard。原有根目录 `app.py`、`agent.py`、通用工具和测试继续作为 Ollama 学习入口；Agent 支持注入独立工具注册表。GitHub Actions 在 Python 3.11/3.12 上运行测试、静态检查和离线 Demo。

本次联调结果见 [验证记录](docs/validation.md)。

参考：[TradingAgents](https://github.com/TauricResearch/TradingAgents)、[TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN)、[AKShare](https://akshare.akfamily.xyz/data/stock/stock.html)、[Ollama](https://docs.ollama.com/capabilities/tool-calling)、[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)。本项目独立实现本地轻量工作流。
