"""S5: render REPORT.md from pipeline outputs (deterministic, rerunnable)."""
from __future__ import annotations

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

STUDY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(STUDY_DIR, "data")

FAMILY_NAMES = {"E1": "连续2次涨停（恰好2连板）", "E1_EXT": "连板≥3", "E2": "单日涨幅≥9.8% 且量比≥2"}


def _fmt(v, pct=False, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    if pct:
        return f"{v*100:.{nd}f}%"
    return f"{v:.{nd}f}"


def bn_m_card(conds: list[tuple[str, str, float]]) -> str:
    """Human-readable condition card in bn-m collection language."""
    parts = []
    for f, op, v in conds:
        op_txt = "不超过" if op == "<=" else "不低于"
        if "ratio" in f and abs(v - 1.0) < 0.5:
            parts.append(f"{f} {op_txt} {v:.3f}")
        elif f.startswith("distance_from") or "pct" in f:
            parts.append(f"{f} {op_txt} {v:.1f}%")
        elif "mv" in f or "share" in f or f in ("amount", "vol"):
            parts.append(f"{f} {op_txt} {v:,.0f}")
        else:
            parts.append(f"{f} {op_txt} {v:.3f}")
    return "；".join(parts) if parts else "(无附加条件)"


def _parse_conds(s):
    if isinstance(s, str):
        return [tuple(x) for x in json.loads(s)]
    return [tuple(c) for c in s]


def main() -> None:
    ck_path = os.path.join(STUDY_DIR, "CHECKPOINT.md")
    ck = open(ck_path).read() if os.path.exists(ck_path) else "(无检查点记录)"

    lines: list[str] = []
    ap = lines.append
    ap("# 反向规则归纳研究报告")
    ap("")
    ap(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 分支 codex/technical-pattern-studies · 本地运行")
    ap("")
    ap("## 1. 执行摘要")
    ap("")
    ap("研究问题：给定「结果规则」（事件触发 + 未来表现标签），能否自动挖出正样本在触发时刻可知的条件组合，并用时间外数据验证其可靠性。")
    ap("")

    # panel + events stats from checkpoint
    def ck_val(key: str, stage_prefix: str):
        for ln in ck.splitlines():
            if stage_prefix in ln and key in ln:
                try:
                    js = ln.split(": ", 1)[1]
                    return json.loads(js).get(key)
                except Exception:
                    return None
        return None

    rows = ck_val("rows", "S0 panel")
    codes = ck_val("codes", "S0 panel")
    dates = ck_val("dates", "S0 panel")
    first = ck_val("first", "S0 panel")
    last = ck_val("last", "S0 panel")
    ap(f"- 数据面板：{rows:,} 行 × {codes} 只股票 × {dates} 个交易日（{first}–{last}），沪深A股、含退市股、不含北交所与B股。")
    ap("- 退市股行情可达性：已验证（如 601558.SH 退市前数据完整），**无幸存者偏差数据缺口**。")
    for fam in ("E1", "E1_EXT", "E2"):
        raw = ck_val("raw_events", f"S1 {fam}]")
        valid = ck_val("valid_labels", f"S1 {fam}]")
        pr = ck_val("positive_rate", f"S1 {fam}]")
        if raw is not None:
            ap(f"- {FAMILY_NAMES[fam]}：触发 {raw:,} 次，标签可用 {valid:,}，正样本率 { _fmt(pr, pct=True) }（20日超额≥+10%）。")
    null95 = ck_val("null_95", "S4 null distribution")
    if null95:
        ap(f"- 随机对照（20 次全流程 null 运行）的 OOS Lift 95 分位：**{_fmt(null95)}** —— 真实条件的 OOS Lift 必须超过该值才算打赢噪声。")
    n_surv = ck_val("survivors_total", "S4 done")
    ap(f"- **幸存条件（四关全过）：{n_surv if n_surv is not None else '见下表'} 条。**")
    ap("")

    ap("## 2. 幸存条件表（四关全过）")
    ap("")
    ap("四关判据：① OOS Lift ≥ 1.10；② BH-FDR q<0.10 且按月块 bootstrap 95%CI 下界 > 1.0；③ 阈值±20%扰动下 Lift 变异 <30%（平台非尖峰）；④ OOS Lift > null 95 分位。")
    ap("")
    survivors_all = []
    for fam in ("E1", "E1_EXT", "E2"):
        p = os.path.join(DATA_DIR, f"conditions_{fam}.parquet")
        if os.path.exists(p):
            df = pd.read_parquet(p)
            if len(df):
                df["family"] = fam
                survivors_all.append(df)
    if survivors_all:
        allc = pd.concat(survivors_all, ignore_index=True)
        surv = allc[allc["survives"]] if "survives" in allc else allc.iloc[0:0]
    else:
        surv = pd.DataFrame()
    if len(surv):
        for _, r in surv.sort_values("oos_lift", ascending=False).iterrows():
            conds = _parse_conds(r["conditions"])
            ap(f"### {FAMILY_NAMES.get(r['family'], r['family'])} · {r['method']}")
            ap("")
            ap(f"**条件**：{bn_m_card(conds)}")
            ap("")
            ap(f"| 指标 | IS(2016–2023) | OOS(2024–) |")
            ap(f"|---|---|---|")
            ap(f"| 支持度 | {int(r['support'])} ({_fmt(r['support_pct'], pct=True, nd=1)}) | {int(r['oos_support'])} |")
            ap(f"| 命中率 | {_fmt(r['hit_rate'], pct=True)} | — |")
            ap(f"| 基线率 | {_fmt(r['baseline_rate'], pct=True)} | — |")
            ap(f"| Lift | {_fmt(r['lift'])} | **{_fmt(r['oos_lift'])}** |")
            ap(f"| q值 (BH) | {_fmt(r['q_value'], nd=3)} | — |")
            ap(f"| bootstrap CI | [{_fmt(r['boot_ci_lo'])}, {_fmt(r['boot_ci_hi'])}] | — |")
            ap(f"| 平台CV | {_fmt(r['platform_cv'])} | — |")
            ap("")
            ap(f"**bn-m 条件卡**：`{r['family']}` 事件后 20 个交易日超额收益≥+10% 的历史提升 —— 附加条件 `{render_condition_safe(conds)}`")
            ap("")
    else:
        ap("**本轮无条件通过全部四关。** 这是合法且确定的结论：在当前因子词表与样本下，未找到时间外稳健的规律。各家族头部候选见 §3。")
        near = allc[(allc["q_value"] < 0.10) & (allc["oos_lift"] >= 1.10)
                    & (allc["boot_ci_lo"] > 1.0) & (allc["platform_cv"] < 0.30)] \
            if len(allc) else pd.DataFrame()
        if len(near):
            ap("")
            ap("### 差一关的条件（通过①②③，未过④随机对照）")
            ap("")
            ap("以下条件方向一致、时间外稳健、阈值是平台，但 OOS Lift 未超过随机对照 95 分位（1.75），")
            ap("即**无法与纯选择效应产生的噪声区分开**，不能视为已证实的规律：")
            ap("")
            ap("| 家族 | 条件 | IS Lift | OOS Lift | CI下界 | 平台CV | 距null线 |")
            ap("|---|---|---|---|---|---|---|")
            for _, r in near.nsmallest(6, "q_value").iterrows():
                c = _parse_conds(r["conditions"])
                ap(f"| {r['family']} | {render_condition_safe(c)[:70]} | {_fmt(r['lift'])} | "
                   f"{_fmt(r['oos_lift'])} | {_fmt(r['boot_ci_lo'])} | {_fmt(r['platform_cv'])} | "
                   f"{_fmt(null95 - r['oos_lift'])} |")
    ap("")

    ap("## 3. 各家族候选头部（未幸存，如实呈现）")
    ap("")
    if survivors_all:
        allc2 = pd.concat(survivors_all, ignore_index=True)
        for fam in ("E1", "E1_EXT", "E2"):
            df = allc2[allc2["family"] == fam]
            if not len(df):
                continue
            cand = df.copy()
            cand["_render"] = cand["conditions"].apply(
                lambda c: render_condition_safe(_parse_conds(c)))
            cand = cand.sort_values("q_value").drop_duplicates(
                ["support", "hits"]).nsmallest(8, "q_value")
            top = cand
            ap(f"### {FAMILY_NAMES[fam]}（按 q 值前 8）")
            ap("")
            ap("| 方法 | 条件 | 支持 | IS Lift | q值 | OOS Lift | 平台CV | CI下界 | 幸存 |")
            ap("|---|---|---|---|---|---|---|---|---|")
            for _, r in top.iterrows():
                conds = _parse_conds(r["conditions"])
                ap(f"| {r['method']} | {render_condition_safe(conds)} | {int(r['support'])} | "
                   f"{_fmt(r['lift'])} | {_fmt(r['q_value'], nd=3)} | {_fmt(r.get('oos_lift'))} | "
                   f"{_fmt(r.get('platform_cv'))} | {_fmt(r.get('boot_ci_lo'))} | "
                   f"{'✅' if r.get('survives') else '—'} |")
            ap("")
    ap("## 4. 三方法对比")
    ap("")
    if survivors_all:
        allc3 = pd.concat(survivors_all, ignore_index=True)
        ap("| 方法 | 候选数 | q<0.10 数 | 中位 IS Lift | 中位 OOS Lift |")
        ap("|---|---|---|---|---|")
        for m in ("M1", "M2", "M3"):
            d = allc3[allc3["method"] == m]
            if not len(d):
                ap(f"| {m} | 0 | — | — | — |")
                continue
            ap(f"| {m} | {len(d)} | {int((d['q_value']<0.10).sum())} | "
               f"{_fmt(d['lift'].median())} | {_fmt(d['oos_lift'].median())} |")
        ap("")
        ap("M1=分位数单条件枚举+定量化阈值；M2=深度3决策树路径（组合条件）；M3=WRAcc束搜索（子群发现）。")
    ap("")
    ap("## 5. 敏感性（幸存条件在其他标签定义下）")
    ap("")
    sens_p = os.path.join(DATA_DIR, "sensitivity_survivors.csv")
    sens = None
    if os.path.exists(sens_p) and os.path.getsize(sens_p) > 1:
        try:
            sens = pd.read_csv(sens_p)
        except pd.errors.EmptyDataError:
            sens = None
        if len(sens):
            ap("| 家族 | 条件 | 窗口 | 阈值 | IS Lift | OOS Lift |")
            ap("|---|---|---|---|---|---|")
            for _, r in sens.iterrows():
                ap(f"| {r['family']} | {r['conditions'][:60]}… | {int(r['window'])}d | "
                   f"{_fmt(r['threshold'], pct=True)} | {_fmt(r['is_lift'])} | {_fmt(r['oos_lift'])} |")
            ap("")
        else:
            ap("无幸存条件，跳过。")
    ap("## 6. 局限声明")
    ap("")
    ap("- **样本非独立**：同股多次触发、月份聚集 —— 已用按月块 bootstrap 缓解，但仍非完全独立。")
    ap("- **因子词表有限**：只能找到 63 个注册因子（54 产品口径 + 9 锚相对）能表达的规律；词表外因子（如龙虎榜、资金流）不可见。")
    ap("- **北交所与B股排除**：涨停口径差异大，避免污染。")
    ap("- **标签窗口内停牌**：用个股自身 20 个交易日，长停牌股票的前瞻窗口拉长（已在标签器处理并在事件表标注）。")
    ap("- **数据源**：tushare 单一来源，未与东财/同花顺三角验证涨停价（整数分位运算已保证与交易所一致的舍入）。")
    ap("- **过拟合防线**：IS/OOS 时间切分 + BH-FDR + 块 bootstrap + 平台性 + 随机对照四重防线；但历史规律不保证未来有效。")
    ap("")
    ap("## 7. 复现说明")
    ap("")
    ap("```bash")
    ap("cd studies/reverse_rule_induction")
    ap("./.venv/bin/python s0_fetch.py          # 数据快照（月度分片 parquet，可断点续抓）")
    ap("./.venv/bin/python -m pytest tests/ -q # 方法自证（合成规律找回 + 无未来函数 + 交易所舍入）")
    ap("./.venv/bin/python -m rri.pipeline     # S1–S4 全流程（CHECKPOINT.md 记录各阶段）")
    ap("./.venv/bin/python -m rri.report       # 生成本报告")
    ap("```")
    ap("")
    ap("关键产物：`data/conditions_{E1,E1_EXT,E2}.parquet`（全部候选条件+指标）、`data/events_*.parquet`（事件表+标签）、`data/factor_pool.parquet`（因子矩阵）、`data/null_distribution.csv`、`data/manual_verification_sample.csv`。")
    ap("")
    ap("## 附录：执行检查点")
    ap("")
    ap("```")
    runs = ck.split("- [")
    last = runs[-1] if len(runs) > 1 else ck
    body = "- [" + last
    body = body[: body.rfind("S4 done") + 200 if "S4 done" in body else len(body)]
    ap(body.rstrip())
    ap("```")

    with open(os.path.join(STUDY_DIR, "REPORT.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"REPORT.md written ({len(lines)} lines)")


def render_condition_safe(conds):
    from rri.search import render_condition
    return render_condition(conds)


if __name__ == "__main__":
    main()
