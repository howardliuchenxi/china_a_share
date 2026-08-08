# Repository Instructions

## Continuous delivery workflow

- After discussing and agreeing on an approach, continue directly through
  contracts, implementation, targeted validation, and handoff in one task.
- Do not split work into mandatory Phase 1, Phase 2, or Phase 3 approval gates.
- For non-trivial changes, state the relevant assumptions and a concise
  technical strategy before editing, then proceed without waiting for another
  confirmation.
- Pause only when a missing user decision would materially change the result,
  additional authority is required, or the next action is destructive or
  otherwise high risk.
- Keep intermediate updates concise and maintain momentum until the agreed
  outcome is implemented and validated.
- After validating requested repository changes, automatically commit the
  task-scoped files with an imperative English commit message and push the
  current branch without requesting separate confirmation.
- Never include unrelated or pre-existing working-tree changes in an automatic
  commit. If a push is rejected, diagnose and resolve non-destructive issues
  directly; escalate only when user action or a material decision is required.

## Self-resolution policy

- When a failure, error, or problem arises (CI run, deployment, test, build,
  runtime error, API issue, etc.), investigate and diagnose it directly.
- If the root cause is a code, configuration, or infrastructure issue that can
  be resolved without human judgement, fix it immediately without asking.
- Only escalate when a human decision is required: secret rotation, billing
  action, manual infrastructure change, a trade-off that materially changes the
  product, or an ambiguous situation where you need the user's preference.
- After fixing, push and verify the fix works.

## General-solution requirement

- Treat every reported failure as evidence of a missing or incorrect general
  contract, validation rule, planning rule, execution mechanism, or domain
  model behavior. Fix the reusable mechanism at the narrowest appropriate
  architectural boundary.
- Never solve a failure by hard-coding the reported prompt, tracking ID, stock,
  company, industry, metric, date, or returned value. Do not add enumerated
  special cases that only disguise the same defect for nearby inputs.
- Regression tests must express the invariant or input class that was broken,
  not merely replay one reported example. Include a materially different case
  when needed to prove the behavior generalizes.
- Prefer schema constraints, semantic validation, deterministic compilation,
  or reusable execution logic over prompt-only guidance when the invariant can
  be enforced locally. Prompt changes may clarify intent but must not be the
  sole correctness boundary for enforceable behavior.
- If a genuinely exceptional upstream limitation makes a narrow special case
  unavoidable, explain its scope, operational cost, and removal path in chat
  and obtain explicit user approval before implementation.

## Local delivery workflow

- After making changes, limit automated validation to fast, relevant code
  checks such as compilation, type checking, linting, or targeted unit tests.
- For UI changes, start the local application and open the exact affected page
  for the user to review. Do not inspect the DOM, take screenshots, or perform
  automated browser interactions unless the user explicitly requests them.
- Leave visual, interactive, and end-to-end page validation to the user.
- Do not deploy the application or mutate cloud resources unless the user
  explicitly requests deployment or a specific cloud change.
- After pushing code to main, never run `make deploy` or trigger Cloud Build
  manually. Production deployment is handled automatically by Cloud Scheduler
  (`china-a-share-reconcile-main`) every 10 minutes via the reconciliation
  trigger. Just push and let the scheduler pick it up.

## Google Cloud resource inventory

- Treat `docs/gcp-resources.md` as the project inventory for live Google Cloud
  resources.
- Read the inventory before creating, changing, or deleting any Google Cloud
  resource for this project.
- In the same task as any Google Cloud resource mutation, verify the resulting
  live state with a read-only command and update `docs/gcp-resources.md`.
- Record the resource type, name, project, region, purpose, material settings,
  IAM boundary, lifecycle policy, and expected cost impact.
- Keep the "Not provisioned" section accurate so planned services are not
  mistaken for live resources.
- Never write secret values, access tokens, authentication codes, private keys,
  or full credentials into the inventory. Secret resource names and enabled
  version numbers are allowed.
- If a resource was changed manually outside Codex, reconcile the inventory the
  next time that change is observed.
