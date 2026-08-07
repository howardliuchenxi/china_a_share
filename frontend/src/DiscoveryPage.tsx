import { useEffect, useMemo, useState } from "react";
import { DATA_DICTIONARY_ENTRIES } from "./dataDictionary";
import {
  BacktestResult,
  DiscoveryTaskRequest,
  DiscoveryTaskStatusResponse,
} from "./contracts";

interface DiscoveryPageProps {
  onApplyFormula: (formula: string) => void;
}

const terminalStatuses = new Set(["succeeded", "failed"]);

function percent(value: number, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

function MetricSet({ result }: { result: BacktestResult }) {
  return (
    <div className="rule-metrics">
      <span><small>上涨概率</small><strong>{percent(result.win_rate)}</strong></span>
      <span><small>相对基准</small><strong className={result.win_rate_lift >= 0 ? "metric-positive" : "metric-negative"}>{result.win_rate_lift >= 0 ? "+" : ""}{percent(result.win_rate_lift)}</strong></span>
      <span><small>平均收益</small><strong>{percent(result.mean_return, 2)}</strong></span>
      <span><small>样本数</small><strong>{result.sample_count}</strong></span>
    </div>
  );
}

export function DiscoveryPage({ onApplyFormula }: DiscoveryPageProps) {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const [targetPool] = useState(params.get("dp_pool") || "A_SHARE");
  const [trainStart, setTrainStart] = useState(params.get("dp_ts") || "20240101");
  const [trainEnd, setTrainEnd] = useState(params.get("dp_te") || "20251231");
  const [valStart, setValStart] = useState(params.get("dp_vs") || "20260101");
  const [valEnd, setValEnd] = useState(params.get("dp_ve") || "20260630");
  const [prompt, setPrompt] = useState(params.get("dp_prompt") || "寻找未来一个月上涨概率较高且样本稳定的市场状态");
  const [forwardDays, setForwardDays] = useState(Number(params.get("dp_forward")) || 20);
  const [minimumSamples, setMinimumSamples] = useState(Number(params.get("dp_samples")) || 30);
  const [maxConditions, setMaxConditions] = useState(Number(params.get("dp_depth")) || 2);
  const [factors, setFactors] = useState<string[]>(() => {
    const selected = params.get("dp_factors");
    return selected ? selected.split(",") : ["pe_ttm", "turnover_rate", "circ_mv"];
  });
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskStatus, setTaskStatus] = useState<DiscoveryTaskStatusResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [showAllFactors, setShowAllFactors] = useState(false);

  const availableFactors = DATA_DICTIONARY_ENTRIES.filter(entry =>
    !["ts_code", "name", "trade_date", "end_date", "ann_date", "calculation_status"].includes(entry.field),
  );
  const bestRule = taskStatus?.progress.leaderboard[0] ?? null;

  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    next.set("page", "discovery");
    next.set("dp_pool", targetPool);
    next.set("dp_ts", trainStart);
    next.set("dp_te", trainEnd);
    next.set("dp_vs", valStart);
    next.set("dp_ve", valEnd);
    next.set("dp_forward", String(forwardDays));
    next.set("dp_samples", String(minimumSamples));
    next.set("dp_depth", String(maxConditions));
    if (prompt) next.set("dp_prompt", prompt); else next.delete("dp_prompt");
    if (factors.length) next.set("dp_factors", factors.join(",")); else next.delete("dp_factors");
    window.history.replaceState({}, "", `${window.location.pathname}?${next.toString()}`);
  }, [targetPool, trainStart, trainEnd, valStart, valEnd, forwardDays, minimumSamples, maxConditions, prompt, factors]);

  function toggleFactor(field: string) {
    setFactors(current => current.includes(field)
      ? current.filter(candidate => candidate !== field)
      : [...current, field]);
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!factors.length) {
      setSubmitError("请至少选择一个可搜索因子。");
      return;
    }
    setIsSubmitting(true);
    setSubmitError("");
    setTaskStatus(null);
    try {
      const payload: DiscoveryTaskRequest = {
        target_pool: targetPool,
        train_start: trainStart,
        train_end: trainEnd,
        val_start: valStart,
        val_end: valEnd,
        factors,
        prompt,
        max_generations: 1,
        forward_days: forwardDays,
        minimum_samples: minimumSamples,
        max_conditions: maxConditions,
      };
      const response = await fetch("/api/discovery/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail?.[0]?.msg || detail?.detail || "任务提交失败");
      }
      const submission = await response.json();
      setTaskId(submission.task_id);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务提交失败");
    } finally {
      setIsSubmitting(false);
    }
  }

  useEffect(() => {
    if (!taskId) return;
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await fetch(`/api/discovery/tasks/${taskId}`);
        if (!response.ok || !active) return;
        const data = await response.json() as DiscoveryTaskStatusResponse;
        setTaskStatus(data);
        if (!terminalStatuses.has(data.status)) {
          timer = window.setTimeout(poll, 2000);
        }
      } catch {
        if (active) timer = window.setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [taskId]);

  return (
    <div className="discovery-page">
      <section className="request-panel discovery-intro">
        <div>
          <p className="eyebrow">Evidence-first discovery</p>
          <h2>从历史数据反向发现规律</h2>
          <p>系统只在训练窗口生成规则，再用独立验证窗口排名。特征来自信号日，收益来自未来交易日。</p>
        </div>
        <div className="research-guardrails">
          <span>无同日收益泄漏</span><span>最小样本约束</span><span>95% 概率区间</span>
        </div>
      </section>

      <section className="request-panel">
        <div className="section-heading"><span>01</span><h2>定义研究目标</h2></div>
        <form onSubmit={handleSubmit} className="discovery-form">
          <label className="wide-field">
            <span>研究目标</span>
            <textarea value={prompt} onChange={event => setPrompt(event.target.value)} rows={2} placeholder="例如：寻找未来 20 个交易日上涨概率较高的市场状态" />
          </label>
          <div className="discovery-settings-grid">
            <label><span>训练开始</span><input value={trainStart} onChange={event => setTrainStart(event.target.value)} inputMode="numeric" /></label>
            <label><span>训练结束</span><input value={trainEnd} onChange={event => setTrainEnd(event.target.value)} inputMode="numeric" /></label>
            <label><span>验证开始</span><input value={valStart} onChange={event => setValStart(event.target.value)} inputMode="numeric" /></label>
            <label><span>验证结束</span><input value={valEnd} onChange={event => setValEnd(event.target.value)} inputMode="numeric" /></label>
            <label><span>未来交易日</span><input type="number" min="1" max="60" value={forwardDays} onChange={event => setForwardDays(Number(event.target.value))} /></label>
            <label><span>最小样本数</span><input type="number" min="5" max="10000" value={minimumSamples} onChange={event => setMinimumSamples(Number(event.target.value))} /></label>
            <label><span>最多条件数</span><select value={maxConditions} onChange={event => setMaxConditions(Number(event.target.value))}><option value={1}>1 个</option><option value={2}>2 个</option></select></label>
          </div>
          <fieldset className="factor-selector">
            <legend>候选因子 <strong>{factors.length}</strong></legend>
            <div className="factor-grid">
              {(showAllFactors ? availableFactors : availableFactors.slice(0, 24)).map(factor => (
                <label key={factor.field} className={`factor-checkbox ${factors.includes(factor.field) ? "is-selected" : ""}`}>
                  <input type="checkbox" checked={factors.includes(factor.field)} onChange={() => toggleFactor(factor.field)} />
                  <span>{factor.label}<small>{factor.field}</small></span>
                </label>
              ))}
            </div>
            {!showAllFactors && availableFactors.length > 24 && <button type="button" className="expand-factors-button" onClick={() => setShowAllFactors(true)}>展开其余 {availableFactors.length - 24} 个字段</button>}
          </fieldset>
          {submitError && <p className="discovery-error" role="alert">{submitError}</p>}
          <div className="discovery-submit-row">
            <p>当前版本搜索训练集分位数形成的单因子与双因子规则。</p>
            <button type="submit" disabled={isSubmitting || taskStatus?.status === "running"}>{isSubmitting ? "正在提交…" : taskStatus?.status === "running" ? "研究进行中" : "开始反向搜索"}</button>
          </div>
        </form>
      </section>

      {taskId && taskStatus && <section className="results-panel discovery-progress-panel">
        <div className="section-heading"><span>02</span><h2>研究进度</h2></div>
        <div className="research-progress-grid">
          <div><small>任务状态</small><strong>{taskStatus.status === "running" ? "运行中" : taskStatus.status === "succeeded" ? "已完成" : taskStatus.status === "queued" ? "排队中" : "失败"}</strong></div>
          <div><small>当前阶段</small><strong>{taskStatus.progress.current_stage}</strong></div>
          <div><small>训练样本</small><strong>{taskStatus.progress.training_sample_count.toLocaleString()}</strong></div>
          <div><small>验证样本</small><strong>{taskStatus.progress.validation_sample_count.toLocaleString()}</strong></div>
          <div><small>入榜规则</small><strong>{taskStatus.progress.formulas_tested}</strong></div>
        </div>
        <p className="live-log">{taskStatus.progress.current_log || "等待任务开始…"}</p>
        {taskStatus.error && <p className="discovery-error">{taskStatus.error.message}</p>}
      </section>}

      {bestRule && <section className="results-panel discovery-summary-panel">
        <div className="section-heading"><span>03</span><h2>验证集摘要</h2></div>
        <div className="headline-metrics">
          <div><small>最佳规则上涨概率</small><strong>{percent(bestRule.val_result!.win_rate)}</strong><em>95% 区间 {percent(bestRule.val_result!.confidence_lower)} – {percent(bestRule.val_result!.confidence_upper)}</em></div>
          <div><small>相对全样本提升</small><strong className={bestRule.val_result!.win_rate_lift >= 0 ? "metric-positive" : "metric-negative"}>{bestRule.val_result!.win_rate_lift >= 0 ? "+" : ""}{percent(bestRule.val_result!.win_rate_lift)}</strong><em>全样本 {percent(bestRule.val_result!.baseline_win_rate)}</em></div>
          <div><small>平均未来收益</small><strong>{percent(bestRule.val_result!.mean_return, 2)}</strong><em>中位数 {percent(bestRule.val_result!.median_return, 2)}</em></div>
          <div><small>事件曲线最大回撤</small><strong>{percent(bestRule.val_result!.max_drawdown, 2)}</strong><em>{bestRule.val_result!.sample_count} 个验证样本</em></div>
        </div>
        <p className="research-caveat">这是事件研究结果，不等同于可直接交易的组合回测；当前尚未计入涨跌停成交约束、手续费和持仓重叠。</p>
      </section>}

      {taskStatus && taskStatus.progress.leaderboard.length > 0 && <section className="results-panel">
        <div className="section-heading"><span>04</span><h2>候选规律排行榜</h2></div>
        <div className="rule-list">
          {taskStatus.progress.leaderboard.map((hypothesis, index) => <article className="rule-card" key={hypothesis.formula}>
            <div className="rule-rank">{String(index + 1).padStart(2, "0")}</div>
            <div className="rule-body">
              <div className="rule-heading"><div><h3>{hypothesis.description}</h3><code>{hypothesis.formula}</code></div><button type="button" onClick={() => onApplyFormula(hypothesis.formula)}>用于今日筛选</button></div>
              <p>{hypothesis.reasoning}</p>
              <div className="window-comparison">
                <div><b>训练窗口</b><MetricSet result={hypothesis.train_result!} /></div>
                <div><b>独立验证</b><MetricSet result={hypothesis.val_result!} /></div>
              </div>
              <p className="confidence-note">验证集上涨概率 95% 区间：{percent(hypothesis.val_result!.confidence_lower)} – {percent(hypothesis.val_result!.confidence_upper)}</p>
            </div>
          </article>)}
        </div>
      </section>}
    </div>
  );
}
