# A股3—5交易日趋势分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 用户已确认实施；功能开发和离线验证已完成，宿主测试及通过后的合并推送已获授权，QQ实际发送未执行。

**本轮执行状态：** 任务1—5的功能开发、公开接口只读核实及可用检查已完成；Python3.10与宿主Python3.12.12分别132项测试通过。真实接口已覆盖五指数两源及独立盘后四价验证，但未观察盘中更新全过程。Ruff不可用未执行；宿主加载/重载通过；真实LLM与降级测试已执行，最新提示词使用Gemini Flash完整验收通过；QQ发送未执行。详见设计第12节。复现日志和过程账本保留在本计划独立的本地忽略目录中，不纳入提交。

**Goal:** 为现有五指数分析添加可选的、由已完成日线支持的3—5交易日趋势研判。

**Architecture:** 新增独立历史客户端和纯趋势计算模块，由现有分析入口选择历史趋势或快照降级，保留原有图片及群发送流程。历史完整性与基准日期由程序控制，LLM只做定性解释。

**Tech Stack:** 现有Python、asyncio、aiohttp、dataclasses、statistics、json、unittest及Ruff；不新增运行时依赖。

**Spec:** `../specs/2026-10-23-market-trend-3-5d-design.md`。任务执行前必须读设计，所有数值口径以设计为准。

## Global Constraints

- 分析区间固定T+3至T+5交易日；T为日历确认且已完成的收盘日，不是自然日或查询时刻起算。
- 新开关`enable_trend_analysis`默认`false`；原LLM开关仍只决定定时推送是否分析。
- 80根请求、60根保留、至少21根连续有效收盘；5/10/20日累计收益、MA5/10/20及20日样本波动按设计计算。
- 日线源每源4秒、单指数9秒、全批12秒；成功缓存900秒、失败冷却60秒；LLM解析及生成：快照30秒，趋势90秒（真实验收后经用户确认调整，以适配当前 Grok）。
- 15:15为插件日线缓冲；旧快照路径历史日期保护保留，趋势路径另行验证T。
- 上证/深证/沪深300任一历史不合格，整体趋势为证据不足。
- 无付费服务、额外凭据、LLM联网、概率模型、自动复盘或新图表。
- 测试只用固定数据/SDK mock；真实行情只读核实、宿主加载与群发送分别报告，不能互相替代。
- 仅本插件范围；新增日志/注释英文，用户文案中文；保留本地配置、群映射及已有用户文件。
- 仅用户要求时提交/推送/发布/重载，不能按技能中的例子自动commit。

## Review Focus

1. 15:15前后、跨周末/国庆/跨年时，缓存不会混入未完成日线，也不把自然日当交易日（任务2、3）。
2. 坏行、缺失交易日、同日期冲突及未知金额，不能静默删行/补零后得到貌似完整的指标（任务2、3）。
3. 单调用取消、全批超时和卸载并发，不能影响其他调用或留下session/worker（任务2、4）。
4. 部分历史或核心指数失败时，仍展示原行情并明确降低整体结论，不重复调用模型（任务4）。
5. 图片构建、某群发送或LLM失败时，复用单批结果，开关关闭不额外抓历史（任务4、5）。

## Task 1: 先核实历史数据源契约

**Files:** 只读`market_api.py`、官方公告、候选公开接口；更新设计中接口核实结论；将核实后的固定公开响应写入任务2测试常量，不新建生产模块。

**Interfaces:** 输出五指数的源覆盖矩阵、响应字段位置/单位/未复权口径、请求参数、失败特征；供任务2锁定适配器。

- [x] 对设计列出的五代码分别访问两源，各设5秒只读核实超时，记录成功/失败，避免无界重试。
- [x] 核对代码、至少21个已完成日线、日序列、与可核对报价页面的一致性；验证金额口径，未确认金额设未知。
- [x] 单独确认腾讯指数返回`day`还是`qfqday`，不得混用复权字段。
- [x] 分别记录“收盘前是否含当日行”“当天行出现时间”“可确认完成的依据”。按设计3.2核实归档语义、完成标志或独立盘后四价核对；只有日期和OHLC的源不能凭15:15放行。把核实依据与判定方法锁定到源契约，未观察到的时段/字段写未验证；缺乏完成依据是适配验收阻断项。
- [x] 若环境阻断，按设计第9节例外，可用明确标注的假设契约继续任务2—5的mock/纯计算/离线集成，任务1真实源验收保持未完成；生产路径不得把未验证完成语义当作已完成。若五指数任一两源均不可用，报告限制；若需换整体方案，先修订设计。

**Deliverable:** 经核实的适配契约，或明确的环境阻断清单。无真实响应时不能把任务1标为全部完成。

## Task 2: 历史数据、日历与资源边界

**Files:** Create `market_history.py`, `assets/trading_calendar.json`, `tests/test_market_history.py`; Modify `.gitignore`。

**Interfaces:**

