# Setup Guide — Windows

The same stack as [SETUP.md](SETUP.md), on **Windows 10 or 11**, using native Windows tools and **PowerShell**: the QA service, Ollama, the Playwright test repo, Paperclip and n8n, wired to Jira, GitHub and Xray.

Phases 1–5 are specific to Windows and are written out in full below. Phases 6–9 (n8n, Jira and Xray) happen in the browser and are the same on every OS, so this guide sends you to `SETUP.md` for those and gives PowerShell versions of the few terminal commands they use.

Do the phases in order. Each phase ends with a **✅ Check** — don't move on until it passes.

> **Use Windows Terminal with PowerShell tabs.** You'll keep four processes running, one per tab: Ollama (tray app), the QA service, Paperclip and n8n.
>
> **Don't use `curl` in PowerShell 5.1.** There, `curl` is an alias for `Invoke-WebRequest` and takes different arguments. This guide uses `curl.exe` (built into Windows 10 and 11) or `Invoke-RestMethod` instead.

---

## Phase 1 — Machine prerequisites

**1.1 Allow local PowerShell scripts.** npm installs `pnpm` as a PowerShell script, and Windows blocks local scripts by default. This setting applies only to your user account: it allows scripts you create locally, and scripts downloaded from the internet must be signed.

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**1.2 Python 3.12.** The service needs Python 3.10 or newer.

```powershell
winget install -e --id Python.Python.3.12
```

**1.3 Git for Windows.** Version 2.31 or newer is needed.

```powershell
winget install -e --id Git.Git
```

**1.4 Node.js 24 LTS.** Paperclip requires Node 24.11 or newer.

```powershell
winget install -e --id OpenJS.NodeJS.LTS
```

**1.5 Close and reopen Windows Terminal** so it picks up the new `PATH`. Then install pnpm:

```powershell
npm install -g pnpm
```

**1.6 Allow long paths in git.** Without this, deep `node_modules` folders can hit Windows' 260-character path limit.

```powershell
git config --global core.longpaths true
```

✅ **Check:** you should see Python 3.12.x, Node v24.11 or later, and a pnpm and git version. If Node is older than 24.11, run `winget upgrade OpenJS.NodeJS.LTS`.

```powershell
py -3.12 --version; node --version; pnpm --version; git --version
```

---

## Phase 2 — Local model (Ollama + Qwen)

**2.1** Install Ollama. It runs as a **system-tray app** that starts automatically, so there's no `ollama serve` tab on Windows.

```powershell
winget install -e --id Ollama.Ollama
```

**2.2** Download the model (about 4.7 GB):

```powershell
ollama pull qwen2.5-coder:7b
```

**Hardware:** with an NVIDIA GPU (8 GB or more of VRAM), Ollama uses it automatically. With CPU only, the model still works, but each report takes about 1–3 minutes. If Ollama is too slow or unavailable, the service falls back to a simpler report automatically.

✅ **Check:** the endpoint lists `qwen2.5-coder:7b`.

```powershell
curl.exe -s http://localhost:11434/v1/models
```

---

## Phase 3 — Playwright test repository

This is the **TypeScript** repo where the agent commits specs. The service works in its **own dedicated clone**, because it switches branches there. Never point it at the folder you work in.

**3.1** Create the repo on GitHub (for example `reginAbdu/qa-playwright-tests`). Then scaffold it locally, keeping the path **short** to stay clear of path-length problems:

```powershell
mkdir C:\code -Force; cd C:\code; pnpm create playwright@latest qa-playwright-tests
```

When prompted, choose **TypeScript**, tests folder `tests`, **no** GitHub Actions workflow, and **yes** to installing browsers. Windows doesn't need `--with-deps`.

