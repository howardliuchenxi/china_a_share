# Discovery Architecture

## Purpose

The discovery module searches historical A-share data for compact, explainable
event-study rules. It reverses the normal workflow: instead of starting with a
hand-written strategy and finding matching observations, it generates a bounded
set of factor thresholds from training data and tests the frozen candidates on
an independent validation window.

The implementation is deterministic. Research notes are stored for provenance
but never sent to a model and never influence rule generation, ranking, or
validation.

## Task lifecycle

`POST /api/discovery/tasks` accepts a `DiscoveryTaskRequest` and dispatches the
persisted task through the existing asynchronous task infrastructure. A worker
runs `EvolutionLoop`, while `GET /api/discovery/tasks/{task_id}` returns progress,
the immutable non-sensitive research configuration, and the final leaderboard.

Despite its historical name, `EvolutionLoop` currently performs one bounded
deterministic search pass. `max_generations` is therefore fixed at one. The task
fails fast when required calendar, daily-price, or adjustment-factor data is
incomplete.

## Research contract

The supported universe is `A_SHARE`. The session loader accepts six-digit
Shanghai, Shenzhen, and Beijing stock codes and excludes known Shanghai 900xxx
and Shenzhen 200xxx B shares.

Each request defines:

- non-overlapping training and validation signal-date windows;
- one to sixty trading sessions for the forward-return horizon;
- a forward-return threshold that defines a hit;
- supported numeric factors and a maximum of two rule conditions;
- minimum observable events, signal dates, distinct securities, and outcome
  coverage required independently in both windows.

The public task status exposes this configuration but excludes the free-form
research note.

## Point-in-time dataset construction

`FactorBacktester` loads the exchange calendar, daily market data, daily basic
factors, and adjustment factors for every required signal and outcome session.
It computes forward returns from consistently split-and-dividend-adjusted close
prices.

The dataset also derives point-in-time historical features without an extra
provider source: split-and-dividend-adjusted five-session return, population
standard deviation of the five adjusted daily returns, maximum peak-to-trough
drawdown across that five-return path, signal-date distance from the path's
adjusted-close peak, and the number of positive adjusted-close returns across
the latest three consecutive market sessions.
It loads five pre-window sessions solely for feature history. A security missing
any required market session receives a missing feature instead of having rows
across a suspension joined into a false consecutive sequence.

Signal rows are retained when their future price is unavailable. This makes
outcome attrition measurable instead of silently studying only surviving rows.
Training observations whose outcome date reaches the validation window are
purged before search, preventing labels from crossing the blind-test boundary.
A full-month minimum calendar extension covers long exchange closures even for
a one-session forward horizon.

## Deterministic rule search

`RuleSearchEngine` creates lower- and upper-tail conditions at every observed
value for factors with at most ten finite values, allowing rare discrete states
such as a three-day streak to remain searchable. Continuous factors use the
training window's 10%, 25%, 50%, 75%, and 90% quantiles. Equivalent selections
are deduplicated. Factors are processed in canonical field-name order, so
request ordering cannot change threshold generation, bounded pairing, or
equivalent-cohort selection. When two conditions are allowed, the pairing pool
reserves one slot for every factor admitted by the public discovery contract
before adding alternate directions. The current 25-factor catalog therefore
caps the pair search at 300 combinations without silently excluding a factor.

Candidates are ranked only on training data by the conservative lower 95%
lift bound. This bound is the lower edge of the envelope combining the
date-clustered HAC interval with the difference between rule and baseline score
intervals, so a zero HAC standard error is not mistaken for certainty:

```text
min(HAC lift lower bound, rule score lower bound - baseline score upper bound)
```

Ties are resolved by the fifth-percentile forward return and then median return,
so an isolated positive outlier cannot win a tie through mean return alone.

The training-ranked validation shortlist is frozen before validation outcomes
are read. Every frozen candidate remains in that order even when its validation
evidence misses a configured sample, date, security, or outcome-coverage
threshold. Such candidates receive `p = 1` and an explicit machine-readable
failure reason instead of disappearing and allowing a lower training rank to
take their place. Validation evidence never reorders the leaderboard.
Each hypothesis records whether its thresholds came from continuous quantiles,
observed discrete values, or a mixture, so displayed provenance matches the
actual training-only generation path.

## Statistical guardrails

Each rule is compared with events for which every referenced factor is finite.
This prevents factor availability, such as PE missing for loss-making companies,
from being mistaken for threshold alpha. Outcome fields and future dates are
forbidden in rule expressions.

