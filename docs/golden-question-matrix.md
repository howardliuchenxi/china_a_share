# Golden Question Matrix

## Purpose

The golden question matrix is a proactive regression corpus for the A-share
analysis workflow. It is organized by research intent rather than by previously
reported incidents. Each family contains at least three natural-language
variants and declares:

- the expected support tier;
- synchronous or asynchronous delivery;
- the provider operations required by the intended analysis.

The executable corpus lives in `tests/golden_questions.py`. CI validates its
breadth, uniqueness, catalog references, unsupported boundaries, and enforced
asynchronous routes.

## Coverage

| Area | Families |
| --- | --- |
| Market behavior | Market breadth, limit-up lists, limit-up trends, two-limit-up follow-through, period returns |
| Screening and liquidity | Valuation screens, turnover and amount rankings, block trades |
| Ownership | Retail holding proxy, holder counts |
| Capital flows | Security money flow, industry money flow, northbound flow, margin financing |
| Fundamentals and corporate actions | Financial statements, dividends, share unlocks, repurchases |
| Explicit unsupported boundaries | Verified individual-investor ownership, future price guarantees, investor demographics |

## Support tiers

- `supported`: The current catalog exposes the required source fields or the
  backend has an audited deterministic calculation.
- `approximation`: The backend may execute an approved proxy but must disclose
  the methodology difference.
- `unsupported`: The available sources cannot support the requested claim
  without guessing. These cases must fail before provider execution.

## Source boundaries

The matrix is grounded in the active Tushare catalog and official documentation:

- [Tushare stock-data catalog](https://tushare.pro/document/2?doc_id=371)
- [Daily indicators](https://tushare.pro/document/2?doc_id=32)
- [Monthly prices](https://tushare.pro/document/2?doc_id=145)
- [Top ten unrestricted float holders](https://tushare.pro/document/2?doc_id=62)
- [Holder counts](https://tushare.pro/document/2?doc_id=166)
- [Financial indicators](https://tushare.pro/document/2?doc_id=79)
- [Security money flow](https://tushare.pro/document/2?doc_id=170)
- [Margin financing details](https://tushare.pro/document/2?doc_id=59)
- [Block trades](https://tushare.pro/document/2?doc_id=161)
- [Restricted-share unlocks](https://tushare.pro/document/2?doc_id=160)
- [Industry money flow](https://tushare.pro/document/2?doc_id=343)
- [Connect money flow](https://tushare.pro/document/2?doc_id=47)

## Expansion rule

A production incident must add a regression assertion to the existing family
when possible. A new family is justified only when the user intent, provider
grain, support tier, or delivery mode is materially different. This prevents
the corpus from becoming a list of one-off phrases.

