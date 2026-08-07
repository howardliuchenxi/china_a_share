import { useEffect, useMemo, useState } from "react";
import { DATA_DICTIONARY_ENTRIES } from "./dataDictionary";
import {
  BacktestResult,
  DiscoveryTaskRequest,
  DiscoveryTaskStatusResponse,
  FactorHypothesis,
} from "./contracts";

interface DiscoveryPageProps {
  onApplyFormula: (formula: string) => void;
}

const terminalStatuses = new Set(["succeeded", "failed"]);
const discoveryFactorFields = new Set([
  "amount", "circ_mv", "close", "distance_from_5d_peak_pct", "dv_ratio", "dv_ttm", "float_share",
  "free_share", "max_drawdown_5d_pct", "open", "pb", "pct_chg", "pe", "pe_ttm", "ps",
  "ps_ttm", "positive_days_3", "return_5d_pct", "total_mv", "total_share", "turnover_rate",
  "turnover_rate_f", "vol", "volatility_5d_pct", "volume_ratio",
]);
const discoveryFactorEntries = DATA_DICTIONARY_ENTRIES.filter(entry =>
  discoveryFactorFields.has(entry.field),
);
const discoveryFactorLabels = new Map(
  discoveryFactorEntries.map(entry => [entry.field, entry.label]),
);
const unsupportedDirectApplicationFields = new Set([
  "distance_from_5d_peak_pct",
  "max_drawdown_5d_pct",
  "positive_days_3",
  "return_5d_pct",
  "volatility_5d_pct",
]);
const untestedValidationReasons = new Set([
  "not_evaluated",
  "insufficient_validation_samples",
  "insufficient_validation_days",
  "insufficient_validation_effective_days",
  "insufficient_validation_securities",
  "insufficient_validation_effective_securities",
  "insufficient_validation_coverage",
  "insufficient_validation_baseline_coverage",
  "insufficient_significance_days",
]);

