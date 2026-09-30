# Autonomous QA Multi-Agent Framework

A FastAPI + LangChain + LangGraph service that takes a Jira ticket in **Ready for QA** and runs a full QA cycle without a person in the loop:

1. Triage the acceptance criteria and the PR diff.
2. Design a functional and non-functional test plan, plus an Xray CSV test matrix.
3. Generate a Playwright **TypeScript** spec, commit it to the test repo, and run it.
4. Summarise the terminal logs into a Markdown report.
5. Send the verdict back to n8n, which updates Jira and imports the CSV into Xray.

```
Jira ──"Ready for QA"──▶ n8n ──POST /qa/runs──▶ QA service ──callback──▶ n8n ──▶ Jira status + comment
                         (diff, Azure/DB logs)       │                         └──▶ Xray CSV import
                                                     └──events──▶ Paperclip (Kanban, live logs, token budget)
```

## Architecture

### Hybrid LLM routing

| Node | Agent | Engine | Why |
|---|---|---|---|
| 1 | Intake Triager | Claude (cloud) | Reasoning over requirements and code |
| 2 | Test Architect | Claude (cloud) | Risk-based test design, Xray matrix |
| 3 | TS Automation Engineer | Claude (cloud) + local git/pnpm | Code generation, then local execution |
| 4 | Report Synthesizer | Qwen 2.5-Coder 7B on Ollama (local, free) | Log parsing and formatting |
| 5 | Closeout | HTTP | Sends the n8n callback |

### LangGraph pipeline

```
START → intake_triager → test_architect → automation_engineer → report_synthesizer → closeout → END
              │                 │                                       ▲
              └── aborted ──────┴───────────────────────────────────────┘
```

If a cloud node aborts (budget cap hit or an unrecoverable LLM error), the graph jumps straight to the report and closeout. **n8n always gets a callback**, with `QA Failed` and the reason.

**Shared state** (`QAState` in `main.py`): `ticket_id`, `summary`, `description`, `github_diff`, `target_url`, `test_plan`, `generated_typescript_code`, `execution_logs`, `execution_passed`, `xray_csv_content`, `final_report`. It also carries operational fields such as `run_id`, `git_branch`, `errors` and `aborted`.

### Node 3 in detail

1. Claude writes `tests/qa/<TICKET>.spec.ts`. Every test title is prefixed with `[TC-XXX]` so results trace back to Xray.
2. The service takes a lock on the shared test-repo clone, then runs:
   `git fetch` → `git checkout <base>` → `git pull --ff-only` → `git checkout -B qa/<TICKET>` → `git add` → `git commit` → `git push -u origin qa/<TICKET>`.
   It uses `-B` rather than `-b` so that re-running the same ticket works. A rejected push is retried with `--force-with-lease`.
3. It then runs `pnpm install` and `pnpm exec playwright test <spec> --reporter=line` with `BASE_URL=<target_url>` and `CI=1`, and captures stdout and stderr.

Error handling:

- **Remote failures don't stop the run.** If fetch, pull or push fails, the problem is logged and the tests still run.
- **Local failures skip the tests.** If checkout, write or commit fails, or pnpm is missing, the run is marked failed and the reason goes into the report.
- **Nothing hangs.** Every subprocess has a timeout.

### Cost guard (Paperclip)

Every Claude call goes through `TokenBudget`:

- The service checks `MAX_CLOUD_TOKENS_PER_RUN` and `MAX_CLOUD_TOKENS_PER_DAY` **before** each call and refuses the call once a cap is reached.
- After each call, it records the provider-reported token usage and emits a `budget.updated` event.

All lifecycle events are POSTed to `PAPERCLIP_WEBHOOK_URL`: `run.queued`, `run.started`, `agent.started`, `agent.log`, `agent.completed`, `agent.failed`, `agent.skipped`, `budget.updated`, `run.completed` and `run.crashed`. Each event has this shape:

```json
{ "run_id": "…", "ticket_id": "SHOP-123", "event": "agent.completed", "timestamp": "…", "data": { … } }
```

> This event schema is generic. Point `PAPERCLIP_WEBHOOK_URL` at your Paperclip instance's ingest endpoint, or at a small adapter or n8n workflow that maps these events onto Paperclip's task and board API. Event delivery is fire-and-forget: if Paperclip is offline, QA runs are not affected.

