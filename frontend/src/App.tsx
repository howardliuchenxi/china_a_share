import { FormEvent, useState } from "react";

import { submitAnalysis } from "./api";
import type { AnalysisResponse, QueryResult, ServiceError } from "./contracts";
import { experimentGroups } from "./experimentTemplates";

type AppTab = "analysis" | "reference";

const workflowSteps = [
  "理解自然语言数据需求",
  "选择白名单中的 Tushare 股票接口",
  "校验 A股 查询计划",
  "获取并展示表格结果",
];
const MAX_VISIBLE_ROWS = 100;
const referenceDatasets = [
  {
    name: "交易日历",
    source: "Tushare · trade_cal",
    description: "上海、深圳和北京证券交易所的交易日与休市安排。",
  },
  {
    name: "股票列表",
    source: "Tushare · stock_basic",
    description: "A 股代码、名称、市场、上市状态与基础行业信息。",
  },
  {
    name: "行业分类",
    source: "Tushare · 申万行业",
    description: "行业层级、行业代码以及股票与行业之间的归属关系。",
  },
  {
    name: "概念标签",
    source: "Tushare · THS / DC",
    description: "同花顺与东方财富口径的概念板块及每日成分。",
  },
];

const errorSourceLabels: Record<ServiceError["source"], string> = {
  tushare: "Tushare",
  deepseek: "DeepSeek",
  system: "系统",
};

