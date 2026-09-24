"""RRI-2 S8: render REPORT_V2.md (expanded vocabulary + near-miss review)."""
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

STUDY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(STUDY, "data")

FAMILY_NAMES = {"E1": "连续2次涨停（恰好2连板）", "E1_EXT": "连板≥3", "E2": "单日涨幅≥9.8% 且量比≥2"}


def _fmt(v, pct=False, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    if pct:
        return f"{v*100:.{nd}f}%"
    return f"{v:.{nd}f}"


def _parse_conds(s):
    return [tuple(x) for x in json.loads(s)] if isinstance(s, str) else \
        [tuple(c) for c in s]


def _render(conds):
    return " 且 ".join(f"{f}{op}{v:.4g}" for f, op, v in conds) if conds else "(全体)"


def main() -> None:
    lines = []
    ap = lines.append
    ap("# 反向规则归纳研究二期报告（扩词表 + 弱信号复核）")
    ap("")
    ap(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 分支 codex/technical-pattern-studies · 本地运行")
    ap("")
    ap("## 1. 一期结论回顾")
    ap("")
    ap("一期（REPORT.md）：63 因子词表、三路搜索、四关判据下 **0 条件幸存**，null 95 分位 1.745。")
    ap("遗留两项：①词表缺龙虎榜/资金流维度；②近似幸存条件（过①②③未过④）未经深度复核。二期补齐。")
    ap("")

    ap("## 2. 新词表（S6/S6b）")
    ap("")
    cov_p = os.path.join(DATA, "new_factor_coverage.json")
    if os.path.exists(cov_p):
        cov = json.load(open(cov_p))
        ap("新增 11 个锚日可知因子（当日盘后发布口径，与一期当日换手率口径一致）：")
        ap("")
        ap("| 因子 | 含义 | 事件行覆盖率 |")
        ap("|---|---|---|")
        desc = {
            "mf_net_ratio": "当日全单净流入/成交额",
            "mf_main_net_ratio": "当日主力（大单+特大单）净流入/成交额",
            "mf_elg_net_ratio": "当日特大单净流入/成交额",
            "mf_main_net_5d_to_circ_mv": "近5日主力净流入合计/流通市值",
            "mf_main_net_z20": "当日主力净流入相对前20日标准化",
            "on_top_list": "锚日是否上龙虎榜",
            "tl_net_amount_ratio": "龙虎榜净买入额/成交额",
            "tl_amount_rate_max": "龙虎榜上榜成交占比",
            "top_list_count_20d": "近20个交易日上榜次数",
            "inst_net_buy_ratio": "机构席位净买入额/成交额",
            "mf_main_net_5d_to_circ_mv ": "",
        }
        for k, v in cov.items():
            if not k.strip():
                continue
            ap(f"| {k} | {desc.get(k, desc.get(k.strip(), ''))} | {_fmt(v, pct=True, nd=1)} |")
        ap("")
    dd_p = os.path.join(DATA, "dropped_factors_v2.json")
    if os.path.exists(dd_p):
        dd = json.load(open(dd_p))
        ap(f"与一期 35 因子合并去冗余（Spearman |ρ|>0.8）后保留 **{len(dd['kept'])}** 个，"
           f"剔除 {len(dd['dropped'])} 个：{', '.join(dd['dropped'])}。")
    ap("")

    ap("## 3. 一期弱信号复核（S7）")
    ap("")
    nm_p = os.path.join(DATA, "near_miss_review.csv")
    if os.path.exists(nm_p):
        nm = pd.read_csv(nm_p)
        lc = [c for c in nm.columns if c.startswith(("oos_lift_1", "oos_lift_4"))]
        yc = ["oos_lift_2024", "oos_lift_2025", "oos_lift_2026"]
        ap(f"一期近似幸存条件共 {len(nm)} 条（E1 {int((nm.family=='E1').sum())} / "
           f"E1_EXT {int((nm.family=='E1_EXT').sum())} / E2 {int((nm.family=='E2').sum())}）。")
        ap("")
        ap(f"- 四种标签定义（10/40日 × 5%/15%）下 OOS Lift 全部 ≥1.10 的：**{int((nm[lc] >= 1.10).all(axis=1).sum())}** 条（标签不敏感的信号）。")
        ap(f"- 2024/2025/2026 逐年 OOS Lift 全部 ≥1.0 的：{int((nm[yc] >= 1.0).all(axis=1).sum())} 条；全部 ≥1.1 的：{int((nm[yc] >= 1.1).all(axis=1).sum())} 条。")
        ap(f"- 在 20 次 null 运行最大 OOS Lift 分布中的百分位：中位数 {_fmt(nm.pct_vs_null.median(), nd=0)}，最高 {nm.pct_vs_null.max():.0f}。")
        ap("")
        ap("### 复核后最值得关注的条件（按 null 百分位前 6）")
        ap("")
        ap("| 家族 | 条件 | OOS Lift(20d/10%) | 2024 | 2025 | 2026 | null百分位 | 判读 |")
        ap("|---|---|---|---|---|---|---|---|")
        for _, r in nm.nlargest(6, "pct_vs_null").iterrows():
            years = [r[c] for c in yc if np.isfinite(r[c])]
            if not years:
                verdict = "—"
            elif min(years) < 1.0:
                verdict = "**明显衰减**（末年<1）"
            elif min(years) >= 1.3:
                verdict = "逐年稳定"
            else:
                verdict = "逐年≥1 但趋弱"
            ap(f"| {r['family']} | {_render(_parse_conds(r['conditions']))[:60]} | "
               f"{_fmt(r['oos_lift_20d_10pct'])} | {_fmt(r['oos_lift_2024'])} | "
               f"{_fmt(r['oos_lift_2025'])} | {_fmt(r['oos_lift_2026'])} | "
               f"{r['pct_vs_null']:.0f} | {verdict} |")
        ap("")
        ap("**核心发现：null 百分位最高的条件（小成交额连板股）呈现清晰的年度衰减**"
           "（2024 → 2026 逐年走低，末年已 <1），属于风格暴露（小盘/微盘效应）而非可复用的规律；"
           "而逐年最稳定的条件（低开 + 量能不过热、距5日线不远）null 百分位仅 50–65，"
           "仍无法与选择效应噪声区分。二期维持一期结论：**无可证实的稳定规律**。")
        ap("")

    ap("## 4. 新词表下的搜索（S7b）")
    ap("")
    nd_p = os.path.join(DATA, "null_distribution_v2.csv")
    nc_p = os.path.join(DATA, "new_factor_contribution.json")
    if os.path.exists(nd_p):
        nd = pd.read_csv(nd_p)
        null95 = nd["max_oos_lift"].dropna().quantile(0.95)
        ap(f"新词表重跑 20 次 null（种子同一期）：null 95 分位 = **{_fmt(null95)}**"
           f"（一期 1.745）。词表扩大后选择效应略有变化，四关第④关以新线为准。")
        ap("")
    if os.path.exists(nc_p):
        contrib = json.load(open(nc_p))
        ap(f"q<0.10 的强候选中使用新因子的数量：{contrib['strong_candidates_using_new_factors']}。")
        ap("")
    surv_any = False
    for fam in ("E1", "E1_EXT", "E2"):
        p = os.path.join(DATA, f"conditions_v2_{fam}.parquet")
        if not os.path.exists(p):
            continue
        df = pd.read_parquet(p)
        if len(df):
            surv = df[df["survives"]]
            if len(surv):
                surv_any = True
                ap(f"### {FAMILY_NAMES[fam]} · 幸存条件（四关全过，null线为新词表线）")
                ap("")
                for _, r in surv.sort_values("oos_lift", ascending=False).iterrows():
                    ap(f"- `{_render(_parse_conds(r['conditions']))}`")
                    ap(f"  - IS Lift {_fmt(r['lift'])}（支持 {int(r['support'])}），"
                       f"OOS Lift **{_fmt(r['oos_lift'])}**（{int(r['oos_support'])}），"
                       f"q={_fmt(r['q_value'], nd=3)}，CI下界 {_fmt(r['boot_ci_lo'])}，"
                       f"平台CV {_fmt(r['platform_cv'])}，null百分位见 §3 方法。")
                ap("")
    if not surv_any:
        ap("**新词表下仍无条件通过全部四关。** 龙虎榜与资金流因子进入了部分强候选组合"
           "（见 new_factor_contribution.json），但没有把任何条件推过随机对照线——"
           "与一期一致，这是当前方法下的确定性结论。")
    ap("")

    ap("## 5. 局限声明（在期一基础上新增）")
    ap("")
    ap("- **盘后数据口径**：当日换手、当日龙虎榜、当日资金流在现实中收盘后才完整可得，"
       "本研究按「锚日可知（盘后）」处理——实际按此操作需以收盘价成交，存在执行滑点与挂单约束。")
    ap("- 龙虎榜覆盖面有限：仅触发披露规则的日子有记录，on_top_list 的信息含量受披露规则变化影响。")
    ap("- 资金流数据的单档划分（大单/特大单阈值）由数据商定义，不同供应商口径不可比。")
    ap("- 其余局限同一期（样本非独立、因子词表仍有限、北交所/B股排除、停牌处理）。")
    ap("")
    ap("## 6. 复现说明")
    ap("")
    ap("```bash")
    ap("cd studies/reverse_rule_induction")
    ap("./.venv/bin/python s6_probe.py         # 端点探针")
    ap("./.venv/bin/python s6_fetch.py         # top_list/top_inst/moneyflow 月度分片")
    ap("./.venv/bin/python -m pytest tests/ -q # 含新因子无未来函数测试（16 项）")
    ap("./.venv/bin/python s6b_build_pool.py   # factor_pool_v2.parquet")
    ap("./.venv/bin/python s7_review.py        # 一期近似幸存条件复核")
    ap("./.venv/bin/python s7b_search_v2.py    # 新词表 null 重跑 + 三路搜索")
    ap("./.venv/bin/python -m rri.report_v2   # 生成本报告")
    ap("```")
    ap("")
    ap("## 附录：二期执行检查点（追加部分）")
    ap("")
    ap("```")
    ck = open(os.path.join(STUDY, "CHECKPOINT.md")).read()
    idx = ck.find("RRI-2")
    ap(ck[idx:].rstrip() if idx >= 0 else ck[-3000:])
    ap("```")

    with open(os.path.join(STUDY, "REPORT_V2.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"REPORT_V2.md written ({len(lines)} lines)")


if __name__ == "__main__":
    main()
