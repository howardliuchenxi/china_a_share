# 反向规则归纳研究二期报告（扩词表 + 弱信号复核）

生成时间：2026-09-24 12:18 · 分支 codex/technical-pattern-studies · 本地运行

## 1. 一期结论回顾

一期（REPORT.md）：63 因子词表、三路搜索、四关判据下 **0 条件幸存**，null 95 分位 1.745。
遗留两项：①词表缺龙虎榜/资金流维度；②近似幸存条件（过①②③未过④）未经深度复核。二期补齐。

## 2. 新词表（S6/S6b）

新增 11 个锚日可知因子（当日盘后发布口径，与一期当日换手率口径一致）：

| 因子 | 含义 | 事件行覆盖率 |
|---|---|---|
| mf_net_ratio | 当日全单净流入/成交额 | 99.9% |
| mf_main_net_ratio | 当日主力（大单+特大单）净流入/成交额 | 99.9% |
| mf_elg_net_ratio | 当日特大单净流入/成交额 | 99.9% |
| mf_main_net_z20 | 当日主力净流入相对前20日标准化 | 96.1% |
| on_top_list | 锚日是否上龙虎榜 | 100.0% |
| tl_net_amount_ratio | 龙虎榜净买入额/成交额 | 9.4% |
| tl_amount_rate_max | 龙虎榜上榜成交占比 | 9.4% |
| top_list_count_20d | 近20个交易日上榜次数 | 99.2% |
| inst_net_buy_ratio | 机构席位净买入额/成交额 | 4.2% |
| mf_main_net_5d_to_circ_mv | 近5日主力净流入合计/流通市值 | 91.0% |

与一期 35 因子合并去冗余（Spearman |ρ|>0.8）后保留 **44** 个，剔除 29 个：amount_5d_to_20d_avg_ratio, amount_to_20d_avg_ratio, amount_to_5d_avg_ratio, base_day_amplitude_pct, close_to_base_close_ratio, close_to_base_high_ratio, distance_from_20d_ma_pct, dv_ratio, float_share, intraday_range_pct, intraday_return_pct, max_drawdown_5d_pct, mf_main_net_ratio, open, pe, ps, return_10d_pct, return_20d_pct, return_5d_pct, total_mv, total_share, turnover_5d_avg_pct, turnover_5d_to_20d_avg_ratio, turnover_rate_f, turnover_to_20d_avg_ratio, turnover_to_5d_avg_ratio, volatility_10d_pct, volume_ratio, volume_to_5d_avg_ratio。

## 3. 一期弱信号复核（S7）

一期近似幸存条件共 210 条（E1 105 / E1_EXT 85 / E2 20）。

- 四种标签定义（10/40日 × 5%/15%）下 OOS Lift 全部 ≥1.10 的：**157** 条（标签不敏感的信号）。
- 2024/2025/2026 逐年 OOS Lift 全部 ≥1.0 的：154 条；全部 ≥1.1 的：106 条。
- 在 20 次 null 运行最大 OOS Lift 分布中的百分位：中位数 40，最高 95。

### 复核后最值得关注的条件（按 null 百分位前 6）

| 家族 | 条件 | OOS Lift(20d/10%) | 2024 | 2025 | 2026 | null百分位 | 判读 |
|---|---|---|---|---|---|---|---|
| E1_EXT | amount<=1.119e+04 | 1.74 | 2.81 | 1.36 | 0.66 | 95 | **明显衰减**（末年<1） |
| E1_EXT | amount<=3.556e+04 | 1.67 | 2.60 | 1.57 | 0.80 | 90 | **明显衰减**（末年<1） |
| E1 | distance_from_5d_ma_pct<=10.57 | 1.65 | 2.23 | 1.53 | 1.35 | 85 | 逐年稳定 |
| E1_EXT | return_20d_before_base_pct<=-10.97 | 1.64 | 2.61 | 1.39 | 1.09 | 85 | 逐年≥1 但趋弱 |
| E1_EXT | turnover_rate<=12.96 | 1.64 | 2.17 | 1.77 | 1.04 | 85 | 逐年≥1 但趋弱 |
| E1_EXT | distance_from_10d_ma_pct<=20.86 | 1.66 | 2.37 | 1.38 | 1.31 | 85 | 逐年稳定 |

**核心发现：null 百分位最高的条件（小成交额连板股）呈现清晰的年度衰减**（2024 → 2026 逐年走低，末年已 <1），属于风格暴露（小盘/微盘效应）而非可复用的规律；而逐年最稳定的条件（低开 + 量能不过热、距5日线不远）null 百分位仅 50–65，仍无法与选择效应噪声区分。二期维持一期结论：**无可证实的稳定规律**。

## 4. 新词表下的搜索（S7b）

新词表重跑 20 次 null（种子同一期）：null 95 分位 = **2.15**（一期 1.745）。词表扩大后选择效应略有变化，四关第④关以新线为准。

q<0.10 的强候选中使用新因子的数量：{'E1': 47, 'E1_EXT': 33, 'E2': 32}。

**新词表下仍无条件通过全部四关。** 龙虎榜与资金流因子进入了部分强候选组合（见 new_factor_contribution.json），但没有把任何条件推过随机对照线——与一期一致，这是当前方法下的确定性结论。

## 5. 局限声明（在期一基础上新增）

