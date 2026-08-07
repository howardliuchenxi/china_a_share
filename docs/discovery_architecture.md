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

Signal rows are retained when their future price is unavailable. This makes
outcome attrition measurable instead of silently studying only surviving rows.
Training observations whose outcome date reaches the validation window are
purged before search, preventing labels from crossing the blind-test boundary.
A full-month minimum calendar extension covers long exchange closures even for
a one-session forward horizon.

## Deterministic rule search

`RuleSearchEngine` creates lower- and upper-tail conditions at the training
window's 10%, 25%, 50%, 75%, and 90% factor quantiles. Equivalent selections
are deduplicated. When two conditions are allowed, a 24-entry pairing pool first
covers every factor with an eligible single-condition candidate and then adds
alternate directions. This caps the pair search at 276 combinations.

Candidates are ranked only on training data by conservative lift:

```text
training win-rate lift - 1.6448536269514722 * clustered lift standard error
```

Ties are resolved by the fifth-percentile forward return and then median return,
so an isolated positive outlier cannot win a tie through mean return alone.

The training-ranked validation shortlist is frozen before validation outcomes
are read. Validation evidence never reorders the leaderboard.

## Statistical guardrails

Each rule is compared with events for which every referenced factor is finite.
This prevents factor availability, such as PE missing for loss-making companies,
from being mistaken for threshold alpha. Outcome fields and future dates are
forbidden in rule expressions.

Reported evidence includes:

- event count, distinct signal dates, and distinct securities;
- rule support among factor-comparable events;
- absolute train-to-validation support-rate drift for applicability diagnostics;
- observable-outcome coverage;
- hit rate, mean and median forward return, and fifth-percentile return;
- hit-rate lift over the factor-comparable baseline;
- date-clustered HAC uncertainty that accounts for overlapping horizons;
- zero-influence calendar gaps that preserve true trading-session HAC lags when
  a factor is unavailable for an entire date;
- a conservative probability interval combining HAC and a signal-date score
  interval;
- one-sided validation p-values and Benjamini-Hochberg q-values.

The false-discovery family contains every frozen candidate sent to validation,
including candidates later excluded for insufficient validation evidence. A
rule passes validation only when training and validation lift are both positive
and the validation q-value is at most 0.10.

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

The summary explicitly describes the first rule as the training leader and
reports how many leaderboard entries passed validation. Applying a rule sends
an explicit natural-language screening request to the analysis page; the user
must still verify the resulting query plan before execution.