Reported evidence includes:

- event count, distinct signal dates, and distinct securities;
- maximum single-security event share, which reveals concentration hidden by a
  distinct-security count;
- Kish effective security count based on per-security event weights; both raw
  and effective security counts must satisfy the configured security breadth
  threshold in training and validation;
- maximum single-date event share, which reveals temporal concentration hidden
  by a distinct-signal-date count;
- Kish effective signal-date count based on daily event weights; this count is
  used by the boundary-safe score interval so a date-concentrated sample cannot
  claim the same precision as an equally distributed set of signal dates; both
  raw and effective date counts must satisfy the configured date-breadth
  threshold in training and validation;
- rule support among factor-comparable events;
- absolute train-to-validation support-rate drift for applicability diagnostics;
- validation-to-training support retention, which exposes relative applicability
  collapse that can look small in percentage-point terms;
- observable-outcome coverage for both selected events and the complete
  factor-comparable baseline; both must satisfy the configured coverage gate;
- worst- and best-case probability bounds that treat every unobserved outcome
  as a failure or success, respectively;
- hit rate, mean and median forward return, and fifth-percentile return;
- hit-rate lift over the factor-comparable baseline;
- a conservative lift interval enveloping the date-clustered HAC interval and
  the difference between selected and baseline concentration-adjusted
  probability intervals, including their unobserved-outcome bounds;
- date-clustered HAC uncertainty that accounts for overlapping horizons;
- zero-influence calendar gaps that preserve true trading-session HAC lags when
  a factor is unavailable for an entire date, while the finite-sample variance
  correction counts only dates with comparable or selected observations;
- a conservative probability interval combining HAC and a signal-date score
  interval;
- finite-date, one-sided Student-t validation p-values and Benjamini-Yekutieli
  q-values, whose
  harmonic penalty controls false discoveries under arbitrary dependence among
  overlapping and nested rules.

The Student-t significance calculation uses the floored Kish effective
signal-date count minus one degree of freedom and requires at least twenty
effective validation signal dates. Date-concentrated or shorter studies remain
visible for exploration but receive `p = 1`, report
`insufficient_significance_days`, and cannot pass FDR. The user-configured raw
and effective distinct-date thresholds remain separate coverage requirements.

The false-discovery family contains every frozen candidate sent to validation,
including candidates retained with insufficient validation evidence. A rule
passes validation only when every configured evidence threshold is satisfied,
training and validation lift are both positive, both lifts remain positive when
selected missing outcomes fail and non-selected comparable-baseline missing
outcomes succeed while preserving the actual selected/baseline overlap,
and the validation q-value is at most 0.10. This prevents an observed-only
p-value from overriding an attrition-sensitive economic conclusion.

## Result interpretation

`BacktestResult` describes event endpoints, not a self-financing portfolio. It
therefore does not claim trading PnL, turnover, capacity, or portfolio drawdown.
`max_drawdown` is retained as a compatibility field but is `null`, because
overlapping forward-return endpoints cannot identify a daily position-level
equity curve.

The current study also omits fees, slippage, limit-up and limit-down execution,
position overlap, and portfolio construction. A validated association is a
research lead, not a causal claim or a deployable trading strategy.

## Frontend

`DiscoveryPage` provides the research form, polling progress, immutable task
snapshot, factor-coverage diagnostics, validation summary, and training-ranked
leaderboard. Every rule shows training and validation evidence side by side,
including event, date, security, support, and label-coverage breadth.

Each window also carries at most five most-recent observable matched events,
ordered deterministically by signal date and security. The UI keeps these audit
rows collapsed by default and exposes signal date, security, outcome settlement
date, adjusted forward return, and the finite signal-date values of only the
factors referenced by that rule. This lets a researcher verify both why the
event matched and how it resolved without allowing response size to grow with
the full event population. Training examples are extracted only after the bounded
training-ranked validation shortlist is frozen, so formulas discarded during
initial screening do not incur presentation-only work.

The summary explicitly describes the first rule as the training leader and
reports how many leaderboard entries passed validation. Applying a rule sends
an explicit natural-language screening request to the analysis page; the user
must still verify the resulting query plan before execution.

Rules containing internal adjusted-history features are not directly applied
until the analysis page can reproduce their adjusted-price and consecutive-
session semantics exactly. Their evidence remains visible, but the disabled
action explains the missing execution capability instead of asking a planner to
guess or silently translate the formula.
