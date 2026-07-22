# China A-Share Lab Project Rules

## Project identity

This repository is a full-stack A-share data laboratory.

- Backend: Python, FastAPI, Pydantic, Tushare and DeepSeek.
- Frontend: React, TypeScript and Vite.
- Production deployment: Google Cloud Run.
- Current shared development branch:
  `codex/initial-cloud-deployment`.

DeepSeek is a query planner only. It must not receive raw Tushare result
rows. Tushare access, validation, execution and financial calculations
must remain deterministic local application logic.

## Non-negotiable product constraints

1. Preserve every existing user-visible capability.

2. Never remove, hide, disable, bypass or weaken existing functionality
   merely to simplify implementation or make a test pass.

3. Never invent production financial data.

4. Every production value displayed to the user must originate from one
   of the following:
   - an actual Tushare response;
   - a documented deterministic calculation over actual source fields;
   - explicit user input;
   - another approved public data source with provenance.

5. Synthetic values are allowed only inside clearly marked automated
   test fixtures. Test fields and semantics must conform to the official
   Tushare API documentation.

6. Missing production data must remain missing. Never silently convert
   missing values to zero, an estimated value or a fabricated value.

7. Every derived financial metric must define:
   - metric name;
   - formula;
   - input fields;
   - source API;
   - missing-value behavior;
   - formula version.

8. Do not describe the percentage outside the top ten floating
   shareholders as retail ownership. It includes every shareholder
   outside the top ten.

9. Do not expose tokens, credentials, environment variables, customer
   data, full production logs or private infrastructure details.

## General implementation rules

1. Implement generalized data and rendering abstractions rather than
   one-off behavior for a single stock, prompt or Tushare API.

2. Result rendering must be based on logical datasets and comparison
   intent, not merely on the number of upstream API calls.

3. A comparison request for multiple stocks should normally produce one
   comparison dataset and one primary comparison table.

4. Raw query results and upstream errors must remain inspectable even
   when a summarized comparison view is added.

5. Preserve backward compatibility for the current AnalysisResponse
   contract unless a versioned migration and tests are added.

6. Keep business calculations in backend domain modules rather than in
   React components.

7. Keep React rendering components separate from result transformation
   and financial metric calculation.

8. Avoid large unrelated refactors during a feature loop.

## Git ownership

1. TRAE is the only writer while the autonomous loop is running.

2. Codex does not participate in the TRAE loop.

3. Work only on:
   `codex/initial-cloud-deployment`.

4. Do not create another branch unless the user explicitly requests it.

5. Do not force-push, rewrite remote history or automatically rebase.

6. Do not merge to another branch.

7. Do not deploy or move production traffic without explicit approval.

8. Create one commit for each successfully validated loop iteration.

## Loop protocol

For every iteration:

1. Read:
   - `.trae/rules/project_rules.md`;
   - `docs/LOOP_ENGINEERING.md`;
   - `.loop/state.json`;
   - `.loop/backlog.md`;
   - `.loop/handoff.md`, when present.

2. Inspect the current implementation and reproduce the current behavior
   before modifying code.

3. Select exactly one small and independently verifiable objective.

4. Record the loop start time and objective in `.loop/state.json`.

5. Implement the smallest generalized change that satisfies the
   acceptance criteria.

6. Run all required validation.

7. Inspect the final diff for unrelated changes.

8. Update the loop state and append a sanitized history entry.

9. Commit and push only when validation succeeds.

10. Send a sanitized Feishu summary after the iteration.

## Required validation

Always run:

- `python -m pytest`
- `cd frontend && pnpm run build`

After Playwright is installed, also run:

- `cd frontend && pnpm run test:e2e`

For user-interface changes:

- inspect the page in Chromium;
- capture desktop and mobile screenshots;
- verify that tables remain readable;
- verify horizontal overflow;
- verify missing values;
- verify loading, empty and error states;
- verify that existing controls still work.

## Stop conditions

Stop the loop and request a user decision when:

- an existing capability regresses;
- production financial data provenance is unclear;
- a required Tushare field or API is unavailable;
- data semantics are ambiguous;
- the same failure occurs twice without measurable progress;
- a database migration is required;
- production deployment is required;
- destructive Git or infrastructure work is required;
- a security or permission decision is required;
- the task expands materially beyond the selected objective.

## Limits

- Maximum eight iterations per TRAE session.
- Maximum sixty minutes per iteration.
- Maximum two attempts at the same failed approach.
- Never run an unbounded loop".
