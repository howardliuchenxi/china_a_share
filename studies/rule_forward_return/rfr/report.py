"""Render REPORT.md from the stats table (all numbers two decimals)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

PCT_COLUMNS = (
    "mean_ret",
    "median_ret",
    "win_rate",
    "mean_excess",
    "net_excess",
    "is_excess",
    "oos_excess",
    "ci_low",
    "null95",
)


def _pct(value: float) -> str:
    if value is None or not np.isfinite(value):
        return "—"
    return f"{value * 100:.2f}%"


def _fmt(value, decimals: int = 0) -> str:
    if value is None or not np.isfinite(value):
        return "—"
    return f"{value:.{decimals}f}"


def _row_cells(row: pd.Series) -> str:
    cells = [
        str(int(row["n_hold"])),
        _fmt(row["n"]),
        _pct(row["mean_ret"]),
        _pct(row["mean_excess"]),
        _pct(row["net_excess"]),
        _pct(row["median_ret"]),
        _pct(row["win_rate"]),
        _pct(row["is_excess"]),
        _pct(row["oos_excess"]),
        _pct(row["ci_low"]),
        _pct(row["null95"]),
        str(row["verdict"]),
    ]
    return "| " + " | ".join(cells) + " |"


_TABLE_HEADER = (
    "| N | 事件数 | 平均收益 | 超额 | 净超额(扣10bp) | 中位 | 胜率 | IS超额 | OOS超额 | CI下界 | null95线 | 判定 |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def render_report(
    stats: pd.DataFrame,
    universe: int,
    dates: pd.DatetimeIndex,
    path: Optional[str] = None,
    *,
    portfolio_summary: Optional[pd.DataFrame] = None,
    portfolio_monthly: Optional[pd.DataFrame] = None,
    left_tail_event_summary: Optional[pd.DataFrame] = None,
    left_tail_annual: Optional[pd.DataFrame] = None,
    left_tail_portfolio_summary: Optional[pd.DataFrame] = None,
    left_tail_monthly: Optional[pd.DataFrame] = None,
    left_tail_yearly: Optional[pd.DataFrame] = None,
    improvement_event_summary: Optional[pd.DataFrame] = None,
    improvement_summary: Optional[pd.DataFrame] = None,
    improvement_monthly: Optional[pd.DataFrame] = None,
    improvement_regime: Optional[pd.DataFrame] = None,
) -> str:
    """Write the markdown report and return its text."""
    if path is None:
        import os

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "REPORT.md")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    span = f"{dates.min().date()} – {dates.max().date()}" if len(dates) else "—"

    lines: list = []
    portfolio_labels = {
        "gapdown2": "开盘低开≤-2%",
        "mom20_top10": "20日动量·池内前10%",
        "rev5_bot10": "5日反转·池内后10%买入",
        "volspike2": "量比≥2",
    }
    candidate_type_labels = {
        "composite": "复合规则",
        "ensemble": "组合",
    }
    lines.append("# 规则前向收益研究报告（美股）")
    lines.append("")
    lines.append(f"生成时间：{now} · 数据：Massive 日线（前复权） · 本地运行")
    lines.append("")
    lines.append("## 1. 执行摘要")
    lines.append("")
    lines.append(
        f"研究问题：满足给定规则的股票，T+1 开盘买入、持有 N 个交易日收盘卖出，"
        f"平均收益与相对同日流动池的 mean_excess 是多少，以及能否与随机选股区分。"
    )
    lines.append("")
    lines.append(f"- 面板：{universe} 只美股 · {len(dates)} 个交易日（{span}）")
    lines.append("- 池口径：滚动 20 日成交额中位数 ≥ 2000 万美元 且 收盘 ≥ 5 美元")
    lines.append("- 事件口径：T 收盘判定 → T+1 开盘买入 → T+N 收盘卖出；超额 = 事件收益 − 同池等权同窗收益")
    lines.append("- 显著性：月块 bootstrap 95%CI 下界 > 0 且 OOS 超额 > 同规模随机选股的 95 分位（null95）")
    lines.append("")

    survivors = stats[(stats["verdict"] == "过随机关") & (stats["n_hold"] == 5)]
    if len(survivors):
        lines.append("**过随机关的规则（N=5 中轴持有期）：**")
        lines.append("")
        for _, row in survivors.sort_values("oos_excess", ascending=False).iterrows():
            lines.append(
                f"- {row['label']}：平均收益 {_pct(row['mean_ret'])}，"
                f"超额 {_pct(row['mean_excess'])}（OOS {_pct(row['oos_excess'])}，"
                f"null95 {_pct(row['null95'])}）"
            )
    else:
        lines.append(
            "**N=5 持有期下没有规则同时通过 CI 与随机关。** 各规则的方向性与稳健性见主表。"
        )
    lines.append("")

    lines.append("## 2. 主表（每规则 × 持有期网格）")
    lines.append("")
    for spec_name in stats["rule"].drop_duplicates():
        block = stats[stats["rule"] == spec_name].sort_values("n_hold")
        first = block.iloc[0]
        lines.append(f"### {first['label']}（{spec_name}）")
        lines.append("")
        lines.append(_TABLE_HEADER)
        for _, row in block.iterrows():
            lines.append(_row_cells(row))
        lines.append("")

    if portfolio_summary is not None and not portfolio_summary.empty:
        lines.append("## 3. N=5 重叠持仓组合")
        lines.append("")
        lines.append(
            "组合使用 5 个轮换资金袖套；每日新袖套占初始组合的 20%，当日命中股等权，"
            "T+1 开盘买入、T+5 收盘退出。策略按买卖各 5bp 扣除完整往返 10bp，"
            "池基准采用同一入场日与持有期但不扣成本。"
        )
        lines.append("")
        lines.append(
            "| 规则 | 月数 | 累计净收益 | 池基准 | 累计净超额 | 最大回撤 | 月度胜率 | 正超额月 | 超额均值CI下界 | 判定 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, row in portfolio_summary.iterrows():
            lines.append(
                f"| {portfolio_labels.get(row['rule'], row['rule'])} | {int(row['months'])} | "
                f"{_pct(row['total_net_return'])} | {_pct(row['total_benchmark_return'])} | "
                f"{_pct(row['total_excess_return'])} | {_pct(row['max_drawdown'])} | "
                f"{_pct(row['monthly_win_rate'])} | {int(row['positive_excess_months'])}/{int(row['months'])} | "
                f"{_pct(row['ci_low'])} | {row['verdict']} |"
            )
        lines.append("")
        lines.append(
            "判定线：月度超额均值 bootstrap 95%CI 下界 > 0，且 24 个月中至少 16 个月超额为正。"
        )
        lines.append("")

    if portfolio_monthly is not None and not portfolio_monthly.empty:
        lines.append("## 4. 组合月度净收益与超额")
        lines.append("")
        for rule_name in portfolio_monthly["rule"].drop_duplicates():
            block = portfolio_monthly[portfolio_monthly["rule"] == rule_name]
            lines.append(
                f"### {portfolio_labels.get(rule_name, rule_name)}（{rule_name}）"
            )
            lines.append("")
            lines.append("| 月份 | 净收益 | 池基准 | 超额 | 月末净值 |")
            lines.append("|---|---|---|---|---|")
            for _, row in block.iterrows():
                lines.append(
                    f"| {row['month']} | {_pct(row['net_return'])} | "
                    f"{_pct(row['benchmark_return'])} | {_pct(row['excess_return'])} | "
                    f"{_fmt(row['strategy_nav'], 4)} |"
                )
            lines.append("")

    scenario_labels = {
        "baseline": "基线",
        "exclude_gap8": "剔除绝对缺口>8%",
        "cap_1pct": "聚合单票上限1%",
        "cap_2pct": "聚合单票上限2%",
        "cap_3pct": "聚合单票上限3%",
    }
    if left_tail_event_summary is not None and not left_tail_event_summary.empty:
        lines.append("## 5. gapdown2 左尾过滤：事件级移动")
        lines.append("")
        lines.append(
            "深缺口过滤仅作为对照，不替换主口径。事件级新条件仍须同时通过月块 CI 与匹配规模随机选股 null95 两关。"
        )
        lines.append("")
        lines.append(
            "| 方案 | 事件数 | 剔除 | 平均收益 | 超额 | OOS超额 | CI下界 | null95 | 胜率 | p5 | 最差 | 判定 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for _, row in left_tail_event_summary.iterrows():
            lines.append(
                f"| {scenario_labels.get(row['scenario'], row['scenario'])} | {int(row['n'])} | "
                f"{int(row['removed_events'])} | {_pct(row['mean_ret'])} | "
                f"{_pct(row['mean_excess'])} | {_pct(row['oos_excess'])} | "
                f"{_pct(row['ci_low'])} | {_pct(row['null95'])} | "
                f"{_pct(row['win_rate'])} | {_pct(row['p5_return'])} | "
                f"{_pct(row['worst_return'])} | {row['verdict']} |"
            )
        lines.append("")

    if left_tail_annual is not None and not left_tail_annual.empty:
        lines.append("### 事件级分年度稳定性")
        lines.append("")
        lines.append("| 方案 | 分段 | 事件数 | 平均收益 | 平均超额 | 胜率 | p5 | 最差 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for _, row in left_tail_annual.iterrows():
            lines.append(
                f"| {scenario_labels.get(row['scenario'], row['scenario'])} | {row['period']} | "
                f"{int(row['event_count'])} | {_pct(row['mean_return'])} | "
                f"{_pct(row['mean_excess'])} | {_pct(row['win_rate'])} | "
                f"{_pct(row['p5_return'])} | {_pct(row['worst_return'])} |"
            )
        lines.append("")

    if (
        left_tail_portfolio_summary is not None
        and not left_tail_portfolio_summary.empty
    ):
        lines.append("## 6. gapdown2 左尾治理：组合级代价收益")
        lines.append("")
        lines.append(
            "单票上限在每次入场时按全组合现有同票敞口计算；已有持仓不强制再平衡，受限资金留现金。"
        )
        lines.append("")
        lines.append(
            "| 方案 | 持仓事件 | 累计净收益 | 累计净超额 | 最大回撤 | 正超额月 | CI下界 | 被过滤 | 因上限未建仓 | 判定 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, row in left_tail_portfolio_summary.iterrows():
            lines.append(
                f"| {scenario_labels.get(row['scenario'], row['scenario'])} | {int(row['positions'])} | "
                f"{_pct(row['total_net_return'])} | {_pct(row['total_excess_return'])} | "
                f"{_pct(row['max_drawdown'])} | {int(row['positive_excess_months'])}/{int(row['months'])} | "
                f"{_pct(row['ci_low'])} | {int(row['filtered_signals'])} | "
                f"{int(row['capped_out_signals'])} | {row['verdict']} |"
            )
        lines.append("")

    if left_tail_yearly is not None and not left_tail_yearly.empty:
        lines.append("### 组合级分年度稳定性")
        lines.append("")
        lines.append("| 方案 | 分段 | 月数 | 净收益 | 池基准 | 超额 | 正超额月 |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, row in left_tail_yearly.iterrows():
            lines.append(
                f"| {scenario_labels.get(row['scenario'], row['scenario'])} | {row['period']} | "
                f"{int(row['months'])} | {_pct(row['net_return'])} | "
                f"{_pct(row['benchmark_return'])} | {_pct(row['excess_return'])} | "
                f"{int(row['positive_excess_months'])}/{int(row['months'])} |"
            )
        lines.append("")

    if left_tail_monthly is not None and not left_tail_monthly.empty:
        lines.append("### 组合月度超额矩阵")
        lines.append("")
        matrix = left_tail_monthly.pivot(
            index="month", columns="scenario", values="excess_return"
        )
        order = [key for key in scenario_labels if key in matrix.columns]
        matrix = matrix.reindex(columns=order)
        lines.append(
            "| 月份 | "
            + " | ".join(scenario_labels[column] for column in matrix.columns)
            + " |"
        )
        lines.append("|---|" + "---|" * len(matrix.columns))
        for month, row in matrix.iterrows():
            lines.append(
                f"| {month} | "
                + " | ".join(_pct(row[column]) for column in matrix.columns)
                + " |"
            )
        lines.append("")

    if improvement_event_summary is not None and not improvement_event_summary.empty:
        lines.append("## 7. 预登记复合规则：事件级两关")
        lines.append("")
        lines.append(
            "本轮固定三条复合规则后一次性评估，不根据结果调整阈值。"
            "它们仍须通过月块 CI 与匹配规模随机选股 null95 两关。"
        )
        lines.append("")
        lines.append(
            "| 候选 | 事件数 | 平均收益 | 超额 | IS超额 | OOS超额 | CI下界 | null95 | 胜率 | 判定 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, row in improvement_event_summary.iterrows():
            lines.append(
                f"| {row['label']} | {int(row['n'])} | {_pct(row['mean_ret'])} | "
                f"{_pct(row['mean_excess'])} | {_pct(row['is_excess'])} | "
                f"{_pct(row['oos_excess'])} | {_pct(row['ci_low'])} | "
                f"{_pct(row['null95'])} | {_pct(row['win_rate'])} | {row['verdict']} |"
            )
        lines.append("")

    if improvement_summary is not None and not improvement_summary.empty:
        lines.append("## 8. 六个改良候选：组合级结果")
        lines.append("")
        lines.append(
            "组合层候选使用四条已过事件级两关的底层规则；复合规则必须先过事件级两关，"
            "再过组合月度门槛。逆波动方案仅使用月初之前 60 个交易日，"
            "月度调仓另扣单边 5bp。共享上限方案在四规则 20 个袖套间共同执行 2% 入场敞口上限。"
        )
        lines.append("")
        lines.append(
            "| 候选 | 类型 | 月数 | 累计净收益 | 池基准 | 累计净超额 | 最大回撤 | 正超额月 | CI下界 | 事件关 | 组合关 | 最终 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for _, row in improvement_summary.iterrows():
            readiness = (
                "门槛通过·待新样本确认"
                if row["final_verdict"] == "通过"
                else "未通过"
            )
            lines.append(
                f"| {row['label']} | "
                f"{candidate_type_labels.get(row['candidate_type'], row['candidate_type'])} | "
                f"{int(row['months'])} | "
                f"{_pct(row['total_net_return'])} | {_pct(row['total_benchmark_return'])} | "
                f"{_pct(row['total_excess_return'])} | {_pct(row['max_drawdown'])} | "
                f"{int(row['positive_excess_months'])}/{int(row['months'])} | "
                f"{_pct(row['ci_low'])} | {row['event_verdict']} | "
                f"{row['verdict']} | {readiness} |"
            )
        lines.append("")
        lines.append(
            "重要：2024–2026 数据已经用于提出这些改良，所以即便门槛通过，也只是开发期结果，"
            "不能重新称为真正 OOS。最终采用需要更长历史或后续新增数据确认。"
        )
        lines.append("")

    if improvement_monthly is not None and not improvement_monthly.empty:
        lines.append("### 改良候选月度超额矩阵")
        lines.append("")
        label_map = (
            improvement_summary.set_index("candidate")["label"].to_dict()
            if improvement_summary is not None
            else {}
        )
        matrix = improvement_monthly.pivot(
            index="month", columns="rule", values="excess_return"
        )
        lines.append(
            "| 月份 | "
            + " | ".join(label_map.get(column, column) for column in matrix.columns)
            + " |"
        )
        lines.append("|---|" + "---|" * len(matrix.columns))
        for month, row in matrix.iterrows():
            lines.append(
                f"| {month} | "
                + " | ".join(_pct(row[column]) for column in matrix.columns)
                + " |"
            )
        lines.append("")

    if improvement_regime is not None and not improvement_regime.empty:
        lines.append("### 风险环境诊断（不作为追加候选）")
        lines.append("")
        lines.append(
            "risk_on 定义为信号日池内至少 50 只有 60 日收益，且其中位数大于 0；"
            "该分段只用于解释收益来源，不据此追加第七条规则。"
        )
        lines.append("")
        lines.append("| 规则 | 环境 | 事件数 | 平均收益 | 超额 | OOS超额 | CI下界 | p5 | 最差 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for _, row in improvement_regime.iterrows():
            lines.append(
                f"| {portfolio_labels.get(row['rule'], row['rule'])} | {row['regime']} | "
                f"{int(row['n'])} | {_pct(row['mean_ret'])} | {_pct(row['mean_excess'])} | "
                f"{_pct(row['oos_excess'])} | {_pct(row['ci_low'])} | "
                f"{_pct(row['p5_return'])} | {_pct(row['worst_return'])} |"
            )
        lines.append("")

    lines.append("## 9. 口径与局限")
    lines.append("")
    lines.append("- 幸存者偏差：股票池按近期流动性选入后回看两年，期间退市个股缺失；两年窗口内大中盘影响有限但存在")
    lines.append("- 交易成本：主表为毛收益；「净超额」列统一扣 10bp 往返成本，未建模滑点与冲击")
    lines.append("- 252 日类规则（动量 252、52 周新高、均线金叉）有效信号窗约为面板后半段一年")
    lines.append("- 停牌/缺 K 线事件跳过并计入 skipped；面板末端窗口不完整的事件同样跳过")
    lines.append(
        "- 组合回测沿用事件级缺失口径：退出日无收盘价的信号不纳入组合；"
        "持有期中途缺 K 线时以上一可得价格估值，不改变退出日"
    )
    lines.append(
        "- 个别极端事件（逼空/题材炒作，如样本内 SPCX 2026-06 单日 +582%）会拉动均值，"
        "读表时以中位数与超额列互为印证"
    )
    lines.append("- 免费数据档历史边界：2024-09-29 起")
    lines.append("")

    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text