**3.2** Replace `C:\code\qa-playwright-tests\playwright.config.ts` with the config from [SETUP.md → step 3.2](SETUP.md#phase-3--playwright-test-repository). It is the same on every OS.

**3.3** Keep line endings consistent. The agent writes specs with LF line endings; this makes git store them that way for everyone:

```powershell
cd C:\code\qa-playwright-tests; Set-Content .gitattributes "* text=auto eol=lf" -Encoding ascii
```

**3.4** Create the spec folder, then commit and push:

```powershell
New-Item -ItemType Directory tests\qa -Force; New-Item tests\qa\.gitkeep -ItemType File -Force
```

```powershell
git add .; git commit -m "Playwright scaffold"; git branch -M main
```

```powershell
git remote add origin https://github.com/reginAbdu/qa-playwright-tests.git; git push -u origin main
```

**3.5** Create the GitHub token exactly as in [SETUP.md → step 3.4](SETUP.md#phase-3--playwright-test-repository): a fine-grained token scoped only to this repo, with **Contents: Read and write**.

✅ **Check:** the repo on GitHub shows `playwright.config.ts`, `package.json`, `pnpm-lock.yaml`, `.gitattributes` and `tests/qa/`.

---

## Phase 4 — QA service

**4.1** Clone this project and create a virtual environment. This guide calls the venv's `python.exe` directly, so you never need to "activate" it.

```powershell
cd C:\code; git clone https://github.com/reginAbdu/TAF.git; cd TAF
```

```powershell
py -3.12 -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**4.2** Create your `.env`:

```powershell
Copy-Item .env.example .env
```

**4.3** Generate a shared secret for n8n to send to the service:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

**4.4** Open `.env` in an editor (for example `notepad .env`) and set at least these values. **Use forward slashes in paths.** Python handles them fine on Windows, and they avoid backslash-escaping problems in `.env` files.

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | A key from **console.anthropic.com → API Keys** |
| `GITHUB_TOKEN` | The token from step 3.5 |
| `TEST_REPO_URL` | `https://github.com/reginAbdu/qa-playwright-tests.git` |
| `TEST_REPO_LOCAL_PATH` | `C:/qa-agent/qa-playwright-tests` (a **new, non-existent** folder with a short path) |
| `QA_SERVICE_API_KEY` | The value from step 4.3 |
| `PAPERCLIP_WEBHOOK_URL` | Leave **empty** for now (see Phase 5.5) |

**4.5** Start the service and leave it running in its own tab. It binds to `127.0.0.1`, so Windows Firewall won't prompt.

```powershell
cd C:\code\TAF; .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

✅ **Check:** in `/health`, `claude_configured`, `tools.git` and `tools.pnpm` should be `True`.

```powershell
Invoke-RestMethod http://localhost:8000/health | ConvertTo-Json -Depth 5
```

If `tools.pnpm` is `False`, that tab started before pnpm was installed. Open a new tab and start the service again.

---

## Phase 5 — Paperclip (Control Room)

**5.1** Install Paperclip and run its onboarding (needs Node 24.11 or newer, from Phase 1):

```powershell
npx --registry https://registry.npmjs.org paperclipai onboard --yes
```

**5.2** Open **http://localhost:3100**. If the UI doesn't come up, run `npx paperclipai --help` to find the start command for your version.

> **If Paperclip fails on Windows:** it runs its own embedded Postgres database, and native Windows support is less proven than macOS and Linux. If onboarding fails, run Paperclip inside **WSL2** instead. Install WSL (`wsl --install`, then reboot), install Node 24 inside Ubuntu, and run the same `npx … onboard` command there. WSL2 forwards `localhost` ports, so Paperclip is still reachable at `http://localhost:3100` from Windows. Everything else in this guide stays on native Windows.

**5.3** In the UI, create the **company** (`QA Lab`), the **project** (`QA Automation`) and the **agent** (`QA Pipeline`): HTTP adapter, heartbeat **off**. The steps are the same as [SETUP.md → step 5.3](SETUP.md#phase-5--paperclip-control-room).

**5.4** Set the agent's **monthly budget**, then create an **API key**. Note the key, the company ID and the agent ID.

**5.5** Connecting the service to Paperclip is **not ready yet.** Keep `PAPERCLIP_WEBHOOK_URL` empty until `main.py` has a native Paperclip adapter. Runs aren't affected: each event is still written to the service's log.

✅ **Check:** the `QA Pipeline` agent is listed under `QA Lab` and shows its budget.

---

## Phase 6 — n8n: install and credentials

**6.1** Start n8n and leave it running in its own tab:

```powershell
npx n8n start --tunnel
```

n8n listens on all network interfaces, so **Windows Firewall will ask** whether to allow Node.js. Choose **Private networks only**, or cancel. Jira reaches n8n through the tunnel, and the QA service calls it over `localhost`, so neither needs the firewall open.

**6.2** Follow [SETUP.md → Phase 6, steps 6.2–6.4](SETUP.md#phase-6--n8n-install-and-credentials). The Jira API token, the Xray API key and the four n8n credentials are all created in the browser, the same way on every OS.

---

## Phase 7 — n8n Workflow B: results → Jira + Xray

Build this workflow exactly as in [SETUP.md → Phase 7](SETUP.md#phase-7--n8n-workflow-b-results--jira--xray). The callback URL is the same: `http://localhost:5678/webhook/qa-result`.

✅ **Check (PowerShell version).** Send a fake callback. Replace `SHOP-1` with a real ticket in *Ready for QA*; note that this will transition that issue.

```powershell
$body = @{ ticket_id = "SHOP-1"; status = "QA Failed"; report_markdown = "## test report"; xray_csv_content = "`"Test ID`",`"Summary`"`n`"TC-001`",`"demo`"`n" } | ConvertTo-Json
```

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:5678/webhook/qa-result -ContentType "application/json" -Body $body
```

The issue should move to *QA Failed*, with a comment and a CSV attachment.

---

## Phase 8 — n8n Workflow A: Jira → QA service

Build this workflow exactly as in [SETUP.md → Phase 8](SETUP.md#phase-8--n8n-workflow-a-jira--qa-service). The URL for the `Start QA run` node is still `http://localhost:8000/qa/runs`.

---

## Phase 9 — End-to-end test

Follow [SETUP.md → Phase 9](SETUP.md#phase-9--end-to-end-test). To check a run's status from PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/qa/runs/<run_id> -Headers @{ "X-API-Key" = "<QA_SERVICE_API_KEY>" } | ConvertTo-Json -Depth 5
```

---

## Windows-specific troubleshooting

For problems that aren't specific to Windows, see [SETUP.md → Troubleshooting](SETUP.md#troubleshooting).

| Symptom | Cause / fix |
|---|---|
| `pnpm : ... running scripts is disabled on this system` | Run step 1.1 (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`), then open a new tab. |
| `py` or `node` is not recognized | The terminal was opened before the install. Close all Windows Terminal windows and reopen. |
| Report says `pnpm is not installed or not on PATH` | uvicorn was started in a tab that predates the pnpm install. Restart it from a new tab. |
| `Filename too long` during git clone or `pnpm install` | Run step 1.6 (`core.longpaths`) and keep `TEST_REPO_LOCAL_PATH` short, for example `C:/qa-agent/...`. |
| `pnpm install` is very slow | Windows Defender scans every file in `node_modules`. It still works, but the first install can take several minutes. |
| Every changed line shows up in `git diff` of a spec | Line-ending mismatch. Make sure the test repo has the `.gitattributes` from step 3.3. |
| `EPERM` / `EBUSY` during `pnpm install` | Another process (an editor, or a still-running Playwright) has files open in `TEST_REPO_LOCAL_PATH`. Close it and re-run. |
| `ConvertTo-Json` output is cut off with `...` | Add `-Depth 5`, as the commands in this guide already do. |
| Paperclip onboarding fails | Use WSL2 for Paperclip only (see the note in Phase 5). |
