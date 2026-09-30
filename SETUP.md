# Setup Guide

Step-by-step setup of the full stack on a MacBook: the QA service, Ollama, the Playwright test repo, Paperclip and n8n, wired to Jira, GitHub and Xray.

> **On Windows?** Follow [SETUP-windows.md](SETUP-windows.md) instead.

Do the steps in order. Each phase ends with a **✅ Check** — don't move on until it passes.

| Phase | What | Time |
|---|---|---|
| 1 | Machine prerequisites | 10 min |
| 2 | Local model (Ollama + Qwen) | 5 min |
| 3 | Playwright test repository | 15 min |
| 4 | QA service | 10 min |
| 5 | Paperclip | 15 min |
| 6 | n8n credentials | 15 min |
| 7 | n8n Workflow B — results → Jira/Xray | 20 min |
| 8 | n8n Workflow A — Jira → QA service | 20 min |
| 9 | End-to-end test | 10 min |

You'll end up running **four long-lived processes**, each in its own terminal tab: Ollama, the QA service, Paperclip and n8n.

---

## Phase 1 — Machine prerequisites

**1.1 Python 3.12.** The service needs Python 3.10 or newer; macOS ships 3.9.

```bash
brew install python@3.12
```

**1.2 Node 24.** Paperclip requires Node 24.11 or newer. Node 24 also works for n8n and Playwright.

```bash
nvm install 24
```

```bash
nvm alias default 24
```

**1.3 pnpm for Node 24.** nvm keeps global packages per Node version, so pnpm has to be installed again for Node 24.

```bash
npm install -g pnpm
```

**1.4 Git 2.31+.** The service passes the GitHub token to git through `GIT_CONFIG_COUNT`, which needs at least this version.

```bash
git --version
```

✅ **Check:** open a **new** terminal tab and confirm all four report the expected versions.

```bash
python3.12 --version && node --version && pnpm --version && git --version
```

---

## Phase 2 — Local model (Ollama + Qwen)

**2.1** Start Ollama and leave it running in its own tab. If you use the Ollama menu-bar app, it's already running and you can skip this.

```bash
ollama serve
```

**2.2** Download the model if you don't already have it (about 4.7 GB):

```bash
ollama pull qwen2.5-coder:7b
```

✅ **Check:** the OpenAI-compatible endpoint lists `qwen2.5-coder:7b`.

```bash
curl -s http://localhost:11434/v1/models
```

---

## Phase 3 — Playwright test repository

This is the **TypeScript** repo where the agent commits specs. The service works in its **own dedicated clone**, because it switches branches there. Never point it at the folder you work in.

**3.1** If the test repo doesn't exist yet, create one on GitHub (for example `reginAbdu/qa-playwright-tests`). Scaffold it in a working folder of your own:

```bash
mkdir -p ~/code && cd ~/code && pnpm create playwright@latest qa-playwright-tests
```

When prompted, choose **TypeScript**, tests folder `tests`, **no** GitHub Actions workflow, and **yes** to installing browsers.

**3.2** Replace `playwright.config.ts` so tests read the target URL from the environment:

```ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  reporter: [['line'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
```

**3.3** Create the folder the agent writes specs into, then commit and push:

```bash
cd ~/code/qa-playwright-tests && mkdir -p tests/qa && touch tests/qa/.gitkeep
```

```bash
git add . && git commit -m "Playwright scaffold" && git branch -M main
```

```bash
git remote add origin https://github.com/reginAbdu/qa-playwright-tests.git && git push -u origin main
```

**3.4** Create a GitHub token for the agent. On GitHub, go to **Settings → Developer settings → Fine-grained tokens → Generate new token**:

- **Repository access:** *Only select repositories* → `qa-playwright-tests`
- **Permissions:** *Contents* → **Read and write**. *Metadata: Read* is added automatically.

Copy the token (`github_pat_…`); you'll put it in `.env` in Phase 4.

You don't need to clone the repo for the agent: the service clones it into `TEST_REPO_LOCAL_PATH` on its first run.