- 生产`DailyBar`、`IndexHistory`、`HistoryBatch`及`HistoryClient.fetch(expected_date: date) -> HistoryBatch`、`HistoryClient.close() -> None`，字段按设计第4节。
- 本任务同时定义`TradingCalendar`数据类型和`session_dates(start: date, end: date) -> tuple[date, ...]`；`HistoryClient.__init__(calendar: TradingCalendar)`接收该日历。先在测试内构造固定日历，任务3再补文件加载与基准函数，不形成反向任务依赖。
- 日历数据JSON结构固定：`schema_version: 1`，`years: {"2025": {"sources": [URL...], "verified_at": "YYYY-MM-DD", "closed_weekdays": [日期...]}, "2026": {...}}`。若两所差异则显式按交易所拆分，不以默认合并隐藏差异。
- 日线模型不含群号、UMO、提供商密钥。

- [x] 先写固定响应测试：正确代码/OHLC、倒序归一、相同重复行去重；冲突重复/未来行/坏价格拒绝主源并尝试备源。断言主源成功不请求备源，备源整段替代且不逐行混接。
- [x] 写缺失交易日、T不在末端、20/21/59/60条边界测试；T后当日未完成行被排除，T前缺失日不能删掉继续算。
- [x] 写15:15后仍返回同日期盘中旧值的测试：即使OHLC自洽、两次不变，没有完成证据也必须拒绝趋势；证据字段/核对方法按任务1锁定的真实契约实现，不凭空造源字段。
- [x] 写可选金额为0/负数/缺失/NaN/Inf、单位换算、未知统计范围测试。断言价格有效时可保留行情，未知金额不变0。
- [x] 写异步测试：同键两调用只有一个worker；取消一调用不取消另一；基准键变化不复用旧缓存；失败60秒后才重试；全批到期保留已完成指数。
- [x] 写关闭测试：close幂等；取消第一个close调用仍清理；关闭后fetch不新建session；所有worker被等待。跨代测试覆盖“A旧基准worker未完成→B新基准→close”与“B已缓存→A晚完成”，断言旧worker可被close找到、不重新写回旧缓存；较早请求晚到不能倒退当前基准。
- [x] 运行`python -X utf8 -m unittest discover -s tests -p test_market_history.py -v`，确认新增契约测试先失败。
- [x] 按已核实契约实现两源请求、归一及校验，维护独立实例资源。测试注入mock，不调用真实接口。
- [x] 从2025/2026年沪深官方公告整理JSON，保留来源和核对日期；逐项核对工作日休市，周末无需重复列入。
- [x] 将`.gitignore`的`/tests/`改为`/tests/*`，只放行`test_market_history.py`、`test_trend_analysis.py`、`test_trend_integration.py`，保持其余本地测试忽略。
- [x] 重跑本任务测试通过；JSON可解析，结果与官方来源一致。

**Deliverable:** 可以离线验证的历史客户端与年度日历；真实源状态与mock结果分别报告。

## Task 3: 基准日期、纯指标和提示词

**Files:** Create `trend_analysis.py`, `tests/test_trend_analysis.py`。

**Interfaces:**

```python
load_trading_calendar(path: Path) -> TradingCalendar
resolve_baseline(now: datetime, calendar: TradingCalendar) -> date | None
build_trend_context(batch: HistoryBatch, now: datetime,
                    calendar: TradingCalendar,
                    manual_closed_dates: list[str]) -> TrendContext
```

消费任务2定义的`TradingCalendar`，它保存核实年份及休市集合，未知年份明确失败，不能推断为普通工作日。日历类型定义放`market_history.py`，加载/基准逻辑放趋势模块，避免循环导入。

`TrendContext`字段固定：`baseline: date`、`target_dates: tuple[date, date] | None`、`history_text: str`、`quality_notes: str`、`eligible_codes: tuple[str, ...]`、`overall_allowed: bool`、`header: str`。`TREND_ANALYSIS_PROMPT`常量使用设计第6节完整文本；格式化传入当前文字快照及上下文，不解析行情文字反推数值。

- [x] 先写人工可验算的固定序列：C[t]=100+t，t=0..20。R5=`100*(120/115-1)`、R20=20%、MA5=118、MA20=110.5；用近似断言验证，且断言不是日涨跌简单相加。
- [x] 波动测试用交替±1%简单收益率：20个收益均值0，样本标准差为`sqrt(20/19)`个百分点；常数价格波动为0。
- [x] 金额比测试：20日金额全100则比1；前15日100、后5日200则比1.6；分母0、任一未知或口径未知返回未知。
- [x] 日期测试：2026-10-23 15:14基准10-22；15:15基准10-23；10-24仍10-23；10-08盘前基准09-30；10-10调休周六仍休市。2027未覆盖时不猜测；目标跨未覆盖年只保留相对T+3/T+5。
- [x] 覆盖核心指数缺失、部分有效、来源混合、未知快照时间、T+1盘中提示及每指数只有最近10日摘要；标题由程序生成。
- [x] 按设计3.2检查本次实际历史窗口起日至T+5范围的手动休市冲突：相关且官方本应开市的日期禁用趋势；无关日期不影响；官方已有休市重复项不是冲突；未来未知年份只提示且不生成目标日期；非法配置项警告忽略。验证输入列表未被修改。
- [x] 运行`python -X utf8 -m unittest discover -s tests -p test_trend_analysis.py -v`确认失败，然后实现加载、基准、指标和上下文。所有数值/缺失规则依设计，计算不访问网络。
- [x] 验证提示词所有占位符均能填入，包含T+3/T+5口径、反向证据、不得输出数字概率等约束；这仅验证模板，不声称模型实际永远遵守。
- [x] 重跑本任务测试通过。

