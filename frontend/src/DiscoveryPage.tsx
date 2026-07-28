import { useEffect, useState } from "react";
import { DATA_DICTIONARY_ENTRIES } from "./dataDictionary";
import { DiscoveryTaskRequest, DiscoveryTaskStatusResponse } from "./contracts";

interface DiscoveryPageProps {
  onApplyFormula: (formula: string) => void;
}

export function DiscoveryPage({ onApplyFormula }: DiscoveryPageProps) {
  const [targetPool, setTargetPool] = useState("A_SHARE");
  const [trainStart, setTrainStart] = useState("20250101");
  const [trainEnd, setTrainEnd] = useState("20250630");
  const [valStart, setValStart] = useState("20250701");
  const [valEnd, setValEnd] = useState("20251231");
  const [prompt, setPrompt] = useState("");
  const [factors, setFactors] = useState<string[]>(["pe_ttm", "turnover_rate"]);
  
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskStatus, setTaskStatus] = useState<DiscoveryTaskStatusResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const availableFactors = DATA_DICTIONARY_ENTRIES.filter(e => !["ts_code", "name", "trade_date", "end_date", "ann_date", "calculation_status"].includes(e.field));

  function toggleFactor(field: string) {
    setFactors(prev => prev.includes(field) ? prev.filter(f => f !== field) : [...prev, field]);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (factors.length === 0) {
      alert("请至少选择一个因子！");
      return;
    }
    setIsSubmitting(true);
    setTaskStatus(null);
    try {
      const payload: DiscoveryTaskRequest = {
        target_pool: targetPool,
        train_start: trainStart,
        train_end: trainEnd,
        val_start: valStart,
        val_end: valEnd,
        factors: factors,
        prompt: prompt,
        max_generations: 3
      };
      const res = await fetch("/api/discovery/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error("Failed to submit discovery task");
      const data = await res.json();
      setTaskId(data.task_id);
    } catch (err) {
      console.error(err);
      alert("任务提交失败");
    } finally {
      setIsSubmitting(false);
    }
  }

  useEffect(() => {
    if (!taskId) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/discovery/tasks/${taskId}`);
        if (!res.ok) return;
        const data = await res.json() as DiscoveryTaskStatusResponse;
        setTaskStatus(data);
        if (data.status === "succeeded" || data.status === "failed") {
          clearInterval(interval);
        }
      } catch (err) {
        console.error(err);
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [taskId]);

  return (
    <div className="discovery-page">
      <section className="request-panel">
        <div className="section-heading"><span>01</span><h2>任务指挥台</h2></div>
        <form onSubmit={handleSubmit} className="discovery-form">
          <label>
            <span>探索目标（Prompt）</span>
            <input type="text" value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="例如：寻找高胜率的低估值加反转策略" />
          </label>
          <div className="form-row">
            <label>
              <span>训练开始日期</span>
              <input type="text" value={trainStart} onChange={e => setTrainStart(e.target.value)} />
            </label>
            <label>
              <span>训练结束日期</span>
              <input type="text" value={trainEnd} onChange={e => setTrainEnd(e.target.value)} />
            </label>
            <label>
              <span>盲测开始日期</span>
              <input type="text" value={valStart} onChange={e => setValStart(e.target.value)} />
            </label>
            <label>
              <span>盲测结束日期</span>
              <input type="text" value={valEnd} onChange={e => setValEnd(e.target.value)} />
            </label>
          </div>
          <fieldset className="factor-selector">
            <legend>选择探索因子 (已选 {factors.length} 个)</legend>
            <div className="factor-grid">
              {availableFactors.slice(0, 30).map(f => (
                <label key={f.field} className="factor-checkbox">
                  <input type="checkbox" checked={factors.includes(f.field)} onChange={() => toggleFactor(f.field)} />
                  {f.label} <small>({f.field})</small>
                </label>
              ))}
              {availableFactors.length > 30 && <span>... 及更多 {availableFactors.length - 30} 个因子。</span>}
            </div>
          </fieldset>
          <button type="submit" disabled={isSubmitting || taskStatus?.status === "running"}>
            {isSubmitting ? "正在提交..." : taskStatus?.status === "running" ? "任务正在进行" : "开始挖掘策略"}
          </button>
        </form>
      </section>

      {taskId && taskStatus && (
        <section className="results-panel">
          <div className="section-heading"><span>02</span><h2>进化直播室</h2></div>
          <div className="evolution-dashboard">
            <p><strong>状态:</strong> {taskStatus.status === "running" ? "🧠 智能体正在思考并回测中..." : taskStatus.status === "succeeded" ? "✅ 挖掘完成" : "❌ 挖掘失败"}</p>
            <p><strong>当前代数:</strong> {taskStatus.progress.current_generation} / {taskStatus.progress.total_generations}</p>
            <p><strong>已测试公式:</strong> {taskStatus.progress.formulas_tested}</p>
            <pre className="live-log">{taskStatus.progress.current_log || "等待日志..."}</pre>
            {taskStatus.error && <p className="error-text">错误: {taskStatus.error.message}</p>}
          </div>
        </section>
      )}

      {taskStatus && taskStatus.progress.leaderboard && taskStatus.progress.leaderboard.length > 0 && (
        <section className="results-panel">
          <div className="section-heading"><span>03</span><h2>黄金规律荣誉榜 (Top Rules)</h2></div>
          <div className="table-scroll">
            <table className="leaderboard-table">
              <thead>
                <tr>
                  <th>排名</th>
                  <th>规律描述</th>
                  <th>公式 (Formula)</th>
                  <th>训练集表现</th>
                  <th>盲测表现</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {taskStatus.progress.leaderboard.map((hyp, idx) => (
                  <tr key={idx}>
                    <td>#{idx + 1}</td>
                    <td><strong>{hyp.description}</strong><br/><small>{hyp.reasoning}</small></td>
                    <td><code>{hyp.formula}</code></td>
                    <td>
                      {hyp.train_result ? (
                        <>胜率: {(hyp.train_result.win_rate * 100).toFixed(1)}%<br/>收益: {(hyp.train_result.mean_return * 100).toFixed(2)}%</>
                      ) : "-"}
                    </td>
                    <td>
                      {hyp.val_result ? (
                        <><strong>胜率: {(hyp.val_result.win_rate * 100).toFixed(1)}%</strong><br/>收益: {(hyp.val_result.mean_return * 100).toFixed(2)}%</>
                      ) : "-"}
                    </td>
                    <td>
                      <button type="button" onClick={() => onApplyFormula(hyp.formula)}>一键应用</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
