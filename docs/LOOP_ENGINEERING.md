# Loop Engineering Workflow

## Purpose

Continuously improve the China A-Share Lab while preserving existing
capabilities, financial-data correctness and deterministic validation.

## Persistent state

The loop uses:

- `.loop/state.json`: current iteration and current status.
- `.loop/backlog.md`: prioritized engineering objectives.
- `.loop/history.jsonl`: one sanitized record per completed iteration.
- `.loop/handoff.md`: handoff between TRAE and manual Codex sessions.
- `.loop/screenshots/`: local visual evidence; do not commit by default.
- `.loop/logs/`: local command logs; do not commit by default.

## State machine

- idle
- observing
- planning
- implementing
- validating
- blocked
- completed
- paused

## Iteration workflow

### 1. Observe

- Pull the latest remote commit with fast-forward only.
- Confirm the working tree is clean.
- Run baseline tests.
- Start the local application.
- Reproduce the current behavior in a browser.
- Capture a before screenshot for UI work.

### 2. Plan

Select one backlog item.

Write:

- objective;
- current behavior;
- expected behavior;
- acceptance criteria;
- relevant files;
- likely risks;
- validation commands.

Do not change code during planning.

### 3. Implement

- Make the smallest generalized change.
- Preserve raw query visibility.
- Preserve existing error handling.
- Preserve existing user controls.
- Do not broaden the task during implementation.

### 4. Validate

Run backend tests, frontend build and E2E tests.

For UI changes, test at least:

- 1440 x 1000 desktop;
- 1280 x 800 laptop;
- 390 x 844 mobile.

Validate:

- loading;
- success;
- partial success;
- empty data;
- upstream error;
- long tables;
- missing columns;
- multiple stocks;
- multiple APIs.

### 5. Record

Append one JSON object to `.loop/history.jsonl` containing:

- loop_id;
- started_at;
- ended_at;
- duration_seconds;
- objective;
- actions;
- files_changed;
- tests;
- result;
- blocker;
- commit;
- next_proposal.

Never include secrets or complete upstream responses.

### 6. Commit

Use:

`loop(<loop-id>): <small objective>`

Push only after successful validation.

### 7. Notify

Send a Feishu summary containing:

- loop ID;
- objective;
- duration;
- changes;
- test result;
- commit;
- next proposed objective;
- whether a decision is required.

## Feature discovery mode

Feature discovery is separate from implementation.

Research output must contain:

- benchmark platform;
- official or credible source;
- observed feature;
- user value;
- corresponding Tushare APIs and fields;
- data availability;
- whether the result is raw or derived;
- implementation size;
- product risk;
- acceptance criteria.

Feature discovery may update `.loop/backlog.md`, but must not implement a
new feature until it is approved.