**Deliverable:** 数值可复现、时间口径明确的模型输入，尚未接入宿主或命令。

## Task 4: 接入分析入口、配置和输出降级

**Files:** Modify `main.py`, `_conf_schema.json`; Create `tests/test_trend_integration.py`。

**Interfaces:** 保留`_generate_llm_analysis(overview, umo="", *, include_market=True)`及`_prepare_market_output`返回契约；实例增加惰性`_history_client: HistoryClient | None`和最近历史状态。复用现有提供商解析，不新增模型请求接口。

- [x] 新测试使用自包含最小SDK stub和固定历史，不依赖其他被忽略的本地测试文件。
- [x] 写开关关闭、仅推送LLM开关关闭、普通行情/速报/单指数入口测试：历史fetch均未调用；原提供商选择及原历史快照跳过行为不变。
- [x] 写手动趋势成功、周六合格历史仍分析、盘中不合并当天日线、15:15日线未更新降级、全部历史失败转快照、核心指数缺失整体证据不足等测试。
- [x] 每个降级分支断言总LLM调用不超过1次；输入始终包含完整当前文字行情和时间局限。历史全失败不能输出趋势成功标题。
- [x] 写文字/图片两模式、图片构造失败、某群发送失败和LLM空白/异常/超时测试：保留原行情，复用解读；两群只fetch一批历史、调用一次LLM。
- [x] 写卸载与活动历史请求竞态测试：close被await、取消传播、关闭后不重新发起历史或LLM；不宣称可以取消宿主所有外部事件，实际宿主排空语义需另验。
- [x] 专门覆盖“历史完成→提供商异步解析等待中→terminate→解析返回”，断言不会再调用LLM生成；检查实例终止标记在两个await阶段后生效。
- [x] 运行`python -X utf8 -m unittest discover -s tests -p test_trend_integration.py -v`确认失败。
- [x] 新增schema布尔字段及状态显示。主流程将`market_closed_dates`以只读参数传给`build_trend_context`，落实相关范围冲突检查。趋势开启时先独立验证历史，再选定趋势/快照prompt，最后复用一次模型调用。新旧路径分别处理历史报价保护。
- [x] 终止中先标记关闭、回收历史客户端，再与已有实时层清理配合；整个流程避免关闭失败阻止另一层清理，使用try/finally。
- [x] 运行本任务测试及现有`test_round1.py`、`test_image_output.py`；默认关闭应尽量使旧行为测试不需修改。需要改旧断言时逐项解释行为变化。

**Deliverable:** 可由开关启用的完整离线闭环，原有群发送与图片流程有回归证据。

## Task 5: 文档、整体核查与交付

**Files:** Modify `README.md`; 只有用户要求发布时再统一更新三处版本信息。

- [x] README同步schema示例、命令说明、固定T+3/T+5含义、15:15缓冲、周末/节假日、年度日历维护、实时与历史金额口径、耗时与降级、纯文本模型约束的局限。
- [x] 实际选择Python运行时，优先当前宿主环境；下列`python`命令指选定运行时，不安装依赖或切换全局环境。
- [x] 运行`python -X utf8 -m unittest discover -s tests -v`，分别报告新测试及已有回归结果；若环境缺依赖，保留实际失败信息，不能用历史通过数替代。
- [x] 对`main.py`、`market_history.py`、`trend_analysis.py`做AST语法解析；对schema和日历做JSON解析。运行可用的Ruff `check`与`format --check`，未安装则报告未执行，不静默安装。
- [x] 运行`git diff --check`。检查暂存/未暂存及新文件，确保只有授权文档/功能文件，无数据、群映射、凭据或背景文件。
- [x] 对照任务1，若真实源仍未核实，交付只能标为“离线实现完成、接口验收受阻”。有接口证据后再确认所有指数、部分失败和收盘更新行为。
- [x] 经用户授权执行宿主加载/重载与WebChat真实LLM、卡片和降级检查；最新提示词使用Gemini Flash完整验收通过。
- [ ] QQ实际消息未执行；WebChat图片输出不能替代真实QQ平台联调。
- [x] 向用户报告改动文件、静态/模拟/接口/宿主/QQ各层状态；准确率与交易收益均未验证。

**Deliverable:** 可审阅的代码、测试及中文使用说明；是否提交/发布由用户另外决定。

## 后续独立任务

若用户希望验证预测能力，再单独设计事前留档和到期评估：固定输入快照、模型版本、提示词版本和T，预注册方向标签与震荡阈值，分别评估T+3、T+5收益。相邻窗口重叠需避免把高度相关样本当独立证据；基准、覆盖率、弃权率和样本量一起报告。不能在本次功能验收中附带宣称“提高准确率”。