> **Full walkthrough:** [SETUP.md](SETUP.md) (macOS) or [SETUP-windows.md](SETUP-windows.md) (Windows) takes you through setting up Ollama, the test repo, this service, Paperclip, n8n and Jira/Xray in order, with a check after each phase.

## Prerequisites

| Tool | Version | Install (macOS) |
|---|---|---|
| Python | **3.11+** (LangGraph 1.x does not support 3.9) | `brew install python@3.12` |
| Node.js | 20+ | `brew install node` or nvm |
| pnpm | 9+ | `corepack enable pnpm` |
| git | 2.31+ (needs `GIT_CONFIG_COUNT` support) | `brew install git` |
| Ollama | latest | `brew install ollama` |

## Installation

```bash
git clone https://github.com/<you>/<this-repo>.git
```

```bash
cd <this-repo>
```

```bash
python3.12 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Then edit `.env` (see the reference below).

### Local model (Ollama)

```bash
ollama pull qwen2.5-coder:7b
```

```bash
ollama serve
```

Check that the OpenAI-compatible endpoint responds:

```bash
curl http://localhost:11434/v1/models
```

If Ollama is unreachable, the Report Synthesizer falls back to a deterministic report. The run still completes.

### TypeScript test repository (pnpm + Playwright)

The service needs a **dedicated clone** of your Playwright repository. Don't use the clone you work in: the agent checks out branches in it. If `TEST_REPO_LOCAL_PATH` doesn't exist, the service clones it on the first run.

The test repo should have:

- `@playwright/test` in `devDependencies` and a committed `pnpm-lock.yaml`.
- A `playwright.config.ts` that reads the base URL from the environment:
  ```ts
  import { defineConfig } from '@playwright/test';
  export default defineConfig({
    testDir: './tests',
    use: { baseURL: process.env.BASE_URL, trace: 'retain-on-failure' },
    retries: process.env.CI ? 1 : 0,
  });
  ```
- Browsers installed once on the machine. Alternatively, set `PLAYWRIGHT_INSTALL_BROWSERS=true` to install them on every run.
  ```bash
  pnpm exec playwright install chromium
  ```

The `GITHUB_TOKEN` should be a fine-grained PAT scoped only to the test repo with **Contents: Read and write**. The service injects it per command as an HTTP auth header through git's environment config, so it never appears in the remote URL, in `.git/config`, in the process list or in logs.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | **Required.** Anthropic API key (from console.anthropic.com). |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Claude model id used by Agents 1–3. |
| `CLAUDE_MAX_TOKENS` | `8192` | Maximum output tokens per Claude call. |
| `CLAUDE_TIMEOUT_SECONDS` | `300` | Timeout for each Claude request. |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama's OpenAI-compatible endpoint. |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Local model used by Agent 4. |
| `OLLAMA_TIMEOUT_SECONDS` | `300` | Timeout for each Ollama request. |
| `GITHUB_TOKEN` | – | PAT used to push `qa/*` branches. |
| `TEST_REPO_URL` | – | HTTPS clone URL of the Playwright repo. |
| `TEST_REPO_LOCAL_PATH` | – | **Required.** Absolute path of the dedicated local clone. |
| `TEST_REPO_BASE_BRANCH` | `main` | Branch that QA branches are created from. |
| `TEST_SPEC_DIR` | `tests/qa` | Where spec files are written, relative to the repo root. |
| `GIT_PUSH_ENABLED` | `true` | Set to `false` to commit locally only (useful for dry runs). |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | `QA Agent` / noreply | Commit identity. |
| `GIT_TIMEOUT_SECONDS` | `120` | Timeout for each git command. |
| `PNPM_INSTALL_TIMEOUT_SECONDS` | `600` | Timeout for `pnpm install`. |
| `PLAYWRIGHT_TIMEOUT_SECONDS` | `1800` | Timeout for the Playwright test run. |
| `PLAYWRIGHT_INSTALL_BROWSERS` | `false` | Install Chromium before every run. |
| `PLAYWRIGHT_EXTRA_ARGS` | `--reporter=line` | Extra CLI arguments for `playwright test`. |
| `PAPERCLIP_WEBHOOK_URL` / `PAPERCLIP_API_KEY` | – | Control Room event sink and its bearer token. |
| `MAX_CLOUD_TOKENS_PER_RUN` | `250000` | Hard cap on Claude tokens per ticket (`0` = off). |
| `MAX_CLOUD_TOKENS_PER_DAY` | `2000000` | Hard cap on Claude tokens per day, in memory (`0` = off). |
| `QA_SERVICE_API_KEY` | – | If set, callers must send it in the `X-API-Key` header. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address, used when running `python main.py`. |
| `MAX_DIFF_CHARS`, `MAX_ENV_LOG_CHARS`, `MAX_EXEC_LOG_CHARS_FOR_LLM` | see `.env.example` | Prompt size limits. Longer inputs are truncated in the middle. |
| `CALLBACK_TIMEOUT_SECONDS` | `30` | Timeout for the n8n callback request. |
| `LOG_LEVEL` | `INFO` | Python log level. |

## Running

Start the service (development mode, with auto-reload):

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

For a long-running process, drop `--reload`. Keep a **single worker**: runs share one git clone, and run state is held in memory.

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Check the configuration:

```bash
curl -s http://localhost:8000/health
```

Interactive API docs are at http://localhost:8000/docs.

## API

### `POST /qa/runs` → `202 Accepted`

Headers: `X-API-Key: <QA_SERVICE_API_KEY>`.

```json
{
  "ticket_id": "SHOP-1234",
  "summary": "Add promo-code field to checkout",
  "description": "As a shopper… Acceptance criteria: …",
  "github_diff": "diff --git a/src/Checkout.tsx b/src/Checkout.tsx …",
  "target_url": "https://staging.example.com",
  "n8n_callback_url": "https://n8n.example.com/webhook/qa-result",
  "azure_logs": "optional",
  "db_logs": "optional"
}
```

Response: `{ "run_id": "…", "ticket_id": "SHOP-1234", "status": "queued", "status_url": "/qa/runs/…" }`.

If a run for the same ticket is already in progress, the service returns `409`.

### `GET /qa/runs/{run_id}`

Returns the run's status (`queued`, `running`, `completed` or `crashed`), the outcome, the branch, token usage, errors and the final report.

### Callback to n8n

```json
{
  "ticket_id": "SHOP-1234",
  "status": "QA Passed",
  "report_markdown": "## QA Report - SHOP-1234 …",
  "xray_csv_content": "\"Test ID\",\"Summary\",\"Test Type\",…",
  "metadata": { "run_id": "…", "git_branch": "qa/SHOP-1234", "git_pushed": true, "cloud_tokens_used": 41234, "aborted": false, "errors": [] }
}
```

`status` is either `"QA Passed"` or `"QA Failed"`. Use `metadata.run_id` as an idempotency key in n8n: the callback is retried on connection errors, 429 and 502–504.

## n8n workflow outline

1. **Jira Trigger**: fires on an issue transition to *Ready for QA*.
2. **GitHub node**: fetches the linked PR's diff (`Accept: application/vnd.github.diff`).
3. **Azure / DB nodes**: pull recent logs.
4. **HTTP Request**: `POST http://<host>:8000/qa/runs` with the `X-API-Key` header.
5. **Webhook (callback)**: receives the result, then:
   - transitions the Jira issue according to `status`;
   - adds `report_markdown` as a comment;
   - imports `xray_csv_content` through the Xray Test Case Importer (CSV).

   The CSV uses one row per step and repeats the `Test ID` for later steps. Its columns are: `Test ID, Summary, Test Type, Priority, Labels, Requirement, Preconditions, Action, Data, Expected Result`. Labels are separated by `;`. The `Requirement` column holds the ticket key, so imported tests link to the story.

## Operational notes

- The run registry and the daily budget counter live **in memory** and reset on restart. Put them in Redis or Postgres if you need persistence or more than one worker.
- The spec is written as `<TICKET>.spec.ts`. The ticket ID is validated, which prevents path traversal and invalid branch names.
- The report header (status, counts, branch, tokens) is built in Python. The local model only formats the log details, so it cannot change the verdict.
