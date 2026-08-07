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
const discoveryFactorFields = new Set([
  "amount", "circ_mv", "close", "dv_ratio", "dv_ttm", "float_share",
  "free_share", "open", "pb", "pct_chg", "pe", "pe_ttm", "ps",
  "ps_ttm", "total_mv", "total_share", "turnover_rate",
  "turnover_rate_f", "vol", "volume_ratio",
]);

function percent(value: number, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

function MetricSet({ result }: { result: BacktestResult }) {
  return (
    <div className="rule-metrics">
      <span><small>收益超过 {percent(result.target_return)}</small><strong>{percent(result.win_rate)}</strong></span>
      <span><small>相对基准（N={result.baseline_sample_count}）</small><strong className={result.win_rate_lift >= 0 ? "metric-positive" : "metric-negative"}>{result.win_rate_lift >= 0 ? "+" : ""}{percent(result.win_rate_lift)}</strong></span>
      <span><small>平均收益</small><strong>{percent(result.mean_return, 2)}</strong></span>
      <span><small>样本 / 交易日</small><strong>{result.sample_count} / {result.trading_day_count}</strong></span>
      <span><small>标签覆盖</small><strong>{percent(result.outcome_coverage_rate)}</strong></span>
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
  const [prompt, setPrompt] = useState(params.get("dp_prompt") || "");
  const [forwardDays, setForwardDays] = useState(Number(params.get("dp_forward")) || 20);
  const [targetReturnPct, setTargetReturnPct] = useState(Number(params.get("dp_target")) || 0);
  const [minimumSamples, setMinimumSamples] = useState(Number(params.get("dp_samples")) || 30);
  const [minimumTradingDays, setMinimumTradingDays] = useState(Number(params.get("dp_days")) || 20);
  const [minimumOutcomeCoveragePct, setMinimumOutcomeCoveragePct] = useState(Number(params.get("dp_coverage")) || 95);
  const [maxConditions, setMaxConditions] = useState(Number(params.get("dp_depth")) || 2);
  const [factors, setFactors] = useState<string[]>(() => {
    const selected = params.get("dp_factors");
    return selected ? [...new Set(selected.split(","))] : ["pe_ttm", "turnover_rate", "circ_mv"];
  });
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskStatus, setTaskStatus] = useState<DiscoveryTaskStatusResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [showAllFactors, setShowAllFactors] = useState(false);

  const availableFactors = DATA_DICTIONARY_ENTRIES.filter(entry =>
    discoveryFactorFields.has(entry.field),
  );
  const bestRule = taskStatus?.progress.leaderboard[0] ?? null;
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
    next.set("dp_coverage", String(minimumOutcomeCoveragePct));
    next.set("dp_depth", String(maxConditions));
    if (prompt) next.set("dp_prompt", prompt); else next.delete("dp_prompt");
    if (factors.length) next.set("dp_factors", factors.join(",")); else next.delete("dp_factors");
    window.history.replaceState({}, "", `${window.location.pathname}?${next.toString()}`);
  }, [targetPool, trainStart, trainEnd, valStart, valEnd, forwardDays, targetReturnPct, minimumSamples, minimumTradingDays, minimumOutcomeCoveragePct, maxConditions, prompt, factors]);

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
        target_return_pct: targetReturnPct,
        minimum_samples: minimumSamples,
        minimum_trading_days: minimumTradingDays,
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
          <p>系统只在训练窗口生成并排序规则，锁定候选后才进行独立验证，验证结果不参与重排。特征来自信号日，收益来自未来交易日。</p>
        </div>
        <div className="research-guardrails">
          <span>无同日收益泄漏</span><span>最小样本约束</span><span>95% 概率区间</span>
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
            <label><span>最少交易日</span><input type="number" min="2" max="1000" value={minimumTradingDays} onChange={event => setMinimumTradingDays(Number(event.target.value))} /></label>
            <label><span>最低标签覆盖（%）</span><input type="number" min="50" max="100" step="1" value={minimumOutcomeCoveragePct} onChange={event => setMinimumOutcomeCoveragePct(Number(event.target.value))} /></label>
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
            <p>搜索训练集 10% / 25% / 50% / 75% / 90% 分位阈值；配对池平衡因子与方向，相同训练样本的规则只保留排名最高者。</p>
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

      {bestRule && <section className="results-panel discovery-summary-panel">
        <div className="section-heading"><span>03</span><h2>验证集摘要</h2></div>
        <div className="headline-metrics">
          <div><small>独立验证结论</small><strong className={bestRule.validation_passed ? "metric-positive" : "metric-negative"}>{bestRule.validation_passed ? "验证通过" : "未通过验证"}</strong><em>训练排名已锁定</em></div>
          <div><small>超过 {percent(bestRule.val_result!.target_return)} 的概率</small><strong>{percent(bestRule.val_result!.win_rate)}</strong><em>HAC + {bestRule.val_result!.trading_day_count} 日 score 下限：{percent(bestRule.val_result!.confidence_lower)} – {percent(bestRule.val_result!.confidence_upper)}</em></div>
          <div><small>相对全样本提升</small><strong className={bestRule.val_result!.win_rate_lift >= 0 ? "metric-positive" : "metric-negative"}>{bestRule.val_result!.win_rate_lift >= 0 ? "+" : ""}{percent(bestRule.val_result!.win_rate_lift)}</strong><em>全样本 {percent(bestRule.val_result!.baseline_win_rate)} · N={bestRule.val_result!.baseline_sample_count}</em></div>
          <div><small>平均未来收益</small><strong>{percent(bestRule.val_result!.mean_return, 2)}</strong><em>中位数 {percent(bestRule.val_result!.median_return, 2)}</em></div>
          <div><small>5% 分位收益</small><strong className={bestRule.val_result!.return_p05 >= 0 ? "metric-positive" : "metric-negative"}>{percent(bestRule.val_result!.return_p05, 2)}</strong><em>{bestRule.val_result!.sample_count} / {bestRule.val_result!.matched_sample_count} 个结果可观测</em></div>
          <div><small>提升检验 q-value</small><strong>{bestRule.q_value.toFixed(3)}</strong><em>{bestRule.fdr_family_size} 个盲测候选 · {bestRule.q_value <= 0.1 ? "通过 10% FDR" : "未通过 10% FDR"}</em></div>
        </div>
        <p className="research-caveat">排行榜名次在训练窗口内锁定，以下验证结果未参与重新排序。每条规则在训练和验证窗口都必须同时满足事件数、独立交易日和未来标签覆盖率门槛；停牌等原因造成的未来价格缺失会保留在分母中，不会被静默当作不存在。未来收益采用前后时点一致的复权收盘价计算；任一必需交易日的行情或复权因子整批缺失、或任一数据源请求失败时，研究会直接失败。估值接口成功但无记录时仍保留行情标签，对应估值因子按缺失处理。这是事件研究结果，不等同于可直接交易的组合回测；当前尚未计入涨跌停成交约束、手续费和持仓重叠。置信区间取日期聚类 HAC 与独立交易日 score 区间的保守包络，既处理相邻信号共享收益，也防止全胜或全败样本显示虚假确定性；提升检验还计入规则与全样本基准的重叠。FDR 分母包含所有进入盲测的冻结候选，包括因验证样本不足而未入榜的规则。统计关联仍不代表因果关系。</p>
      </section>}

      {taskStatus && taskStatus.progress.leaderboard.length > 0 && <section className="results-panel">
        <div className="section-heading"><span>04</span><h2>候选规律排行榜</h2></div>
        <div className="rule-list">
          {taskStatus.progress.leaderboard.map((hypothesis, index) => <article className="rule-card" key={hypothesis.formula}>
            <div className="rule-rank">{String(index + 1).padStart(2, "0")}</div>
            <div className="rule-body">
              <div className="rule-heading"><div><h3>{hypothesis.description}</h3><code>{hypothesis.formula}</code></div><div className="rule-actions"><strong className={hypothesis.validation_passed ? "metric-positive" : "metric-negative"}>{hypothesis.validation_passed ? "验证通过" : "未通过验证"}</strong><button type="button" onClick={() => onApplyFormula(hypothesis.formula)}>用于今日筛选</button></div></div>
              <p>{hypothesis.reasoning}</p>
              <div className="window-comparison">
                <div><b>训练窗口</b><MetricSet result={hypothesis.train_result!} /></div>
                <div><b>独立验证</b><MetricSet result={hypothesis.val_result!} /></div>
              </div>
              <p className="confidence-note">验证集收益超过 {percent(hypothesis.val_result!.target_return)} 的概率 95% 区间：{percent(hypothesis.val_result!.confidence_lower)} – {percent(hypothesis.val_result!.confidence_upper)}</p>
              <p className="confidence-note">验证集下行尾部：5% 分位收益 {percent(hypothesis.val_result!.return_p05, 2)}</p>
              <p className="confidence-note">保守可信分：{hypothesis.validation_score.toFixed(3)} · 训练—验证差距：{percent(hypothesis.generalization_gap)}</p>
              <p className="confidence-note">相对基准提升检验 p-value：{hypothesis.p_value.toFixed(3)} · FDR 校正 q-value：{hypothesis.q_value.toFixed(3)}（{hypothesis.fdr_family_size} 个盲测候选）</p>
            </div>
          </article>)}
        </div>
      </section>}
    </div>
  );
}