function percent(value: number, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

function validationReason(hypothesis: FactorHypothesis) {
  switch (hypothesis.validation_reason) {
    case "training_lift_not_positive": return "训练期未获得正提升";
    case "training_outcome_attrition_not_robust": return "训练期提升无法抵御标签缺失";
    case "insufficient_validation_samples": return "验证期有效事件数不足";
    case "insufficient_validation_days": return "验证期独立交易日不足";
    case "insufficient_validation_effective_days": return "验证期日期集中度折算有效日不足";
    case "insufficient_validation_securities": return "验证期独立证券数不足";
    case "insufficient_validation_effective_securities": return "验证期证券集中度折算有效数不足";
    case "insufficient_validation_coverage": return "验证期未来标签覆盖率不足";
    case "insufficient_validation_baseline_coverage": return "验证期可比基准标签覆盖率不足";
    case "validation_lift_not_positive": return "验证期未复现正提升";
    case "validation_outcome_attrition_not_robust": return "验证期提升无法抵御标签缺失";
    case "insufficient_significance_days": return "验证期不足 20 个日期集中度折算有效日";
    case "fdr_not_passed": return "未通过 10% BY-FDR";
    case "passed": return "训练与验证同向，且通过 10% BY-FDR";
    default: return "尚未完成预留验证";
  }
}

function significanceWasTested(hypothesis: FactorHypothesis) {
  return !untestedValidationReasons.has(hypothesis.validation_reason);
}

function supportShiftDescription(hypothesis: FactorHypothesis) {
  const trainingSupport = hypothesis.train_result?.rule_support_rate ?? 0;
  const validationSupport = hypothesis.val_result?.rule_support_rate ?? 0;
  if (validationSupport > trainingSupport) {
    return `验证期扩张至训练期的 ${percent(hypothesis.support_retention_ratio)}`;
  }
  if (validationSupport < trainingSupport) {
    return `验证期收缩至训练期的 ${percent(hypothesis.support_retention_ratio)}`;
  }
  return "验证期与训练期覆盖相同";
}

function thresholdSource(source: FactorHypothesis["threshold_source"]) {
  switch (source) {
    case "quantile": return "训练窗口分位阈值";
    case "observed_value": return "训练窗口离散实际值";
    case "mixed": return "训练窗口分位阈值与离散实际值";
    default: return "未记录的训练阈值来源";
  }
}

function directApplicationLimitation(formula: string) {
  const fields = formula.match(/[A-Za-z_][A-Za-z0-9_]*/g) ?? [];
  const unsupported = [...new Set(fields.filter(
    field => unsupportedDirectApplicationFields.has(field),
  ))];
  return unsupported.length > 0
    ? `分析页尚不能按相同的复权与连续交易日口径执行：${unsupported.join("、")}`
    : null;
}

function ruleTitle(formula: string) {
  const fields = [...new Set(
    (formula.match(/[A-Za-z_][A-Za-z0-9_]*/g) ?? [])
      .filter(token => discoveryFactorFields.has(token)),
  )];
  const labels = fields.map(field => discoveryFactorLabels.get(field) ?? field);
  return labels.length > 0 ? `${labels.join(" × ")}分位规律` : "候选分位规律";
}

function readableRuleExpression(formula: string) {
  return formula
    .replace(
      /\b[A-Za-z_][A-Za-z0-9_]*\b/g,
      token => discoveryFactorLabels.get(token) ?? token,
    )
    .replace(/\band\b/g, "且")
    .replace(/<=/g, "≤")
    .replace(/>=/g, "≥");
}

function MetricSet({ result }: { result: BacktestResult }) {
  const hasOutcomes = result.sample_count > 0;
  const hasBaseline = result.baseline_sample_count > 0;
  const hasLift = hasOutcomes && hasBaseline;
  return (
    <div className="rule-metrics">
      <span><small>收益超过 {percent(result.target_return)}</small><strong>{hasOutcomes ? percent(result.win_rate) : "—"}</strong></span>
      <span><small>可比基准命中率（含规则样本）</small><strong>{hasBaseline ? percent(result.baseline_win_rate) : "—"}</strong></span>
      <span><small>相对可比全体（N={result.baseline_sample_count}）</small><strong className={hasLift ? (result.win_rate_lift >= 0 ? "metric-positive" : "metric-negative") : undefined}>{hasLift ? `${result.win_rate_lift >= 0 ? "+" : ""}${percent(result.win_rate_lift)}` : "—"}</strong></span>
      <span><small>平均收益</small><strong>{hasOutcomes ? percent(result.mean_return, 2) : "—"}</strong></span>
      <span><small>收益起点</small><strong>信号日复权收盘</strong></span>
      <span><small>样本 / 交易日 / 证券</small><strong>{result.sample_count} / {result.trading_day_count} / {result.security_count}</strong></span>
      <span><small>日期集中度折算后有效交易日</small><strong>{result.effective_trading_day_count.toFixed(1)}</strong></span>
      <span><small>证券集中度折算后有效证券</small><strong>{result.effective_security_count.toFixed(1)}</strong></span>
      <span><small>最大单股事件占比</small><strong>{percent(result.max_security_event_share)}</strong></span>
      <span><small>最大单日事件占比</small><strong>{percent(result.max_signal_date_event_share)}</strong></span>
      <span><small>规则覆盖（可比事件 {result.eligible_sample_count}）</small><strong>{percent(result.rule_support_rate)}</strong></span>
      <span><small>标签覆盖</small><strong>{percent(result.outcome_coverage_rate)}</strong></span>
      <span><small>可比基准标签覆盖</small><strong>{percent(result.baseline_outcome_coverage_rate)}</strong></span>
    </div>
  );
}

function EventExamples({
  result,
  windowLabel,
}: {
  result: BacktestResult;
  windowLabel: string;
}) {
  if (result.event_examples.length === 0) return null;
  return (
    <details className="event-examples">
      <summary>核验最近 {result.event_examples.length} 条{windowLabel}命中事件</summary>
      <div className="table-scroll">
        <table>
          <thead><tr><th>信号日</th><th>证券</th><th>命中因子</th><th>结算日</th><th>未来收益</th></tr></thead>
          <tbody>{result.event_examples.map((example, index) => (
            <tr key={`${example.trade_date}-${example.ts_code ?? index}`}>
              <td>{example.trade_date}</td>
              <td>{example.ts_code ?? "—"}</td>
              <td>{Object.entries(example.factor_values).map(([field, value]) => `${discoveryFactorLabels.get(field) ?? field}=${value.toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`).join(" · ") || "—"}</td>
              <td>{example.future_trade_date ?? "—"}</td>
              <td>{percent(example.forward_return, 2)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </details>
  );
}

export function DiscoveryPage({ onApplyFormula }: DiscoveryPageProps) {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const [targetPool] = useState(params.get("dp_pool") || "A_SHARE");
  const [trainStart, setTrainStart] = useState(params.get("dp_ts") || "20240101");
  const [trainEnd, setTrainEnd] = useState(params.get("dp_te") || "20251231");
  const [valStart, setValStart] = useState(params.get("dp_vs") || "20260101");
  const [valEnd, setValEnd] = useState(params.get("dp_ve") || "20260630");
  const [prompt, setPrompt] = useState(params.get("dp_prompt") || "");
  const [forwardDays, setForwardDays] = useState(Number(params.get("dp_forward")) || 20);
  const [targetReturnPct, setTargetReturnPct] = useState(Number(params.get("dp_target")) || 0);
  const [minimumSamples, setMinimumSamples] = useState(Number(params.get("dp_samples")) || 30);
  const [minimumTradingDays, setMinimumTradingDays] = useState(Number(params.get("dp_days")) || 20);
  const [minimumSecurities, setMinimumSecurities] = useState(Number(params.get("dp_securities")) || 10);
  const [minimumOutcomeCoveragePct, setMinimumOutcomeCoveragePct] = useState(Number(params.get("dp_coverage")) || 95);
  const [maxConditions, setMaxConditions] = useState(Number(params.get("dp_depth")) || 2);
  const [factors, setFactors] = useState<string[]>(() => {
    const selected = params.get("dp_factors");
    return selected
      ? [...new Set(selected.split(","))].filter(field => discoveryFactorFields.has(field))
      : ["pe_ttm", "turnover_rate", "circ_mv"];
  });
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskStatus, setTaskStatus] = useState<DiscoveryTaskStatusResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [showAllFactors, setShowAllFactors] = useState(false);

  const availableFactors = discoveryFactorEntries;
  const leaderboard = taskStatus?.progress.leaderboard ?? [];
  const topTrainingRule = leaderboard[0] ?? null;
  const validationPassedCount = leaderboard.filter(
    hypothesis => hypothesis.validation_passed,
  ).length;
  const topValidationResult = topTrainingRule?.val_result ?? null;
  const topHasOutcomes = (topValidationResult?.sample_count ?? 0) > 0;
  const topHasBaseline = (topValidationResult?.baseline_sample_count ?? 0) > 0;
  const topSignificanceWasTested = topTrainingRule
    ? significanceWasTested(topTrainingRule)
    : false;
  const coverageFactors = [...new Set([
    ...Object.keys(taskStatus?.progress.training_factor_coverage ?? {}),
    ...Object.keys(taskStatus?.progress.validation_factor_coverage ?? {}),
  ])].sort();

  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    next.set("page", "discovery");
    next.set("dp_pool", targetPool);
    next.set("dp_ts", trainStart);
    next.set("dp_te", trainEnd);
    next.set("dp_vs", valStart);
    next.set("dp_ve", valEnd);
    next.set("dp_forward", String(forwardDays));
    next.set("dp_target", String(targetReturnPct));
    next.set("dp_samples", String(minimumSamples));
    next.set("dp_days", String(minimumTradingDays));
    next.set("dp_securities", String(minimumSecurities));
    next.set("dp_coverage", String(minimumOutcomeCoveragePct));
    next.set("dp_depth", String(maxConditions));
    if (prompt) next.set("dp_prompt", prompt); else next.delete("dp_prompt");
    if (factors.length) next.set("dp_factors", factors.join(",")); else next.delete("dp_factors");
    window.history.replaceState({}, "", `${window.location.pathname}?${next.toString()}`);
  }, [targetPool, trainStart, trainEnd, valStart, valEnd, forwardDays, targetReturnPct, minimumSamples, minimumTradingDays, minimumSecurities, minimumOutcomeCoveragePct, maxConditions, prompt, factors]);

  function toggleFactor(field: string) {
    setFactors(current => current.includes(field)
      ? current.filter(candidate => candidate !== field)
      : [...current, field]);
  }

  function selectAllFactors() {
    setFactors(availableFactors.map(factor => factor.field));
    setShowAllFactors(true);
  }

  function clearFactors() {
    setFactors([]);
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
        target_return_pct: targetReturnPct,
        minimum_samples: minimumSamples,
        minimum_trading_days: minimumTradingDays,
        minimum_securities: minimumSecurities,
        minimum_outcome_coverage_pct: minimumOutcomeCoveragePct,
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
        if (!active) return;
        if (!response.ok) {
          timer = window.setTimeout(poll, 2000);
          return;
        }
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
          <p>系统只在训练窗口生成并排序规则，锁定候选后才进行独立验证，验证结果不参与重排。特征来自信号日，标签是信号日复权收盘到未来交易日复权收盘的事件收益。</p>
        </div>
        <div className="research-guardrails">
          <span>未来标签隔离</span><span>最小样本约束</span><span>95% 概率区间</span>
        </div>
      </section>

      <section className="request-panel">
        <div className="section-heading"><span>01</span><h2>定义研究参数</h2></div>
        <form onSubmit={handleSubmit} className="discovery-form">
          <label className="wide-field">
            <span>研究备注（不参与计算）</span>
            <textarea value={prompt} onChange={event => setPrompt(event.target.value)} rows={2} placeholder="可选：记录这次研究的假设、背景或用途" />
          </label>
          <div className="discovery-settings-grid">
            <label><span>训练开始</span><input value={trainStart} onChange={event => setTrainStart(event.target.value)} inputMode="numeric" /></label>
            <label><span>训练结束</span><input value={trainEnd} onChange={event => setTrainEnd(event.target.value)} inputMode="numeric" /></label>
            <label><span>验证开始</span><input value={valStart} onChange={event => setValStart(event.target.value)} inputMode="numeric" /></label>
            <label><span>验证结束</span><input value={valEnd} onChange={event => setValEnd(event.target.value)} inputMode="numeric" /></label>
            <label><span>未来交易日</span><input type="number" min="1" max="60" value={forwardDays} onChange={event => setForwardDays(Number(event.target.value))} /></label>
            <label><span>目标收益（%）</span><input type="number" min="-100" max="1000" step="0.5" value={targetReturnPct} onChange={event => setTargetReturnPct(Number(event.target.value))} /></label>
            <label><span>最小样本数</span><input type="number" min="5" max="10000" value={minimumSamples} onChange={event => setMinimumSamples(Number(event.target.value))} /></label>
            <label><span>最少交易日</span><input type="number" min="2" max={minimumSamples} value={minimumTradingDays} onChange={event => setMinimumTradingDays(Number(event.target.value))} /></label>
            <label><span>最少证券数</span><input type="number" min="2" max={minimumSamples} value={minimumSecurities} onChange={event => setMinimumSecurities(Number(event.target.value))} /></label>
            <label><span>最低标签覆盖（%）</span><input type="number" min="50" max="100" step="1" value={minimumOutcomeCoveragePct} onChange={event => setMinimumOutcomeCoveragePct(Number(event.target.value))} /></label>
            <label><span>最多条件数</span><select value={maxConditions} onChange={event => setMaxConditions(Number(event.target.value))}><option value={1}>1 个</option><option value={2}>2 个</option></select></label>
          </div>
          <fieldset className="factor-selector">
            <legend>候选因子 <strong>{factors.length}</strong></legend>
            <div className="factor-selector-actions">
              <span>可搜索 {availableFactors.length} 个</span>
              <button type="button" className="expand-factors-button" onClick={selectAllFactors}>全选全部因子</button>
              <button type="button" className="expand-factors-button" onClick={clearFactors}>清空重选</button>
            </div>
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
            <p>连续因子搜索训练集 10% / 25% / 50% / 75% / 90% 分位阈值；有限取值不超过 10 个的离散因子会枚举全部实际阈值，避免罕见序列状态被宽分位遗漏。候选按相对可比基准提升的保守 95% 下界排序，同分时依次比较 5% 下行收益和中位收益，避免平均值被单个异常上涨拉高。{discoveryFactorFields.size} 个配对席位会先覆盖所有存在有效候选的因子，再补充反方向条件，仍有空位时每个因子方向按训练排名补入一个次优阈值；交互生成完成后，训练提升非正或无法抵御标签缺失的规则不会占用预留验证名额，相同训练样本的规则只保留排名最高者。</p>
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
          <div><small>隔离清除</small><strong>{taskStatus.progress.training_samples_purged.toLocaleString()}</strong></div>
          <div><small>验证样本</small><strong>{taskStatus.progress.validation_sample_count.toLocaleString()}</strong></div>
          <div><small>已评估候选</small><strong>{taskStatus.progress.candidates_evaluated}</strong></div>
          <div><small>入榜规则</small><strong>{taskStatus.progress.formulas_tested}</strong></div>
        </div>
        {coverageFactors.length > 0 && <>
          <p className="coverage-heading">因子可用率（训练 / 验证）</p>
          <div className="research-progress-grid factor-coverage-grid">
            {coverageFactors.map(factor => <div key={factor}>
              <small>{availableFactors.find(entry => entry.field === factor)?.label ?? factor}</small>
              <strong>{percent(taskStatus.progress.training_factor_coverage[factor] ?? 0)} / {percent(taskStatus.progress.validation_factor_coverage[factor] ?? 0)}</strong>
            </div>)}
          </div>
        </>}
        <p className="live-log">{taskStatus.progress.current_log || "等待任务开始…"}</p>
        {taskStatus.progress.training_samples_purged > 0 && <p className="research-caveat">已清除 {taskStatus.progress.training_samples_purged.toLocaleString()} 条未来结算日进入验证窗口的训练样本，防止标签泄漏。</p>}
        {taskStatus.error && <p className="discovery-error">{taskStatus.error.message}</p>}
      </section>}

      {topTrainingRule && <section className="results-panel discovery-summary-panel">
        <div className="section-heading"><span>03</span><h2>验证集摘要</h2></div>
        <p className="coverage-heading">本次研究配置（任务快照）</p>
        <div className="research-progress-grid research-config-grid">
          <div><small>训练窗口</small><strong>{taskStatus!.research_config.train_start} – {taskStatus!.research_config.train_end}</strong></div>
          <div><small>验证窗口</small><strong>{taskStatus!.research_config.val_start} – {taskStatus!.research_config.val_end}</strong></div>
          <div><small>未来收益周期</small><strong>{taskStatus!.research_config.forward_days} 个交易日</strong></div>
          <div><small>收益区间</small><strong>信号日复权收盘 → 第 {taskStatus!.research_config.forward_days} 个未来交易日复权收盘</strong></div>
          <div><small>样本 / 交易日 / 证券门槛</small><strong>{taskStatus!.research_config.minimum_samples} / {taskStatus!.research_config.minimum_trading_days} / {taskStatus!.research_config.minimum_securities}</strong></div>
          <div><small>标签覆盖 / 条件数</small><strong>{taskStatus!.research_config.minimum_outcome_coverage_pct}% / {taskStatus!.research_config.max_conditions}</strong></div>
        </div>
        <div className="headline-metrics">
          <div><small>训练榜首预留验证结论</small><strong className={topTrainingRule.validation_passed ? "metric-positive" : "metric-negative"}>{topTrainingRule.validation_passed ? "验证通过" : "未通过验证"}</strong><em>{validationPassedCount} / {leaderboard.length} 条入榜规律验证通过 · {validationReason(topTrainingRule)}</em></div>
          <div><small>超过 {percent(topValidationResult!.target_return)} 的概率</small><strong>{topHasOutcomes ? percent(topValidationResult!.win_rate) : "—"}</strong><em>{topHasOutcomes ? `HAC、${topValidationResult!.effective_trading_day_count.toFixed(1)} 个有效日 score 与缺失标签的保守包络：${percent(topValidationResult!.confidence_lower)} – ${percent(topValidationResult!.confidence_upper)}` : "无可观测验证结果，无法估计概率区间"}</em></div>
          <div><small>相对可比全体提升</small><strong className={topHasOutcomes && topHasBaseline ? (topValidationResult!.win_rate_lift >= 0 ? "metric-positive" : "metric-negative") : undefined}>{topHasOutcomes && topHasBaseline ? `${topValidationResult!.win_rate_lift >= 0 ? "+" : ""}${percent(topValidationResult!.win_rate_lift)}` : "—"}</strong><em>{topHasOutcomes && topHasBaseline ? `95% 区间 ${percent(topValidationResult!.lift_confidence_lower)} – ${percent(topValidationResult!.lift_confidence_upper)} · 可比基准命中率 ${percent(topValidationResult!.baseline_win_rate)}（含规则样本，N=${topValidationResult!.baseline_sample_count}）` : "规则或可比基准没有可观测结果，无法估计提升"}</em></div>
          <div><small>平均未来收益</small><strong>{topHasOutcomes ? percent(topValidationResult!.mean_return, 2) : "—"}</strong><em>{topHasOutcomes ? `中位数 ${percent(topValidationResult!.median_return, 2)}` : "无可观测验证结果"}</em></div>
          <div><small>5% 分位收益</small><strong className={topHasOutcomes ? (topValidationResult!.return_p05 >= 0 ? "metric-positive" : "metric-negative") : undefined}>{topHasOutcomes ? percent(topValidationResult!.return_p05, 2) : "—"}</strong><em>{topValidationResult!.sample_count} / {topValidationResult!.matched_sample_count} 个结果可观测</em></div>
          <div><small>提升检验 q-value</small><strong>{topSignificanceWasTested ? topTrainingRule.q_value.toFixed(3) : "未检验"}</strong><em>{topSignificanceWasTested ? `${topTrainingRule.fdr_family_size} 个盲测候选 · ${topTrainingRule.q_value <= 0.1 ? "通过 10% BY-FDR" : "未通过 10% BY-FDR"}` : `证据不足，按 p=1.000 计入 ${topTrainingRule.fdr_family_size} 个盲测候选的 BY-FDR`}</em></div>
        </div>
        <p className="research-caveat">研究池限定为沪深北六位证券代码，并排除沪市 900xxx 与深市 200xxx B 股。只有信号日存在官方日线和有效复权因子的证券才会生成事件：信号日停牌等没有可定义收盘信号的证券不会进入当天股票池；信号生成后，未来停牌、退市等造成的结果缺失则会保留在分母中，不会被静默当作不存在。排行榜名次在训练窗口内锁定，以下验证结果未参与重新排序；即使训练候选在验证期证据不足，也会保留原名次并明确显示失败原因，不会让后续规则替补上位。标记为“验证通过”的规则必须满足全部证据门槛、在训练和验证窗口相对基准均为正向提升，并通过验证集 10% BY-FDR。每条规则的基准只包含该规则引用因子均为有限值的可比较事件，避免把因子缺失本身误认为阈值规律；规则禁止引用未来收益、未来价格或未来日期字段。每条规则在两个窗口都必须同时满足事件数、独立交易日、独立证券数和未来标签覆盖率门槛，避免单一个股的长期历史被误称为市场规律；未来价格缺失会按缺失结果全部失败或全部成功的边界扩展概率区间。未来收益采用前后时点一致的复权收盘价计算；任一必需交易日的行情或复权因子整批缺失、或任一数据源请求失败时，研究会直接失败。估值接口成功但无记录时仍保留行情标签，对应估值因子按缺失处理。这是事件研究结果，不等同于可直接交易的组合回测；信号日完整行情通常只能在收盘后确认，因此结果不代表能够按同一收盘价成交。当前尚未计入涨跌停成交约束、手续费和持仓重叠，也没有足以计算真实组合最大回撤的逐日持仓净值路径，因此不会伪造回撤值。命中率置信区间取日期聚类 HAC、日期集中度折算后的 score 区间和缺失标签边界的保守包络；相对提升区间再取 HAC 提升区间和规则—基准概率区间差的保守包络，既处理相邻信号共享收益，也防止全胜、全败或少量结果缺失时显示虚假确定性；正式显著性仍由计入规则与可比较样本重叠的提升检验及 BY-FDR 判定。嵌套分位规则共享大量样本，因此 q-value 使用可控制任意依赖候选族的 Benjamini–Yekutieli 谐波惩罚。验证期少于 20 个日期集中度折算有效日时仍可探索，但显著性固定为 p=1，不能通过 FDR。FDR 分母包含所有进入盲测的冻结候选，包括验证证据不足但仍保留展示的规则。统计关联仍不代表因果关系。</p>
        <p className="research-caveat">“预留验证”只表示本次任务的搜索和排名没有读取该窗口结果。如果查看结果后又用同一验证窗口调整因子、日期、门槛或目标收益，该窗口已经参与人工选择，不能继续视为真正样本外证据；此时应改用更晚且从未查看的数据再次确认。</p>
        <p className="research-caveat">“可比基准”是所有规则因子均有有限值且结果可观测的事件全体，其中包含规则命中样本，并不是只由未命中事件组成的对照组。页面中的提升等于规则命中率减去这个可比全体命中率；当规则优于未命中事件时，这一口径会保守压低提升幅度，统计误差也按两者真实重叠关系计算。</p>
        <p className="research-caveat">验证通过还要求训练与验证窗口的标签缺失最坏情形提升均大于零：规则内缺失结果按全部失败、规则外的可比基准缺失结果按全部成功计算，同时保持规则样本与基准样本的真实重叠关系。最低标签覆盖门槛同时约束规则命中事件和完整的因子可比基准。用户配置的交易日门槛同时约束原始不同日期数和按事件权重折算的有效日期数；正式显著性另有至少 20 个有效日的固定底线，Student-t 自由度按有效日数向下取整后计算。证券门槛同样同时约束原始不同证券数和有效证券数，防止少数日期或个股贡献绝大多数命中事件。</p>
      </section>}

      {taskStatus && taskStatus.progress.leaderboard.length > 0 && <section className="results-panel">
        <div className="section-heading"><span>04</span><h2>候选规律排行榜</h2></div>
        <div className="rule-list">
          {taskStatus.progress.leaderboard.map((hypothesis, index) => {
            const applicationLimitation = directApplicationLimitation(hypothesis.formula);
            return <article className="rule-card" key={hypothesis.formula}>
            <div className="rule-rank">{String(index + 1).padStart(2, "0")}</div>
            <div className="rule-body">
              <div className="rule-heading"><div><h3>{ruleTitle(hypothesis.formula)}</h3><p className="rule-expression">{readableRuleExpression(hypothesis.formula)}</p><code>{hypothesis.formula}</code></div><div className="rule-actions"><strong className={hypothesis.validation_passed ? "metric-positive" : "metric-negative"}>{hypothesis.validation_passed ? "验证通过" : "未通过验证"}</strong><button type="button" disabled={applicationLimitation !== null} title={applicationLimitation ?? undefined} onClick={() => onApplyFormula(hypothesis.formula)}>{applicationLimitation ? "暂不可带入" : "带入分析页"}</button></div></div>
              {applicationLimitation && <p className="confidence-note">带入限制：{applicationLimitation}。研究结果仍可查看，但不会交给模型猜测执行口径。</p>}
              <p>阈值来源：{thresholdSource(hypothesis.threshold_source)}。规则按训练期相对提升的保守 95% 下界完成排名锁定；该下界同时包络日期聚类 HAC 区间与规则—基准 score 区间差，避免把零标准误误认为确定性。同分时优先选择 5% 下行分位和中位收益更高的规则。随后进入预留验证，验证结果未参与重新排序。</p>
              <div className="window-comparison">
                <div><b>训练窗口</b><MetricSet result={hypothesis.train_result!} /><EventExamples result={hypothesis.train_result!} windowLabel="训练" /></div>
                <div><b>预留验证</b><MetricSet result={hypothesis.val_result!} /><EventExamples result={hypothesis.val_result!} windowLabel="验证" /></div>
              </div>
              {hypothesis.val_result!.sample_count > 0 ? <>
                <p className="confidence-note">验证集收益超过 {percent(hypothesis.val_result!.target_return)} 的概率 95% 区间：{percent(hypothesis.val_result!.confidence_lower)} – {percent(hypothesis.val_result!.confidence_upper)}</p>
                <p className="confidence-note">验证集相对基准提升 95% 区间：{percent(hypothesis.val_result!.lift_confidence_lower)} – {percent(hypothesis.val_result!.lift_confidence_upper)}</p>
                <p className="confidence-note">标签缺失最坏—最好提升：{percent(hypothesis.val_result!.outcome_robust_lift_lower)} – {percent(hypothesis.val_result!.outcome_robust_lift_upper)}</p>
                <p className="confidence-note">验证集下行尾部：5% 分位收益 {percent(hypothesis.val_result!.return_p05, 2)}</p>
              </> : <p className="confidence-note">验证期暂无可观测结果，概率、收益分布与相对提升均无法估计；请结合因子覆盖率和验证判定排查。</p>}
              <p className="confidence-note">保守相对提升：{hypothesis.val_result!.sample_count > 0 ? percent(hypothesis.validation_score) : "—"} · 训练—验证提升差距：{hypothesis.val_result!.sample_count > 0 ? percent(hypothesis.generalization_gap) : "—"} · 规则覆盖差距：{percent(hypothesis.support_rate_gap)} · {supportShiftDescription(hypothesis)}</p>
              <p className="confidence-note">{significanceWasTested(hypothesis) ? `有限有效交易日 Student-t 提升检验 p-value：${hypothesis.p_value.toFixed(3)} · BY-FDR 校正 q-value：${hypothesis.q_value.toFixed(3)}（${hypothesis.fdr_family_size} 个盲测候选）` : `显著性未检验：证据门槛不足，按 p=1.000 计入 BY-FDR（${hypothesis.fdr_family_size} 个盲测候选，q=${hypothesis.q_value.toFixed(3)}）`}</p>
              <p className="confidence-note">验证判定：{validationReason(hypothesis)}</p>
            </div>
          </article>})}
        </div>
        <p className="research-caveat">覆盖保留比例是验证覆盖率除以训练覆盖率，用于识别规则适用人群的扩张或收缩；最大单股和最大单日事件占比分别用于识别虽满足证券数或交易日数、但仍被少数个股或市场日期主导的样本。有效交易日数按每日事件权重进行 Kish 折算，并用于 score 区间；日期越集中，该数值相对原始交易日数越低。这些指标是适用性诊断而非显著性通过门槛。“带入分析页”仅对分析页能够保持同一计算口径的字段开放，并会生成今日筛选请求；含内部复权序列特征的规则在同口径执行器完成前不会被交给模型猜测。提交普通规则后仍需经过查询规划，请在执行明细中核对公式字段、运算符和阈值是否保持一致。</p>
      </section>}
    </div>
  );
}
