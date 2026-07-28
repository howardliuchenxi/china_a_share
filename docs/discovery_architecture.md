# 策略自动挖掘引擎 (Discovery Engine) 设计草案

## 1. 核心理念

复用 `china_a_share` 现有的高速 Tushare 接口层、多线程执行器、精密的 L1/L2 缓存和自然语言路由能力，将原本的“单次查询交互”升级为“后台异步循环进化的量化研究闭环”。大模型充当量化研究员，系统充当回测黑盒。

## 2. 后端架构设计 (Backend)

在现有的 `src/china_a_share` 下新增 `discovery` 子模块，与原有的 `application/workflow.py` 平行。

### 2.1 契约定义 (`src/china_a_share/core/contracts.py`)
新增挖掘任务与因子公式的数据结构：
* `DiscoveryTaskRequest`: 用户提交的任务配置（目标行业、训练时间段、验证时间段、偏好提示）。
* `FactorHypothesis`: AI 生成的单条假说（例如表达式 `ma(turnover_rate, 5) > 10 AND pe < 20`，以及解释说明）。
* `BacktestResult`: 回测黑盒返回的评价指标（胜率、超额收益率、最大回撤等）。
* `DiscoveryTaskStatus`: 记录当前循环到第几代、已测试公式数、当前 Top 3 公式。

### 2.2 因子回测黑盒 (`src/china_a_share/discovery/backtester.py`)
复用 `workflow.py` 的并发抓取能力。
**工作流**：
1. 解析 `FactorHypothesis` 中的表达式为 Pandas 的过滤/运算条件。
2. 批量拉取“训练时间范围”内每一天的股票池基础数据。
3. 应用条件筛选出每日符合特征的候选股票组合。
4. 拉取这些股票未来 5 天（或 N 天）的真实涨跌幅（复用 `period_return_by_ts_code` 逻辑）。
5. 计算胜率和收益，返回 `BacktestResult`。

### 2.3 进化控制器 (`src/china_a_share/discovery/evolution_loop.py`)
这是整个系统的“大脑中枢”（独立运行的异步后台任务）。
**循环逻辑**：
1. **生成 (Generate)**：将用户的约束条件和数据字典喂给 DeepSeek/Vertex 模型，提示其生成 5 条初始 `FactorHypothesis`。
2. **评估 (Evaluate)**：调用 `backtester.py` 计算这 5 个公式在**训练集**上的表现。
3. **选择与盲测 (Select & Validate)**：选出表现最好的 1 条，在**验证集**（未知数据）上跑一次盲测。如果表现依旧坚挺，加入“排行榜”。
4. **反思与变异 (Reflect & Mutate)**：将表现好的和表现差的公式连同它们的回测指标打包发给模型，要求它总结经验并交叉组合，变异出下一代的 5 个新公式。
5. **结束条件**：到达最大循环代数（如 10 代）或达到预期胜率阈值。

### 2.4 API 端点 (`src/china_a_share/api.py`)
* `POST /api/discovery/tasks`：接收前端配置，启动后台进化循环（复用现有的异步任务提交机制）。
* `GET /api/discovery/tasks/{id}`：轮询获取当前进化的日志流、排行榜和最新状态。

## 3. 前端界面设计 (Frontend)

在 `App.tsx` 中新增 `DiscoveryView` 视图，包含三个核心面板：

### 3.1 任务指挥台 (Mission Control)
表单区域，供用户设定挖掘目标：
* **限定池**：如全市场、仅沪深300、特定行业（复用现有的字典数据）。
* **训练窗口**：起止日期（用于 AI 试错拟合）。
* **盲测窗口**：起止日期（用于防止 AI 过拟合的最终考验）。
* **AI 引导词**：文本框，如“请寻找小市值且近期有机构资金流入的特征组合”。

### 3.2 进化直播室 (Live Evolution Dashboard)
任务启动后进入此视图。
* **状态打字机**：实时滚动的系统日志（“正在回测第 3 代假说…”、“发现严重过拟合，已淘汰公式 X”）。
* **代际胜率折线图**：展示随着 AI 迭代，训练集胜率与验证集胜率的演变趋势。

### 3.3 黄金规律荣誉榜 (The Golden Rules Leaderboard)
任务结束后的最终产出物展示。
* 列表展示 Top N 规律。
* 核心列：
  * **公式 / 描述**：人类可读的 AI 总结。
  * **盲测胜率**：在未见过的验证集上的准确度。
  * **超额收益**：相对基准的溢价。
* **一键应用按钮**：点击后，可直接将这个选股公式发送回原有的 `AnalysisView`（分析面板），看看“今天”有哪几只股票符合这个刚挖出来的极品规律！