✅ **Check:** the repo on GitHub shows `playwright.config.ts`, `package.json`, `pnpm-lock.yaml` and `tests/qa/`.

---

## Phase 4 — QA service

**4.1** Create a virtual environment and install the dependencies:

```bash
cd ~/Documents/TAF && python3.12 -m venv .venv
```

```bash
source .venv/bin/activate && pip install -r requirements.txt
```

**4.2** Create your `.env` from the template:

```bash
cp .env.example .env
```

**4.3** Generate a shared secret for n8n to send to the service:

```bash
openssl rand -hex 32
```

**4.4** Edit `.env` and set at least these values:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | A key from **console.anthropic.com → API Keys** (a claude.ai subscription doesn't include API access). The key's workspace needs access to `claude-opus-5-5` and `claude-sonnet-5-5` |
| `GITHUB_TOKEN` | The token from step 3.4 |
| `TEST_REPO_URL` | `https://github.com/reginAbdu/qa-playwright-tests.git` |
| `TEST_REPO_LOCAL_PATH` | `/Users/regina/qa-agent/qa-playwright-tests` (a **new, non-existent** folder) |
| `QA_SERVICE_API_KEY` | The value from step 4.3 |
| `PAPERCLIP_API_URL` / `PAPERCLIP_API_KEY` | Leave **empty** for now; you'll fill them in at step 5.6 |

**4.5** Start the service and leave it running in its own tab:

```bash
cd ~/Documents/TAF && source .venv/bin/activate && uvicorn main:app --host 127.0.0.1 --port 8000
```

✅ **Check:** in `/health`, `claude_configured`, `tools.git` and `tools.pnpm` should all be `true`. `claude_routes` shows which model each agent uses. `is_git_clone` stays `false` until the first run clones the repo.

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

If `tools.pnpm` is `false`, you started uvicorn from a shell where Node 24 or pnpm isn't on `PATH`. Open a new tab and start it again.

---

## Phase 5 — Paperclip (Control Room)

**5.1** Install Paperclip and run its onboarding. It stores its data in an embedded Postgres database, and running `onboard` again later keeps your settings.

```bash
npx --registry https://registry.npmjs.org paperclipai onboard --yes
```

**5.2** Open **http://localhost:3100**. If the UI doesn't come up after onboarding, run `npx paperclipai --help` to find the start command for your version.

**5.3** In the UI, create:

1. A **company** named `QA Lab`.
2. A **project** named `QA Automation`.
3. An **agent** named `QA Pipeline`. This agent is the identity the QA service acts as. **n8n** starts the runs, not Paperclip, so Paperclip must never try to run this agent itself:
   - **Adapter:** *HTTP*, URL `http://localhost:8000/health`. The adapter is never actually used, but Paperclip requires one.
   - **Heartbeat:** **off**.
   - **Wake on demand:** **off**. This one matters: it's *on* by default, and while it's on, Paperclip tries to run the agent every time the service assigns it a QA issue.

**5.4** Set the agent's **monthly budget** (for example $50). The service books the cost of every Claude call against this budget. When Paperclip pauses the agent for going over budget, the service stops making Claude calls immediately, and the ticket gets a `QA Failed` result that explains why. The local `MAX_CLOUD_TOKENS_PER_RUN` / `_PER_DAY` caps in `.env` also still apply.

**5.5** Create an **API key** for the `QA Pipeline` agent and copy it. You don't need to note any IDs: the service looks up the agent and company from the key.

**5.6** Connect the service. In `~/Documents/TAF/.env` set:

| Variable | Value |
|---|---|
| `PAPERCLIP_API_URL` | `http://localhost:3100` (without `/api`) |
| `PAPERCLIP_API_KEY` | The key from step 5.5 |
| `PAPERCLIP_PROJECT` | `QA Automation` |

Then restart the QA service: press `Ctrl+C` in its tab and run the uvicorn command from step 4.5 again.

✅ **Check:** the service's startup log says `Paperclip connected as agent 'QA Pipeline'`. In the health output below, the `paperclip` section should show `"reachable": true`, `"wake_on_demand": false`, `"heartbeat_enabled": false` and `"warnings": []`.

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

If `warnings` is not empty, it says what to change in Paperclip.

---

## Phase 6 — n8n: install and credentials

**6.1** Start n8n and leave it running in its own tab. `--tunnel` gives n8n a public URL, which Jira Cloud needs in order to reach a server on your laptop:

```bash
npx n8n start --tunnel
```

Open **http://localhost:5678** and create the owner account. The tunnel is for development only; for permanent use, host n8n or put it behind cloudflared.

**6.2** Create a **Jira API token.** Go to **id.atlassian.com → Security → Create and manage API tokens → Create API token**. Your Atlassian user must be a **Jira admin**: the Jira Trigger node registers its webhook automatically, and only admins can do that.

**6.3** Create an **Xray API key.** In Jira, go to **Apps → Xray → API Keys → Create API Key**, and copy the client ID and secret.

**6.4** In n8n, go to **Overview → Credentials → Create credential** and add these five:

| Credential type | Name | Fields |
|---|---|---|
| **Jira SW Cloud API** | `Jira` | Email, API token, domain `https://<you>.atlassian.net` |
| **GitHub API** | `GitHub` | A classic PAT with `repo` read access to the **application** repo, where the PRs live |
| **Header Auth** | `QA Service` | Name `X-API-Key`, value = your `QA_SERVICE_API_KEY` |
| **Header Auth** | `GitHub Diff` | Name `Authorization`, value `Bearer <same GitHub PAT>` |
| **Custom Auth** | `Xray` | JSON: `{"body": {"client_id": "<XRAY_CLIENT_ID>", "client_secret": "<XRAY_CLIENT_SECRET>"}}` (from step 6.3) |

✅ **Check:** each credential shows **Connection tested successfully**. Header Auth and Custom Auth credentials have no test, which is fine.

---

## Phase 7 — n8n Workflow B: results → Jira + Xray

Build this workflow first, because Workflow A needs its webhook URL.

**7.1** Go to **Create workflow** and name it `QA Result → Jira`.

**7.2 Webhook node.**
- **HTTP Method:** `POST`
- **Path:** `qa-result`
- **Respond:** *Immediately*

Your callback URL is **`http://localhost:5678/webhook/qa-result`**. That is the production URL, which only works while the workflow is **active**; `/webhook-test/` works only while you are listening in the editor.

**7.3 Look up your Jira transition IDs.** Your Jira workflow must have *QA Passed* and *QA Failed* statuses, reachable from *Ready for QA*. With a real issue that is in *Ready for QA*, open this URL in a browser where you're logged in to Jira:

`https://<you>.atlassian.net/rest/api/3/issue/<ISSUE-KEY>/transitions`

Note the `id` values for the transitions to QA Passed and QA Failed.

**7.4 IF node — `Passed?`**
- Condition: `{{ $json.body.status }}` **is equal to** `QA Passed`

**7.5 Two HTTP Request nodes for the transition** — one on the *true* branch, one on the *false* branch:
- **Method:** `POST`
- **URL:** `https://<you>.atlassian.net/rest/api/3/issue/{{ $('Webhook').item.json.body.ticket_id }}/transitions`
- **Authentication:** *Predefined Credential Type* → **Jira Software Cloud API** → `Jira`
- **Send Body:** JSON → `{"transition": {"id": "<PASSED_ID>"}}` on the true branch, `<FAILED_ID>` on the false branch

**7.6 HTTP Request — `Add report comment`.** Connect both transition nodes into it.
- **Method:** `POST`
- **URL:** `https://<you>.atlassian.net/rest/api/2/issue/{{ $('Webhook').item.json.body.ticket_id }}/comment`
- **Authentication:** `Jira` (same as 7.5)
- **Body:** JSON, *Using Fields Below* → name `body`, value `{{ $('Webhook').item.json.body.report_markdown }}`

API **v2** is used because it accepts a plain string. Jira doesn't render Markdown, so tables appear as plain text.

**7.7 IF node — `Has Xray tests?`.** Connect it after `Add report comment`.
- Condition (Number): `{{ ($('Webhook').item.json.body.xray_tests_json || []).length }}` **is larger than** `0`

If a run stopped before the test plan was written (for example, because of the budget guard), there's nothing to import, and the false branch simply ends.

**7.8 HTTP Request — `Xray authenticate`** (true branch).
- **Method:** `POST`
- **URL:** `https://xray.cloud.getxray.app/api/v2/authenticate`
- **Authentication:** *Generic Credential Type* → **Custom Auth** → `Xray`
- **Options → Response → Response Format:** **Text**, put output in field `token`

Xray returns the token as a quoted string, which is why the next node removes the quotes.

**7.9 HTTP Request — `Xray import tests`.**
- **Method:** `POST`
- **URL:** `https://xray.cloud.getxray.app/api/v2/import/test/bulk`
- **Header:** `Authorization` = `Bearer {{ $('Xray authenticate').item.json.token.replace(/"/g, '') }}`
- **Body:** JSON, *Using JSON* → `{{ JSON.stringify($('Webhook').item.json.body.xray_tests_json) }}`

This creates one Manual test per test case, with its steps. The tests are filed in the Xray folder `AI QA/<TICKET>` and linked to the story, so they appear in the story's Xray test-coverage panel. The response is `{"jobId": "…"}`.

**7.10 Wait node** — **15 seconds**. The import runs as a background job on Xray's side.

**7.11 HTTP Request — `Xray import status`.**
- **Method:** `GET`
- **URL:** `https://xray.cloud.getxray.app/api/v2/import/test/bulk/{{ $('Xray import tests').item.json.jobId }}/status`
- **Header:** `Authorization` = the same value as in 7.9

**7.12 HTTP Request — `Comment import result`.** This records the result on the ticket, so a failed import doesn't go unnoticed.
- **Method:** `POST`
- **URL:** `https://<you>.atlassian.net/rest/api/2/issue/{{ $('Webhook').item.json.body.ticket_id }}/comment`
- **Authentication:** `Jira`
- **Body:** JSON, *Using Fields Below* → name `body`, value:
  `{{ 'Xray import ' + $json.status + ': ' + ($json.result?.issues || []).map(i => i.key).join(', ') + (($json.result?.errors || []).length ? ' | errors: ' + JSON.stringify($json.result.errors) : '') }}`

A `status` of `working` means Xray hasn't finished yet. If that happens regularly, raise the wait in 7.10.

**7.13 (Optional) Attach the CSV as an audit copy.** From `Add report comment`, add a second branch:
1. **Convert to File — `CSV file`:** operation *Convert to Text File*, text input field `{{ $('Webhook').item.json.body.xray_csv_content }}`, file name `{{ $('Webhook').item.json.body.ticket_id }}-xray-tests.csv`.
2. **HTTP Request — `Attach CSV to issue`:** `POST https://<you>.atlassian.net/rest/api/3/issue/{{ $('Webhook').item.json.body.ticket_id }}/attachments`, authentication `Jira`, header `X-Atlassian-Token: no-check`, body *Form-Data* → parameter type **n8n Binary File**, name `file`, input field `data`.

The CSV can also be imported by hand through **Xray → Test Case Importer** if the API import ever fails.

**7.14** Click **Save**, then turn the **Active** toggle on.

✅ **Check:** send a fake callback. Use a real ticket key that is in *Ready for QA*. Note that this **transitions that issue and creates one real Xray test**, which you can delete afterwards.

```bash
curl -s -X POST http://localhost:5678/webhook/qa-result -H 'Content-Type: application/json' -d '{"ticket_id":"SHOP-1","status":"QA Failed","report_markdown":"## test report","xray_csv_content":"\"Test ID\",\"Summary\"\n\"TC-001\",\"demo\"\n","xray_tests_json":[{"testtype":"Manual","fields":{"summary":"[SHOP-1] TC-001 setup check","project":{"key":"SHOP"}},"steps":[{"action":"Open the home page","data":"","result":"Page loads"}],"update":{"issuelinks":[{"add":{"type":{"name":"Test"},"outwardIssue":{"key":"SHOP-1"}}}]},"xray_test_repository_folder":"AI QA/SHOP-1"}]}'
```

The issue should move to *QA Failed*, with the report comment, an `Xray import successful: SHOP-…` comment, and the new test linked in its Xray coverage panel.

---

## Phase 8 — n8n Workflow A: Jira → QA service

**8.1** Create a workflow named `Ready for QA → QA Service`.

**8.2 Jira Trigger node.**
- **Credential:** `Jira`
- **Events:** `jira:issue_updated`
- **Additional Fields → Filter (JQL):** `project = SHOP` (your project key)

**8.3 IF node — `Moved to Ready for QA?`.** This passes only real status changes into *Ready for QA*, not every edit made while the ticket is in that status.
- Condition type **Boolean**:
  `{{ ($json.changelog?.items || []).some(i => i.field === 'status' && i.toString === 'Ready for QA') }}` **is true**

**8.4 HTTP Request — `Find PR`.** This assumes your team puts the Jira key in PR titles (for example `SHOP-123: add promo code`).
- **Method:** `GET`
- **URL:** `https://api.github.com/search/issues?q=repo:<ORG>/<APP_REPO>+is:pr+{{ $('Jira Trigger').item.json.issue.key }}+in:title&sort=updated`
- **Authentication:** Predefined → **GitHub API** → `GitHub`

**8.5 HTTP Request — `Fetch diff`.**
- **Method:** `GET`
- **URL:** `https://api.github.com/repos/<ORG>/<APP_REPO>/pulls/{{ $json.items[0].number }}`
- **Authentication:** Generic → **Header Auth** → `GitHub Diff`
- **Header:** `Accept: application/vnd.github.diff`
- **Options → Response → Response Format:** **Text**, put output in field `data`
- **Settings → On Error:** *Continue*, so a ticket without a PR still gets tested (with an empty diff)

**8.6 Logs (optional — skip on the first pass).**
- **Azure:** an HTTP Request `POST https://api.loganalytics.azure.com/v1/workspaces/<WORKSPACE_ID>/query` with body `{"query": "AppExceptions | where TimeGenerated > ago(24h) | take 50"}` and a Microsoft OAuth2 credential. Name the node `Azure logs`.
- **Database:** a Postgres or MySQL node that runs your slow-query or error-log query. Name it `DB logs`.

**8.7 HTTP Request — `Start QA run`.**
- **Method:** `POST`
- **URL:** `http://localhost:8000/qa/runs`
- **Authentication:** Generic → **Header Auth** → `QA Service`
- **Body:** JSON, *Using Fields Below*. This option escapes the text safely, so diffs full of quotes won't break the request.

| Name | Value |
|---|---|
| `ticket_id` | `{{ $('Jira Trigger').item.json.issue.key }}` |
| `summary` | `{{ $('Jira Trigger').item.json.issue.fields.summary }}` |
| `description` | `{{ typeof $('Jira Trigger').item.json.issue.fields.description === 'string' ? $('Jira Trigger').item.json.issue.fields.description : JSON.stringify($('Jira Trigger').item.json.issue.fields.description ?? '') }}` |
| `github_diff` | `{{ $('Fetch diff').item.json.data ?? '' }}` |
| `target_url` | `https://staging.yourapp.com` (or a Jira custom field) |
| `n8n_callback_url` | `http://localhost:5678/webhook/qa-result` |
| `azure_logs` | `{{ JSON.stringify($('Azure logs').all().map(i => i.json)) }}` (only if you added 8.6) |
| `db_logs` | `{{ JSON.stringify($('DB logs').all().map(i => i.json)) }}` (only if you added 8.6) |

**8.8** Click **Save**, then turn the workflow **Active**. Activating it registers the webhook in Jira automatically.

✅ **Check:** in Jira, go to **Settings (⚙) → System → WebHooks**. You should see a webhook pointing at your n8n tunnel URL.

---

## Phase 9 — End-to-end test

**9.1** Pick or create a small test ticket with clear acceptance criteria, and make sure its PR title contains the ticket key.

**9.2** Move the ticket to **Ready for QA**.

**9.3** Watch the run as it goes:

| Where | What you should see |
|---|---|
| n8n → **Executions** | Workflow A succeeds, and `Start QA run` returns `202` with a `run_id` |
| QA service terminal | Log lines for triage → architect → automation → git → pnpm → report |
| `curl -s -H "X-API-Key: <key>" http://localhost:8000/qa/runs/<run_id>` | `status` goes from `running` to `completed`, with an `outcome` |
| GitHub test repo | A new branch `qa/<TICKET>` containing `tests/qa/<TICKET>.spec.ts` |
| n8n → **Executions** | Workflow B runs when the service calls back |
| Paperclip board (`QA Automation`) | Issue `[TICKET] …` in *In progress*, with a comment as each agent finishes. It ends in *Done* (passed) or *Blocked* (failed), and its cost appears on the agent's spend |
| Jira ticket | Status changed, report comment added, Xray tests linked and filed under `AI QA/<TICKET>` |

Most runs take 3–10 minutes; the Playwright step is the slowest.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Workflow A never triggers | Jira can't reach n8n. Make sure n8n was started with `--tunnel` and the workflow is **active**, then check Jira → WebHooks. |
| `Start QA run` returns **401** | The `X-API-Key` value in n8n doesn't match `QA_SERVICE_API_KEY` in `.env`. |
| `Start QA run` returns **409** | A run for that ticket is already in progress; wait for it to finish. |
| `Start QA run` returns **422** | A required field is empty or invalid (the ticket key format, or a missing URL). The response body names the field. |
| Report says `pnpm is not installed` | uvicorn was started without Node 24 or pnpm on `PATH`. Restart it from a fresh terminal tab. |
| `git push failed` in the report | `GITHUB_TOKEN` is missing *Contents: write* on the test repo, or `TEST_REPO_URL` is wrong. |
| `Cannot check out base branch` | Someone edited files in `TEST_REPO_LOCAL_PATH`. Discard the changes there; that clone belongs to the agent. |
| Report says "Local report synthesizer unavailable" | Ollama isn't running (`ollama serve`). The run still completes, using a simpler report. |
| `BUDGET GUARD` in the report | A token cap was hit. Raise `MAX_CLOUD_TOKENS_PER_RUN`, or look into why that ticket needed so much. |
| `/health` shows `paperclip.reachable: false` | Paperclip isn't running, or `PAPERCLIP_API_URL` is wrong. It should be `http://localhost:3100`, without `/api`. |
| Paperclip returns `401` / `403` in the service log | `PAPERCLIP_API_KEY` isn't an *agent* key for `QA Pipeline`. Create one on that agent's page. |
| Paperclip keeps starting runs of the QA agent on its own | *Wake on demand* or the heartbeat is still on (step 5.3). `/health` warns about this. |
| `BUDGET GUARD - Paperclip agent is paused` | Paperclip paused the agent, usually because its monthly budget was hit. Raise the budget or resume the agent in Paperclip. |
| Xray import comment shows `errors` mentioning the link type | Your Jira has no *Test* link type. Set `XRAY_REQUIREMENT_LINK_TYPE` to Xray's link type on your site (see Jira → Settings → Issues → Issue linking). |
| Xray import errors mention `project` | The tests go to the ticket's project by default. Set `XRAY_PROJECT_KEY` if tests live in a different project. |
| Xray import errors mention `priority` | Keep `XRAY_INCLUDE_PRIORITY=false`: your Jira uses different priority names. |
| `Xray authenticate` returns 401 | Wrong client ID or secret in the `Xray` Custom Auth credential, or the API key was revoked. |
| Workflow B returns 404 | The workflow isn't active, or the callback URL uses `/webhook-test/` instead of `/webhook/`. |
| Jira transition returns 400 | Wrong transition ID, or that transition isn't allowed from the issue's current status. |