- **盘后数据口径**：当日换手、当日龙虎榜、当日资金流在现实中收盘后才完整可得，本研究按「锚日可知（盘后）」处理——实际按此操作需以收盘价成交，存在执行滑点与挂单约束。
- 龙虎榜覆盖面有限：仅触发披露规则的日子有记录，on_top_list 的信息含量受披露规则变化影响。
- 资金流数据的单档划分（大单/特大单阈值）由数据商定义，不同供应商口径不可比。
- 其余局限同一期（样本非独立、因子词表仍有限、北交所/B股排除、停牌处理）。

## 6. 复现说明

```bash
cd studies/reverse_rule_induction
./.venv/bin/python s6_probe.py         # 端点探针
./.venv/bin/python s6_fetch.py         # top_list/top_inst/moneyflow 月度分片
./.venv/bin/python -m pytest tests/ -q # 含新因子无未来函数测试（16 项）
./.venv/bin/python s6b_build_pool.py   # factor_pool_v2.parquet
./.venv/bin/python s7_review.py        # 一期近似幸存条件复核
./.venv/bin/python s7b_search_v2.py    # 新词表 null 重跑 + 三路搜索
./.venv/bin/python -m rri.report_v2   # 生成本报告
```

## 附录：二期执行检查点（追加部分）

```
": 1.708, "secs": 13}
- [2026-09-23 12:58:24] S4 null 19/20: {"max_oos_lift": 1.2079, "secs": 13}
- [2026-09-23 12:58:37] S4 null 20/20: {"max_oos_lift": 1.4891, "secs": 13}
- [2026-09-23 12:58:37] S4 null distribution: {"null_95": 1.7453, "completed_runs": 20}
- [2026-09-23 12:59:02] S3/S4 E1: {"is_events": 8119, "oos_events": 3975, "is_positive": 1156, "oos_positive": 629, "candidates": 378, "survivors": 0}
- [2026-09-23 12:59:20] S3/S4 E1_EXT: {"is_events": 5186, "oos_events": 2160, "is_positive": 741, "oos_positive": 334, "candidates": 360, "survivors": 0}
- [2026-09-23 13:00:29] S3/S4 E2: {"is_events": 34363, "oos_events": 16065, "is_positive": 5815, "oos_positive": 2565, "candidates": 379, "survivors": 0}
- [2026-09-23 13:00:34] S4 done: {"survivors_total": 0, "elapsed_min": 10.6}
- [2026-09-24 12:08:10] S6b dedup v2: {"kept": 44, "dropped": 29}
- [2026-09-24 12:10:27] S4 null 1/20: {"max_oos_lift": 1.1845, "secs": 15}
- [2026-09-24 12:10:40] S4 null 2/20: {"max_oos_lift": 1.2753, "secs": 14}
- [2026-09-24 12:10:54] S4 null 3/20: {"max_oos_lift": 1.4829, "secs": 14}
- [2026-09-24 12:11:07] S4 null 4/20: {"max_oos_lift": 1.2397, "secs": 13}
- [2026-09-24 12:11:21] S4 null 5/20: {"max_oos_lift": 1.3653, "secs": 13}
- [2026-09-24 12:11:34] S4 null 6/20: {"max_oos_lift": 1.6678, "secs": 13}
- [2026-09-24 12:11:48] S4 null 7/20: {"max_oos_lift": 1.293, "secs": 14}
- [2026-09-24 12:12:01] S4 null 8/20: {"max_oos_lift": 1.5126, "secs": 13}
- [2026-09-24 12:12:14] S4 null 9/20: {"max_oos_lift": 1.607, "secs": 13}
- [2026-09-24 12:12:28] S4 null 10/20: {"max_oos_lift": 2.1375, "secs": 13}
- [2026-09-24 12:12:41] S4 null 11/20: {"max_oos_lift": 1.4089, "secs": 13}
- [2026-09-24 12:12:54] S4 null 12/20: {"max_oos_lift": 2.1228, "secs": 13}
- [2026-09-24 12:13:07] S4 null 13/20: {"max_oos_lift": 1.1486, "secs": 13}
- [2026-09-24 12:13:20] S4 null 14/20: {"max_oos_lift": 1.4659, "secs": 13}
- [2026-09-24 12:13:34] S4 null 15/20: {"max_oos_lift": 1.8759, "secs": 13}
- [2026-09-24 12:13:48] S4 null 16/20: {"max_oos_lift": 2.4537, "secs": 14}
- [2026-09-24 12:14:01] S4 null 17/20: {"max_oos_lift": 1.7471, "secs": 13}
- [2026-09-24 12:14:14] S4 null 18/20: {"max_oos_lift": 1.708, "secs": 13}
- [2026-09-24 12:14:27] S4 null 19/20: {"max_oos_lift": 1.4683, "secs": 13}
- [2026-09-24 12:14:41] S4 null 20/20: {"max_oos_lift": 1.4891, "secs": 13}
- [2026-09-24 12:14:41] S4 null v2: {"null_95": 2.1533}
- [2026-09-24 12:15:09] S7b E1 v2: {"is_events": 8119, "oos_events": 3975, "is_positive": 1156, "oos_positive": 629, "candidates": 455, "survivors": 0}
- [2026-09-24 12:15:29] S7b E1_EXT v2: {"is_events": 5186, "oos_events": 2160, "is_positive": 741, "oos_positive": 334, "candidates": 433, "survivors": 0}
- [2026-09-24 12:16:49] S7b E2 v2: {"is_events": 34363, "oos_events": 16065, "is_positive": 5815, "oos_positive": 2565, "candidates": 448, "survivors": 0}
- [2026-09-24 12:16:49] S7b done: {"elapsed_min": 8.7, "new_factor_hits": {"E1": 47, "E1_EXT": 33, "E2": 32}}

```
