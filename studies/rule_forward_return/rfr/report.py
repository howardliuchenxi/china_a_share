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
) -> str:
    """Write the markdown report and return its text."""
    if path is None:
        import os

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "REPORT.md")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    span = f"{dates.min().date()} – {dates.max().date()}" if len(dates) else "—"

    lines: list = []
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

    lines.append("## 3. 口径与局限")
    lines.append("")
    lines.append("- 幸存者偏差：股票池按近期流动性选入后回看两年，期间退市个股缺失；两年窗口内大中盘影响有限但存在")
    lines.append("- 交易成本：主表为毛收益；「净超额」列统一扣 10bp 往返成本，未建模滑点与冲击")
    lines.append("- 252 日类规则（动量 252、52 周新高、均线金叉）有效信号窗约为面板后半段一年")
    lines.append("- 停牌/缺 K 线事件跳过并计入 skipped；面板末端窗口不完整的事件同样跳过")
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