function ErrorCard({ error }: { error: ServiceError }) {
  return (
    <div className="error-card" role="alert">
      <strong>{errorSourceLabels[error.source]} 错误</strong>
      <p>{error.message}</p>
      <dl>
        {error.code != null && <><dt>错误码</dt><dd>{String(error.code)}</dd></>}
        {error.http_status != null && <><dt>HTTP 状态</dt><dd>{error.http_status}</dd></>}
      </dl>
      {error.raw_response && (
        <details>
          <summary>查看上游原始响应</summary>
          <pre>{JSON.stringify(error.raw_response, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function ResultTable({ result }: { result: QueryResult }) {
  if (result.error) return <ErrorCard error={result.error} />;
  const visibleRows = result.rows.slice(0, MAX_VISIBLE_ROWS);
  return (
    <div className="result-block">
      <div className="result-heading">
        <h3>{result.api_name}</h3>
        <span>共 {result.row_count.toLocaleString()} 行</span>
      </div>
      {Object.keys(result.summary).length > 0 && (
        <dl className="summary-grid">
          {Object.entries(result.summary).map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd>{value.toLocaleString()}</dd></div>
          ))}
        </dl>
      )}
      {visibleRows.length > 0 ? (
        <>
          <div className="table-scroll">
            <table>
              <thead><tr>{result.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
              <tbody>
                {visibleRows.map((row, index) => (
                  <tr key={`${result.query_id}-${index}`}>
                    {result.columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.row_count > MAX_VISIBLE_ROWS && (
            <p className="table-note">仅显示前 {MAX_VISIBLE_ROWS} 行。</p>
          )}
        </>
      ) : <p className="empty-state">查询成功，但没有返回数据。</p>}
    </div>
  );
}

function ReferenceDataPage() {
  return (
    <div className="reference-page">
      <section className="reference-summary" aria-labelledby="reference-summary-heading">
        <div className="section-heading">
          <span>01</span>
          <h2 id="reference-summary-heading">数据集状态</h2>
        </div>
        <div className="setup-notice">
          <p className="panel-label">存储尚未接入</p>
          <h3>先确认需要沉淀的数据，再连接持久化存储。</h3>
          <p>当前页面只展示计划缓存的数据范围，不会写入本地文件或触发上游刷新。</p>
        </div>
        <div className="dataset-grid">
          {referenceDatasets.map((dataset, index) => (
            <article className="dataset-card" key={dataset.name}>
              <div className="dataset-card-heading">
                <span>{String(index + 1).padStart(2, "0")}</span>
                <span className="status-badge">尚未缓存</span>
              </div>
              <h3>{dataset.name}</h3>
              <p>{dataset.description}</p>
              <dl>
                <div><dt>来源</dt><dd>{dataset.source}</dd></div>
                <div><dt>本地记录</dt><dd>—</dd></div>
                <div><dt>最后更新</dt><dd>—</dd></div>
              </dl>
            </article>
          ))}
        </div>
      </section>

      <section className="tag-workspace" aria-labelledby="tag-workspace-heading">
        <div className="section-heading">
          <span>02</span>
          <h2 id="tag-workspace-heading">股票与标签</h2>
        </div>
        <div className="tag-layout">
          <div className="stock-lookup">
            <p className="panel-label">股票查询</p>
            <h3>按股票代码查看基础信息</h3>
            <label htmlFor="stock-code">A 股代码</label>
            <div className="lookup-control">
              <input id="stock-code" type="text" placeholder="例如：002594.SZ" disabled />
              <button type="button" disabled>查询</button>
            </div>
            <p className="control-note">接入持久化存储后开放查询。</p>
          </div>
          <div className="tag-empty-state">
            <p className="panel-label">标签视图</p>
            <h3>数据商标签与自定义标签将在这里分开展示。</h3>
            <div className="tag-examples" aria-label="标签展示示例">
              <span><small>THS</small> 新能源车</span>
              <span><small>DC</small> 锂电池</span>
              <span><small>自定义</small> 动力电池核心企业</span>
            </div>
            <p>示例仅用于说明展示方式，不代表当前已经缓存的数据。</p>
          </div>
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const [activeTab, setActiveTab] = useState<AppTab>("analysis");
  const [prompt, setPrompt] = useState("");
  const [response, setResponse] = useState<AnalysisResponse | null>(null);
  const [localError, setLocalError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [selectedGroupId, setSelectedGroupId] = useState(experimentGroups[0].id);
  const selectedGroup = experimentGroups.find((group) => group.id === selectedGroupId)
    ?? experimentGroups[0];
  const selectedTemplateApi = selectedGroup.templates.find(
    (template) => template.prompt === prompt,
  )?.apiName ?? "";

  function selectTemplate(apiName: string) {
    const template = selectedGroup.templates.find((item) => item.apiName === apiName);
    if (!template) return;
    setPrompt(template.prompt);
    setResponse(null);
    setLocalError("");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim() || isLoading) return;
    setIsLoading(true);
    setLocalError("");
    setResponse(null);
    try {
      setResponse(await submitAnalysis({ prompt: prompt.trim() }));
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "本地请求失败。");
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <p className="eyebrow">本地 A股实验室</p>
        <nav className="primary-tabs" aria-label="主要功能" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "analysis"}
            onClick={() => setActiveTab("analysis")}
          >
            数据分析
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "reference"}
            onClick={() => setActiveTab("reference")}
          >
            基础信息
          </button>
        </nav>
        <h1>{activeTab === "analysis" ? "用自然语言探索 A股数据。" : "整理 A股基础信息。"}</h1>
        <p className="hero-copy">
          {activeTab === "analysis"
            ? "数据范围固定为上海、深圳和北京证券交易所上市股票。"
            : "集中查看交易日历、股票列表、行业分类与概念标签的数据状态。"}
        </p>
      </header>

      {activeTab === "analysis" ? <>
      <section className="request-panel" aria-labelledby="request-heading">
        <div className="section-heading"><span>01</span><h2 id="request-heading">数据请求</h2></div>
        <form onSubmit={handleSubmit}>
          <div className="experiment-library">
            <div className="library-title">
              <p className="panel-label">实验模板</p>
              <h3>从测试问题开始</h3>
            </div>
            <div className="experiment-selectors">
              <div className="group-control">
                <label htmlFor="experiment-group">选择测试分组</label>
                <select
                  id="experiment-group"
                  value={selectedGroupId}
                  onChange={(event) => setSelectedGroupId(event.target.value)}
                >
                  {experimentGroups.map((group) => (
                    <option value={group.id} key={group.id}>{group.label}</option>
                  ))}
                </select>
              </div>
              <div className="question-control">
                <label htmlFor="experiment-question">选择测试问题</label>
                <select
                  id="experiment-question"
                  value={selectedTemplateApi}
                  onChange={(event) => selectTemplate(event.target.value)}
                >
                  <option value="">展开并选择一个问题</option>
                  {selectedGroup.templates.map((template) => (
                    <option value={template.apiName} key={template.apiName}>
                      {template.apiName} · {template.prompt}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <p className="library-description">{selectedGroup.description}</p>
          </div>
          <label htmlFor="analysis-prompt">描述你需要的数据</label>
          <textarea
            id="analysis-prompt"
            rows={6}
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="例如：北京时间2026年7月17日有多少只A股上涨，多少只下跌？"
          />
          <button type="submit" disabled={!prompt.trim() || isLoading}>
            {isLoading ? "正在分析…" : "开始分析"}
          </button>
        </form>
      </section>

      <section className="results-panel" aria-labelledby="results-heading">
        <div className="section-heading"><span>02</span><h2 id="results-heading">数据与错误</h2></div>
        {localError && <div className="error-card" role="alert"><strong>本地错误</strong><p>{localError}</p></div>}
        {response?.error && <ErrorCard error={response.error} />}
        {response?.results.map((result) => <ResultTable result={result} key={result.query_id} />)}
        {!localError && !response && <p className="empty-output">Tushare 查询结果将在这里显示。</p>}
      </section>

      <section className="details-stack" aria-label="查询详情">
        <details className="collapsible-panel">
          <summary><span>03</span><strong>查询流程</strong></summary>
          <ol className="workflow-list">{workflowSteps.map((step) => <li key={step}>{step}</li>)}</ol>
        </details>

        <details className="collapsible-panel">
          <summary><span>04</span><strong>查询计划</strong></summary>
          {response?.plan ? (
            <div className="plan-content">
              <h2>{response.plan.interpretation}</h2>
              {response.plan.queries.map((query) => (
                <div className="query-card" key={query.query_id}>
                  <strong>{query.api_name}</strong><p>{query.purpose}</p>
                  <code>{JSON.stringify(query.params)}</code>
                </div>
              ))}
            </div>
          ) : <p className="empty-output">完成分析后，可在这里查看经过校验的接口和参数。</p>}
        </details>
      </section>
      </> : <ReferenceDataPage />}
    </main>
  );
}
