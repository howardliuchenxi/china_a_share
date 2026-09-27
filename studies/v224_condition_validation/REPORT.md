# v2.24 Condition Effect Validation

## Decision summary

The v2.24 non-retroactive L1/L2 rule materially expands the L7 signal set, but this study does not establish a reliable improvement in signal quality.

- The canonical v2.24 state machine produced 5,077 L7 events from 2025-01-01 through 2026-09-24, versus 1,396 events under the counterfactual that reapplies L1/L2 to L3-L6 every day.
- There were 3,693 canonical-only events. The larger event set is the clearest demonstrated effect of v2.24.
- On the 10-session keep-first sample, the signal-close N10 return had a 46.68% hit rate, a 0.64% mean, a -0.55% median, a -0.10% 5% trimmed mean, and a -0.29% mean excess return versus the same-date equal-weight A-share benchmark.
- The next-open N10 return had a 47.14% hit rate, a 0.69% mean, a -0.52% median, a -0.02% 5% trimmed mean, and a -0.32% mean excess return.
- The v2.24-minus-counterfactual N10 signal-close differences were +1.36 percentage points in hit rate and +0.05% in mean return. Both cluster-bootstrap confidence intervals crossed zero.
- Results were unstable across calendar years. The v2.24 signal-close N10 mean was +1.05% in 2025 and -0.41% in 2026; the hit rate declined from 48.60% to 41.66%.

The positive absolute N10 mean is therefore not sufficient for acceptance. The negative median, negative trimmed mean, sub-50% hit rate, and negative benchmark-relative return show that a small right tail drives the headline average.

## Evaluation design

The study implements the supplied v2.24 rules through L7 as a daily state machine with one active L1-L7 layer per security. It permits same-day multi-hop transitions, applies r60 before competing transitions, does not retest L1/L2 for canonical L3-L6 names, and tests the L6-to-L7 trigger only after the L6 entry session.

The comparison state machine differs only by reapplying L1/L2 to securities in L3-L6. It is a narrow counterfactual for the v2.24 scope change and is not presented as a complete v2.23 reconstruction.

The study reports horizons N=1 through N=10 using:

- Research return: adjusted close at t+N divided by adjusted close at the signal close, minus one.
- Tradable diagnostic: adjusted close at t+N divided by the next security trading session's adjusted open, minus one.
- Benchmark-relative return: security return minus the same-date equal-weight Shanghai/Shenzhen A-share panel return.
- Overlap control: retain the first signal for the same security within the following 10 security trading sessions.
- Uncertainty: 1,000 fixed-seed bootstrap samples clustered by signal date.

## Data quality and limitations

The analytical panel contains 2,171,772 security-date rows and 5,271 securities with no duplicate keys. Monthly `daily_basic` coverage is 100.00%; the minimum monthly industry mapping rate is 99.35%.

The principal limitation is historical industry membership. The available `ths_member` response has current membership but no effective dates, so extraction-date THS membership is applied historically. This creates point-in-time look-ahead risk in L2. The workbook states this limitation prominently.

The study validates L7 signal behavior, not a complete executable L10 portfolio. L10 sell and holding rules remain undefined. Signal-close returns are research diagnostics rather than executable fills, which is why next-open diagnostics are reported separately.

## Reproduction

Run the following commands from the repository root:

```bash
studies/reverse_rule_induction/.venv/bin/python studies/v224_condition_validation/fetch_inputs.py
studies/reverse_rule_induction/.venv/bin/python studies/v224_condition_validation/backtest.py
studies/reverse_rule_induction/.venv/bin/python studies/v224_condition_validation/build_notebook.py
node studies/v224_condition_validation/build_workbook.mjs
```

The workbook builder requires the repository's configured Artifact Tool Node runtime. Supplemental market data and generated intermediate outputs stay under ignored `data/` and `outputs/` directories.
