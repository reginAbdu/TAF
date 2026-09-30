"""
Autonomous QA Multi-Agent Framework
===================================

FastAPI + LangChain + LangGraph service that turns a Jira "Ready for QA" ticket
into an executed, reported, Xray-importable QA cycle.

End-to-end flow
---------------
    Jira ──webhook──▶ n8n ──POST /qa/runs──▶ THIS SERVICE ──callback──▶ n8n ──▶ Jira + Xray
                                                │
                                                └──events──▶ Paperclip (Control Room / budget audit)

LangGraph pipeline (one run per ticket)
---------------------------------------
    START
      │
      ▼
    [1] intake_triager        (Claude, cloud)   analyse acceptance criteria + PR diff + env logs
      │
      ▼
    [2] test_architect        (Claude, cloud)   functional / non-functional plan + Xray CSV matrix
      │
      ▼
    [3] automation_engineer   (Claude, cloud)   Playwright TypeScript spec
      │                       (local CLI)       write file → git branch/commit/push → pnpm install → playwright test
      ▼
    [4] report_synthesizer    (Qwen, local)     raw terminal logs → Markdown QA report
      │
      ▼
    [5] closeout              (HTTP)            POST results to the n8n callback URL
      │
      ▼
     END

If a cloud node aborts (token budget exceeded, unrecoverable LLM error) the graph
short-circuits straight to the report synthesizer so that n8n *always* receives a
callback with a "QA Failed" status and an explanation - a run never silently dies.

Hybrid LLM routing
------------------
* High-reasoning work (nodes 1-3) → Anthropic Claude via `langchain-anthropic`.
* Cheap parsing / formatting work (node 4) → local Ollama (Qwen 2.5-Coder) via the
  OpenAI-compatible endpoint at http://localhost:11434/v1 using `langchain-openai`.
  If Ollama is down, a deterministic Python fallback report is produced instead.

Cost control
------------
Every Claude call passes through `TokenBudget`, which enforces a per-run and a
per-day cloud token cap and streams usage to Paperclip. Once a cap is hit, no
further cloud calls are made for that run.
"""

from __future__ import annotations

import base64
import csv
import functools
import hmac
import io
import json
import logging
import operator
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Type, TypedDict
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, HttpUrl, field_validator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()


# =============================================================================
# 1. CONFIGURATION
# =============================================================================


def _env_str(name: str, default: str = "") -> str:
    """Read a string env var, stripping whitespace; empty values fall back to default."""
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to `default` on missing/invalid values."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("qa.config").warning("Invalid integer for %s=%r, using %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var ("1", "true", "yes", "on" are truthy)."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """All runtime configuration, resolved once at import time from the environment / .env."""

    # --- Cloud LLM (Claude) -------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _env_str("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env_str("CLAUDE_MODEL", "claude-sonnet-5-5"))
    claude_max_tokens: int = field(default_factory=lambda: _env_int("CLAUDE_MAX_TOKENS", 8192))
    claude_timeout_s: int = field(default_factory=lambda: _env_int("CLAUDE_TIMEOUT_SECONDS", 300))

    # --- Local LLM (Ollama / Qwen) -----------------------------------------
    ollama_base_url: str = field(default_factory=lambda: _env_str("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    ollama_model: str = field(default_factory=lambda: _env_str("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    ollama_timeout_s: int = field(default_factory=lambda: _env_int("OLLAMA_TIMEOUT_SECONDS", 300))

    # --- TypeScript test repository ----------------------------------------
    github_token: str = field(default_factory=lambda: _env_str("GITHUB_TOKEN"))
    test_repo_url: str = field(default_factory=lambda: _env_str("TEST_REPO_URL"))
    test_repo_local_path: str = field(default_factory=lambda: _env_str("TEST_REPO_LOCAL_PATH"))
    test_repo_base_branch: str = field(default_factory=lambda: _env_str("TEST_REPO_BASE_BRANCH", "main"))
    test_spec_dir: str = field(default_factory=lambda: _env_str("TEST_SPEC_DIR", "tests/qa"))
    git_push_enabled: bool = field(default_factory=lambda: _env_bool("GIT_PUSH_ENABLED", True))
    git_author_name: str = field(default_factory=lambda: _env_str("GIT_AUTHOR_NAME", "QA Agent"))
    git_author_email: str = field(default_factory=lambda: _env_str("GIT_AUTHOR_EMAIL", "qa-agent@users.noreply.github.com"))
    git_timeout_s: int = field(default_factory=lambda: _env_int("GIT_TIMEOUT_SECONDS", 120))

    # --- pnpm / Playwright execution ---------------------------------------
    pnpm_install_timeout_s: int = field(default_factory=lambda: _env_int("PNPM_INSTALL_TIMEOUT_SECONDS", 600))
    playwright_timeout_s: int = field(default_factory=lambda: _env_int("PLAYWRIGHT_TIMEOUT_SECONDS", 1800))
    playwright_install_browsers: bool = field(default_factory=lambda: _env_bool("PLAYWRIGHT_INSTALL_BROWSERS", False))
    playwright_extra_args: str = field(default_factory=lambda: _env_str("PLAYWRIGHT_EXTRA_ARGS", "--reporter=line"))

    # --- Budget / Paperclip Control Room -----------------------------------
    max_cloud_tokens_per_run: int = field(default_factory=lambda: _env_int("MAX_CLOUD_TOKENS_PER_RUN", 250_000))
    max_cloud_tokens_per_day: int = field(default_factory=lambda: _env_int("MAX_CLOUD_TOKENS_PER_DAY", 2_000_000))
    paperclip_webhook_url: str = field(default_factory=lambda: _env_str("PAPERCLIP_WEBHOOK_URL"))
    paperclip_api_key: str = field(default_factory=lambda: _env_str("PAPERCLIP_API_KEY"))

    # --- Service ------------------------------------------------------------
    qa_service_api_key: str = field(default_factory=lambda: _env_str("QA_SERVICE_API_KEY"))
    max_diff_chars: int = field(default_factory=lambda: _env_int("MAX_DIFF_CHARS", 120_000))
    max_env_log_chars: int = field(default_factory=lambda: _env_int("MAX_ENV_LOG_CHARS", 40_000))
    max_exec_log_chars_for_llm: int = field(default_factory=lambda: _env_int("MAX_EXEC_LOG_CHARS_FOR_LLM", 60_000))
    callback_timeout_s: int = field(default_factory=lambda: _env_int("CALLBACK_TIMEOUT_SECONDS", 30))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))


SETTINGS = Settings()


# =============================================================================
# 2. LOGGING (with secret redaction)
# =============================================================================


class _RedactSecretsFilter(logging.Filter):
    """Scrubs known secrets from every log record so tokens never land in log files."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            for secret in self._secrets:
                message = message.replace(secret, "***REDACTED***")
            record.msg, record.args = message, None
        return True


def redact(text: str) -> str:
    """Remove configured secrets from arbitrary text (subprocess output, error strings, ...)."""
    for secret in (SETTINGS.github_token, SETTINGS.anthropic_api_key, SETTINGS.paperclip_api_key):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***REDACTED***")
    return text


logging.basicConfig(
    level=getattr(logging, SETTINGS.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(
        _RedactSecretsFilter([SETTINGS.github_token, SETTINGS.anthropic_api_key, SETTINGS.paperclip_api_key])
    )
log = logging.getLogger("qa")


# =============================================================================
# 3. GENERIC HELPERS
# =============================================================================

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CODE_FENCE_RE = re.compile(r"```(?:typescript|ts|tsx|javascript|js)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_ansi(text: str) -> str:
    """Remove terminal colour codes so logs are readable by LLMs and in Jira comments."""
    return _ANSI_RE.sub("", text)


def truncate_middle(text: str, limit: int) -> str:
    """
    Keep the head and (larger) tail of a long text. Test/CI logs put the verdict at
    the end, so the tail is weighted 2:1 over the head.
    """
    if not text or len(text) <= limit:
        return text or ""
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n\n... [truncated {len(text) - limit:,} characters] ...\n\n{text[-tail:]}"


def extract_code_block(text: str) -> str:
    """Return the largest fenced code block from an LLM answer, or the raw text if unfenced."""
    blocks = _CODE_FENCE_RE.findall(text or "")
    code = max(blocks, key=len) if blocks else (text or "")
    return code.strip() + "\n"


def message_text(message: BaseMessage) -> str:
    """Flatten a LangChain message's content (str or list of content blocks) into plain text."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def token_usage(message: Any) -> int:
    """Total (input + output) tokens reported by the provider for a single LLM response."""
    usage = getattr(message, "usage_metadata", None) or {}
    total = usage.get("total_tokens")
    if total is None:
        total = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    return int(total or 0)


def _retrying_session(total_retries: int = 4) -> requests.Session:
    """
    HTTP session that retries connection failures and transient gateway errors.
    500 is intentionally NOT retried on POSTs to avoid double-posting Jira comments
    when n8n partially processed the request.
    """
    session = requests.Session()
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=1,
        backoff_factor=1.5,
        status_forcelist=(429, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# =============================================================================
# 4. PAPERCLIP CONTROL ROOM (event stream) + TOKEN BUDGET (financial guard)
# =============================================================================


class ControlRoom:
    """
    Fire-and-forget event emitter for the Paperclip Control Room.

    Every run / agent lifecycle transition and every cloud-token spend is POSTed as a
    small JSON event to PAPERCLIP_WEBHOOK_URL so the Kanban board and live agent
    output stay in sync. Delivery happens on a background thread pool: a slow or
    offline Paperclip can never block or fail a QA run.
    """

    def __init__(self, url: str, api_key: str) -> None:
        self._url = url
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paperclip")
        self._session = _retrying_session(total_retries=2)
        self._log = logging.getLogger("qa.paperclip")

    def emit(self, run_id: str, ticket_id: str, event: str, **data: Any) -> None:
        payload = {"run_id": run_id, "ticket_id": ticket_id, "event": event, "timestamp": utc_now(), "data": data}
        self._log.info("[%s] %s %s", ticket_id, event, json.dumps(data, default=str)[:500])
        if self._url:
            self._pool.submit(self._post, payload)

    def _post(self, payload: Dict[str, Any]) -> None:
        try:
            response = self._session.post(self._url, json=payload, headers=self._headers, timeout=5)
            if response.status_code >= 400:
                self._log.warning("Paperclip rejected event (%s): %s", response.status_code, response.text[:200])
        except requests.RequestException as exc:
            self._log.debug("Paperclip unreachable: %s", exc)


class BudgetExceededError(RuntimeError):
    """Raised before a cloud LLM call when the per-run or per-day token cap is exhausted."""


class TokenBudget:
    """
    Thread-safe cloud token accountant.

    * `ensure_available` is called BEFORE every Claude call and refuses the call once
      a cap is reached - this is what stops runaway spending loops.
    * `record` is called AFTER every call with the provider-reported usage.
    A single call can overshoot a cap by at most `CLAUDE_MAX_TOKENS` output tokens
    plus its prompt, so size the caps with that headroom in mind.
    """

    def __init__(self, per_run_cap: int, per_day_cap: int, control_room: ControlRoom) -> None:
        self._per_run_cap = per_run_cap
        self._per_day_cap = per_day_cap
        self._control_room = control_room
        self._lock = threading.Lock()
        self._run_usage: Dict[str, int] = {}
        self._day = date.today()
        self._day_usage = 0

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day, self._day_usage = today, 0

    def ensure_available(self, run_id: str) -> None:
        with self._lock:
            self._roll_day()
            used_run = self._run_usage.get(run_id, 0)
            if self._per_run_cap > 0 and used_run >= self._per_run_cap:
                raise BudgetExceededError(
                    f"Per-run cloud token cap reached ({used_run:,}/{self._per_run_cap:,} tokens)."
                )
            if self._per_day_cap > 0 and self._day_usage >= self._per_day_cap:
                raise BudgetExceededError(
                    f"Daily cloud token cap reached ({self._day_usage:,}/{self._per_day_cap:,} tokens)."
                )

    def record(self, run_id: str, ticket_id: str, agent: str, tokens: int) -> None:
        with self._lock:
            self._roll_day()
            self._run_usage[run_id] = self._run_usage.get(run_id, 0) + tokens
            self._day_usage += tokens
            run_total, day_total = self._run_usage[run_id], self._day_usage
        self._control_room.emit(
            run_id,
            ticket_id,
            "budget.updated",
            agent=agent,
            tokens=tokens,
            run_total=run_total,
            run_cap=self._per_run_cap,
            day_total=day_total,
            day_cap=self._per_day_cap,
        )

    def run_usage(self, run_id: str) -> int:
        with self._lock:
            return self._run_usage.get(run_id, 0)

    def release(self, run_id: str) -> int:
        """Forget a finished run's counter (the daily total is kept) and return its final usage."""
        with self._lock:
            return self._run_usage.pop(run_id, 0)


CONTROL_ROOM = ControlRoom(SETTINGS.paperclip_webhook_url, SETTINGS.paperclip_api_key)
BUDGET = TokenBudget(SETTINGS.max_cloud_tokens_per_run, SETTINGS.max_cloud_tokens_per_day, CONTROL_ROOM)


# =============================================================================
# 5. LLM CLIENTS (hybrid routing)
# =============================================================================


class LLMOutputError(RuntimeError):
    """Raised when an LLM answer cannot be parsed into the expected structure."""


@functools.lru_cache(maxsize=1)
def claude_llm() -> ChatAnthropic:
    """Cloud 'analytical brain' used by agents 1-3."""
    if not SETTINGS.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")
    return ChatAnthropic(
        model=SETTINGS.claude_model,
        api_key=SETTINGS.anthropic_api_key,
        max_tokens=SETTINGS.claude_max_tokens,
        temperature=0.2,
        timeout=SETTINGS.claude_timeout_s,
        max_retries=3,
    )


@functools.lru_cache(maxsize=1)
def qwen_llm() -> ChatOpenAI:
    """Free local model (Ollama, OpenAI-compatible API) used by agent 4."""
    return ChatOpenAI(
        model=SETTINGS.ollama_model,
        base_url=SETTINGS.ollama_base_url,
        api_key="ollama",  # Ollama ignores the key but the OpenAI client requires one.
        temperature=0.0,
        timeout=SETTINGS.ollama_timeout_s,
        max_retries=1,
    )


def call_claude_text(state: "QAState", agent: str, messages: List[BaseMessage]) -> str:
    """Budget-guarded free-text Claude call."""
    BUDGET.ensure_available(state["run_id"])
    response = claude_llm().invoke(messages)
    BUDGET.record(state["run_id"], state["ticket_id"], agent, token_usage(response))
    return message_text(response)


def call_claude_structured(
    state: "QAState", agent: str, messages: List[BaseMessage], schema: Type[BaseModel]
) -> BaseModel:
    """
    Budget-guarded Claude call that returns a validated Pydantic object (tool-calling
    under the hood). `include_raw=True` keeps the raw AIMessage so usage is still billed.
    """
    BUDGET.ensure_available(state["run_id"])
    result = claude_llm().with_structured_output(schema, include_raw=True).invoke(messages)
    BUDGET.record(state["run_id"], state["ticket_id"], agent, token_usage(result.get("raw")))
    parsed = result.get("parsed")
    if parsed is None:
        raise LLMOutputError(f"{agent}: could not parse structured output: {result.get('parsing_error')}")
    return parsed


# =============================================================================
# 6. STRUCTURED LLM OUTPUT SCHEMAS
# =============================================================================


class TriageAnalysis(BaseModel):
    """Output of Agent 1 - the Intake Triager."""

    feature_summary: str = Field(description="Plain-language summary of what changed and why.")
    acceptance_criteria: List[str] = Field(description="Explicit, testable acceptance criteria (derived if missing).")
    impacted_areas: List[str] = Field(description="Pages, components, APIs or data flows touched by the diff.")
    risk_level: Literal["low", "medium", "high", "critical"]
    risks: List[str] = Field(description="Concrete regression / defect risks worth testing.")
    environment_findings: List[str] = Field(
        default_factory=list, description="Relevant signals from Azure / DB logs (errors, slow queries, ...)."
    )
    gaps_and_questions: List[str] = Field(
        default_factory=list, description="Ambiguities in the ticket that QA should flag to the team."
    )


class TestStep(BaseModel):
    action: str = Field(description="What the tester / automation does.")
    data: str = Field(default="", description="Input data for the step, if any.")
    expected_result: str = Field(description="Observable expected outcome.")


class TestCase(BaseModel):
    test_id: str = Field(description="Stable ID such as TC-001.")
    summary: str = Field(description="One-line test title.")
    kind: Literal["functional", "non-functional"]
    category: str = Field(description="e.g. happy-path, negative, edge-case, regression, performance, accessibility, security.")
    priority: Literal["Highest", "High", "Medium", "Low", "Lowest"]
    automatable: bool = Field(description="True if this can be automated with Playwright against the target URL.")
    preconditions: str = Field(default="")
    acceptance_criteria_refs: List[str] = Field(default_factory=list, description="Which acceptance criteria this covers.")
    steps: List[TestStep] = Field(min_length=1)


class TestPlan(BaseModel):
    """Output of Agent 2 - the Test Architect."""

    strategy: str = Field(description="Short overall test strategy.")
    in_scope: List[str]
    out_of_scope: List[str] = Field(default_factory=list)
    test_cases: List[TestCase] = Field(min_length=1)


# =============================================================================
# 7. LANGGRAPH SHARED STATE
# =============================================================================


class QAState(TypedDict, total=False):
    # --- Required contract (from the brief) --------------------------------
    ticket_id: str
    summary: str
    description: str
    github_diff: str
    target_url: str
    test_plan: str                      # Markdown rendering of the plan
    generated_typescript_code: str
    execution_logs: str
    execution_passed: bool
    xray_csv_content: str
    final_report: str

    # --- Operational fields -------------------------------------------------
    run_id: str
    n8n_callback_url: str
    environment_logs: str               # Azure / DB logs gathered by n8n
    triage_analysis: Dict[str, Any]
    test_cases: List[Dict[str, Any]]
    spec_file_path: str
    git_branch: str
    git_pushed: bool
    callback_delivered: bool
    aborted: bool                       # True → skip remaining cloud nodes
    errors: Annotated[List[str], operator.add]  # reducer: nodes append, never overwrite


# =============================================================================
# 8. SUBPROCESS / GIT / PNPM EXECUTION LAYER
# =============================================================================


class CommandError(RuntimeError):
    """A local CLI command failed (non-zero exit, timeout, or missing binary)."""

    def __init__(self, message: str, result: Optional["CommandResult"] = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_log(self) -> str:
        """Human-readable log block used in execution_logs."""
        header = f"$ {' '.join(self.args)}\n# exit={self.returncode} duration={self.duration_s:.1f}s"
        if self.timed_out:
            header += " TIMED OUT"
        body = ""
        if self.stdout.strip():
            body += f"\n--- stdout ---\n{self.stdout.rstrip()}"
        if self.stderr.strip():
            body += f"\n--- stderr ---\n{self.stderr.rstrip()}"
        return header + body


def _decode(stream: Any) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


def run_command(
    args: List[str],
    cwd: Path,
    timeout_s: int,
    env: Optional[Dict[str, str]] = None,
    check: bool = False,
) -> CommandResult:
    """
    Run a CLI command without a shell (no injection surface), capture stdout/stderr,
    strip ANSI codes and redact secrets. Never raises for a non-zero exit unless
    `check=True`; always raises CommandError for a missing binary.
    """
    started = time.monotonic()
    merged_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=merged_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        result = CommandResult(
            args=args,
            returncode=proc.returncode,
            stdout=redact(strip_ansi(proc.stdout or "")),
            stderr=redact(strip_ansi(proc.stderr or "")),
            duration_s=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            args=args,
            returncode=-1,
            stdout=redact(strip_ansi(_decode(exc.stdout))),
            stderr=redact(strip_ansi(_decode(exc.stderr))) + f"\n[process killed after {timeout_s}s timeout]",
            duration_s=time.monotonic() - started,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Executable not found: {args[0]!r} ({exc})") from exc
    except OSError as exc:
        raise CommandError(f"Could not start {args[0]!r}: {exc}") from exc

    log.debug("cmd=%s exit=%s %.1fs", " ".join(args), result.returncode, result.duration_s)
    if check and not result.ok:
        raise CommandError(f"Command failed: {' '.join(args)} (exit {result.returncode})", result)
    return result


class TestRepository:
    """
    Wraps the local clone of the TypeScript Playwright repository.

    Authentication: GITHUB_TOKEN is injected per-command through git's
    GIT_CONFIG_COUNT/KEY/VALUE environment variables as an HTTP Authorization
    header. The token therefore never appears in the remote URL, `.git/config`,
    the process argument list, or logs.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.test_repo_local_path).expanduser().resolve() if settings.test_repo_local_path else None

    # ------------------------------------------------------------------ git --
    def _git_env(self) -> Dict[str, str]:
        env = {
            "GIT_TERMINAL_PROMPT": "0",  # never hang waiting for a username/password prompt
            "GIT_AUTHOR_NAME": self.settings.git_author_name,
            "GIT_AUTHOR_EMAIL": self.settings.git_author_email,
            "GIT_COMMITTER_NAME": self.settings.git_author_name,
            "GIT_COMMITTER_EMAIL": self.settings.git_author_email,
        }
        token = self.settings.github_token
        host = urlparse(self.settings.test_repo_url).hostname if self.settings.test_repo_url else "github.com"
        if token and host:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
                }
            )
        return env

    def git(self, *args: str, check: bool = True, cwd: Optional[Path] = None) -> CommandResult:
        return run_command(
            ["git", *args],
            cwd=cwd or self.path,  # type: ignore[arg-type]
            timeout_s=self.settings.git_timeout_s,
            env=self._git_env(),
            check=check,
        )

    def ensure_ready(self, logs: List[str]) -> None:
        """Validate the local clone, cloning it from TEST_REPO_URL on first use."""
        if self.path is None:
            raise CommandError("TEST_REPO_LOCAL_PATH is not configured.")
        if shutil.which("git") is None:
            raise CommandError("git is not installed or not on PATH.")

        if (self.path / ".git").is_dir():
            return
        if self.path.exists() and any(self.path.iterdir()):
            raise CommandError(f"{self.path} exists, is not empty, and is not a git repository.")
        if not self.settings.test_repo_url:
            raise CommandError(f"{self.path} is not a git clone and TEST_REPO_URL is not set to clone it.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(
            ["git", "clone", self.settings.test_repo_url, str(self.path)],
            cwd=self.path.parent,
            timeout_s=max(self.settings.git_timeout_s, 600),
            env=self._git_env(),
        )
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError("git clone failed", result)

    def publish_spec(self, ticket_id: str, code: str, logs: List[str]) -> Tuple[Path, str, bool]:
        """
        Write the spec on a fresh `qa/{ticket_id}` branch, commit and push it.

        Returns (spec_path, branch, pushed). Remote problems (fetch/pull/push) are logged
        and tolerated so tests still run locally; local problems (checkout, write,
        commit) raise CommandError because the spec would not be in a known state.
        """
        assert self.path is not None
        base = self.settings.test_repo_base_branch
        branch = f"qa/{ticket_id}"

        # 1. Sync the base branch (best effort - offline runs still work).
        result = self.git("fetch", "origin", "--prune", check=False)
        logs.append(result.as_log())
        if not result.ok:
            logs.append("[warn] git fetch failed - continuing with the local state of the repository.")

        result = self.git("checkout", base, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError(
                f"Cannot check out base branch '{base}'. Is the working tree dirty? "
                "Use a dedicated clone for the QA agent.",
                result,
            )
        result = self.git("pull", "--ff-only", "origin", base, check=False)
        logs.append(result.as_log())
        if not result.ok:
            logs.append(f"[warn] git pull --ff-only origin {base} failed - using local '{base}'.")

        # 2. Create (or reset, on re-runs of the same ticket) the QA branch from base.
        #    `-B` is the idempotent form of `checkout -b`.
        result = self.git("checkout", "-B", branch, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError(f"git checkout -B {branch} failed", result)

        # 3. Write the spec file (path is confined to the repository).
        spec_dir = (self.path / self.settings.test_spec_dir).resolve()
        if self.path != spec_dir and self.path not in spec_dir.parents:
            raise CommandError(f"TEST_SPEC_DIR escapes the repository: {self.settings.test_spec_dir}")
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / f"{ticket_id}.spec.ts"
        spec_path.write_text(code, encoding="utf-8")
        rel_spec = spec_path.relative_to(self.path).as_posix()
        logs.append(f"[info] wrote {rel_spec} ({len(code):,} bytes)")

        # 4. Stage + commit (skip commit if the content is identical to HEAD).
        result = self.git("add", "--", rel_spec, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError("git add failed", result)

        staged = self.git("diff", "--cached", "--quiet", "--", rel_spec, check=False)
        if staged.returncode == 0:
            logs.append("[info] spec unchanged since last commit - nothing to commit.")
        else:
            result = self.git(
                "commit", "-m", f"test({ticket_id}): add AI-generated Playwright spec",
                "-m", "Generated by the autonomous QA multi-agent framework.",
                check=False,
            )
            logs.append(result.as_log())
            if not result.ok:
                raise CommandError("git commit failed", result)

        # 5. Push (tolerated failure). A rejected push of our own qa/* branch is
        #    retried with --force-with-lease, which only overwrites what we last fetched.
        pushed = False
        if not self.settings.git_push_enabled:
            logs.append("[info] GIT_PUSH_ENABLED=false - skipping git push.")
        else:
            result = self.git("push", "-u", "origin", branch, check=False)
            logs.append(result.as_log())
            if not result.ok and ("rejected" in result.stderr or "non-fast-forward" in result.stderr):
                result = self.git("push", "--force-with-lease", "-u", "origin", branch, check=False)
                logs.append(result.as_log())
            pushed = result.ok
            if not pushed:
                logs.append("[warn] git push failed - spec is committed locally only. Check GITHUB_TOKEN scopes.")

        return spec_path, branch, pushed

    # ----------------------------------------------------------- pnpm / PW --
    def run_playwright(self, spec_path: Path, target_url: str, logs: List[str]) -> bool:
        """`pnpm install` then `pnpm exec playwright test <spec>`. Returns True only if tests pass."""
        assert self.path is not None
        if shutil.which("pnpm") is None:
            logs.append("[error] pnpm is not installed or not on PATH (try: corepack enable pnpm).")
            return False

        env = {
            "CI": "1",               # Playwright: no interactive HTML report server, retries per config
            "FORCE_COLOR": "0",
            "NO_COLOR": "1",
            "BASE_URL": target_url,  # consumed by the generated spec and playwright.config.ts
            "PLAYWRIGHT_BASE_URL": target_url,
        }

        install = run_command(["pnpm", "install"], cwd=self.path, timeout_s=self.settings.pnpm_install_timeout_s, env=env)
        logs.append(install.as_log())
        if not install.ok:
            logs.append("[error] pnpm install failed - tests were not executed.")
            return False

        if self.settings.playwright_install_browsers:
            browsers = run_command(
                ["pnpm", "exec", "playwright", "install", "--with-deps", "chromium"],
                cwd=self.path, timeout_s=self.settings.pnpm_install_timeout_s, env=env,
            )
            logs.append(browsers.as_log())

        rel_spec = spec_path.relative_to(self.path).as_posix()
        extra = self.settings.playwright_extra_args.split() if self.settings.playwright_extra_args else []
        test = run_command(
            ["pnpm", "exec", "playwright", "test", rel_spec, *extra],
            cwd=self.path, timeout_s=self.settings.playwright_timeout_s, env=env,
        )
        logs.append(test.as_log())
        return test.ok


TEST_REPO = TestRepository(SETTINGS)

# The local clone is a single shared working tree: git checkout + test execution for
# one ticket must never interleave with another ticket's run.
REPO_LOCK = threading.Lock()


# =============================================================================
# 9. RENDERING HELPERS (Markdown test plan, Xray CSV, report header)
# =============================================================================

XRAY_CSV_HEADERS = [
    "Test ID", "Summary", "Test Type", "Priority", "Labels",
    "Requirement", "Preconditions", "Action", "Data", "Expected Result",
]


def _label(value: str) -> str:
    """Jira labels cannot contain whitespace."""
    return re.sub(r"\s+", "-", value.strip())


def build_xray_csv(plan: TestPlan, ticket_id: str) -> str:
    """
    Build an Xray Test Case Importer CSV. Each test step is one row; subsequent steps
    of the same test repeat only the Test ID (Xray groups rows by that column).
    Using the csv module (QUOTE_ALL) guarantees commas/quotes/newlines in LLM text
    never corrupt the file.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(XRAY_CSV_HEADERS)
    for index, case in enumerate(plan.test_cases, start=1):
        test_id = case.test_id.strip() or f"TC-{index:03d}"
        labels = ";".join(
            sorted({_label(l) for l in ("ai-generated", case.kind, case.category, ticket_id) if l.strip()})
        )
        for step_no, step in enumerate(case.steps):
            first = step_no == 0
            writer.writerow([
                test_id,
                f"[{ticket_id}] {case.summary}" if first else "",
                "Manual" if first else "",
                case.priority if first else "",
                labels if first else "",
                ticket_id if first else "",
                case.preconditions if first else "",
                step.action,
                step.data,
                step.expected_result,
            ])
    return buffer.getvalue()


def render_test_plan_markdown(plan: TestPlan) -> str:
    lines = ["## Strategy", plan.strategy, "", "## Scope", "**In scope**"]
    lines += [f"- {item}" for item in plan.in_scope]
    if plan.out_of_scope:
        lines += ["", "**Out of scope**"] + [f"- {item}" for item in plan.out_of_scope]
    lines += ["", "## Test cases"]
    for case in plan.test_cases:
        auto = "automated" if case.automatable else "manual"
        lines += ["", f"### {case.test_id} - {case.summary}",
                  f"*{case.kind} / {case.category} / priority {case.priority} / {auto}*"]
        if case.preconditions:
            lines.append(f"**Preconditions:** {case.preconditions}")
        for n, step in enumerate(case.steps, start=1):
            data = f" (data: `{step.data}`)" if step.data else ""
            lines.append(f"{n}. {step.action}{data} → _{step.expected_result}_")
    return "\n".join(lines)


_PW_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|flaky|skipped|did not run|interrupted)", re.IGNORECASE)


def parse_playwright_counts(logs: str) -> Dict[str, int]:
    """Extract the final Playwright summary counters (e.g. '3 passed', '1 failed')."""
    counts: Dict[str, int] = {}
    for number, label in _PW_COUNT_RE.findall(logs or ""):
        counts[label.lower()] = int(number)  # last occurrence wins = final summary line
    return counts


# =============================================================================
# 10. AGENT NODES
# =============================================================================


def agent_node(name: str, engine: str, abort_on_error: bool) -> Callable:
    """
    Decorator that gives every node the same operational envelope:
      * skips work if an upstream node already aborted the run (cloud nodes only),
      * emits agent.started / agent.completed / agent.failed events to Paperclip,
      * converts BudgetExceededError and unexpected exceptions into state errors
        instead of crashing the graph.
    """

    def decorator(fn: Callable[[QAState], Dict[str, Any]]) -> Callable[[QAState], Dict[str, Any]]:
        @functools.wraps(fn)
        def wrapper(state: QAState) -> Dict[str, Any]:
            run_id, ticket_id = state["run_id"], state["ticket_id"]
            if abort_on_error and state.get("aborted"):
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.skipped", agent=name, reason="run aborted upstream")
                return {}
            CONTROL_ROOM.emit(run_id, ticket_id, "agent.started", agent=name, engine=engine)
            started = time.monotonic()
            try:
                update = fn(state) or {}
                CONTROL_ROOM.emit(
                    run_id, ticket_id, "agent.completed", agent=name, engine=engine,
                    duration_s=round(time.monotonic() - started, 1), cloud_tokens_run_total=BUDGET.run_usage(run_id),
                )
                return update
            except BudgetExceededError as exc:
                message = f"{name}: BUDGET GUARD - {exc}"
                log.error("[%s] %s", ticket_id, message)
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.failed", agent=name, reason="budget_exceeded", error=str(exc))
                return {"errors": [message], "aborted": True, "execution_passed": False}
            except Exception as exc:  # noqa: BLE001 - a node must never take down the graph
                message = f"{name}: {type(exc).__name__}: {redact(str(exc))}"
                log.exception("[%s] %s", ticket_id, message)
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.failed", agent=name, error=message)
                failure: Dict[str, Any] = {"errors": [message]}
                if abort_on_error:
                    failure.update({"aborted": True, "execution_passed": False})
                return failure

        return wrapper

    return decorator


# ----------------------------------------------------------------- Node 1 ----
TRIAGE_SYSTEM_PROMPT = """You are the Intake Triager in an autonomous QA team - a principal QA engineer.
You receive a Jira ticket that has just moved to "Ready for QA", the GitHub PR diff that implements it,
and recent Azure / database logs from the target environment.

Your job:
1. Extract the explicit acceptance criteria. If they are missing or vague, derive precise, testable ones
   from the description and the diff, and list the ambiguities under gaps_and_questions.
2. Map the diff to impacted user-facing areas (pages, components, API endpoints, data flows).
3. Identify concrete regression and defect risks introduced by the change (not generic advice).
4. Pull out anything in the environment logs that matters for testing (errors, timeouts, slow queries).
5. Assign an overall risk level.
Be specific and grounded in the provided material; never invent features that are not in the ticket or diff."""


@agent_node("Intake Triager", engine="claude", abort_on_error=True)
def intake_triager(state: QAState) -> Dict[str, Any]:
    diff = truncate_middle(state.get("github_diff", ""), SETTINGS.max_diff_chars) or "(no diff provided)"
    env_logs = truncate_middle(state.get("environment_logs", ""), SETTINGS.max_env_log_chars) or "(no logs provided)"
    prompt = f"""# Jira ticket {state['ticket_id']}
## Summary
{state.get('summary', '')}

## Description / acceptance criteria
{state.get('description', '') or '(empty)'}

## Target environment
{state.get('target_url', '')}

## GitHub PR diff
```diff
{diff}
```

## Azure / DB logs
```
{env_logs}
```"""
    analysis = call_claude_structured(
        state, "Intake Triager",
        [SystemMessage(content=TRIAGE_SYSTEM_PROMPT), HumanMessage(content=prompt)],
        TriageAnalysis,
    )
    return {"triage_analysis": analysis.model_dump()}


# ----------------------------------------------------------------- Node 2 ----
ARCHITECT_SYSTEM_PROMPT = """You are the Test Architect in an autonomous QA team.
From the triage analysis you design a complete, risk-based test plan for the change.

Rules:
- Cover every acceptance criterion with at least one functional test (happy path, negative, edge cases).
- Add non-functional tests where the change warrants it: performance (e.g. page load / API latency budgets),
  accessibility (WCAG 2.1 AA basics), security (authz, input validation), resilience, cross-browser.
- Every test case has concrete, observable expected results - never "works correctly".
- Mark `automatable=true` only for cases Playwright can execute against the target URL without
  manual setup the automation cannot perform.
- Use sequential IDs TC-001, TC-002, ...
- Prefer 6-20 high-value cases over exhaustive permutations. Order by priority."""


@agent_node("Test Architect", engine="claude", abort_on_error=True)
def test_architect(state: QAState) -> Dict[str, Any]:
    prompt = f"""# Ticket {state['ticket_id']}: {state.get('summary', '')}
Target URL: {state.get('target_url', '')}

## Triage analysis
```json
{json.dumps(state.get('triage_analysis', {}), indent=2)}
```

## Original description
{state.get('description', '') or '(empty)'}

## Diff (abridged)
```diff
{truncate_middle(state.get('github_diff', ''), SETTINGS.max_diff_chars // 3)}
```"""
    plan = call_claude_structured(
        state, "Test Architect",
        [SystemMessage(content=ARCHITECT_SYSTEM_PROMPT), HumanMessage(content=prompt)],
        TestPlan,
    )
    assert isinstance(plan, TestPlan)
    return {
        "test_plan": render_test_plan_markdown(plan),
        "test_cases": [case.model_dump() for case in plan.test_cases],
        "xray_csv_content": build_xray_csv(plan, state["ticket_id"]),
    }


# ----------------------------------------------------------------- Node 3 ----
AUTOMATION_SYSTEM_PROMPT = """You are a senior TypeScript SDET writing Playwright Test specs.
Produce ONE complete, compilable `.spec.ts` file for the automatable test cases you are given.

Hard requirements:
- TypeScript only. First line: `import { test, expect } from '@playwright/test';`
- Wrap everything in `test.describe('<TICKET_ID>: <summary>', () => { ... })`.
- One `test('[TC-XXX] <summary>', async ({ page }) => { ... })` per automatable test case; keep the TC id
  prefix in the title so results trace back to Xray.
- Resolve the base URL as: `const BASE_URL = process.env.BASE_URL ?? '<target url>';` and navigate with
  `page.goto(BASE_URL + '/path')` or `page.goto(BASE_URL)`.
- Use resilient, user-facing locators (getByRole, getByLabel, getByText, getByTestId) and web-first
  assertions (`await expect(locator).toBeVisible()`); never use `page.waitForTimeout` or fixed sleeps.
- Tests must be independent and idempotent; use `test.step()` to mirror the plan's steps.
- Non-functional checks must use only built-in Playwright APIs (e.g. `page.evaluate` on
  `performance.getEntriesByType('navigation')` for load budgets, keyboard navigation / ARIA checks for a11y).
- Never hard-code secrets. If credentials are required read them from `process.env` and call
  `test.skip(!process.env.X, 'reason')` when missing.
- Where the exact selector is unknown, choose the most likely accessible role/name from the diff and add a
  short `// ASSUMPTION:` comment.
Return ONLY the file content inside a single ```typescript fenced block."""


def _validate_playwright_spec(code: str) -> str:
    """Basic structural validation of the generated spec; auto-fixes a missing import."""
    if "@playwright/test" not in code:
        code = "import { test, expect } from '@playwright/test';\n\n" + code
    if not re.search(r"\btest\s*\(", code):
        raise LLMOutputError("Generated spec contains no `test(` blocks.")
    if "```" in code:
        raise LLMOutputError("Generated spec still contains markdown fences.")
    return code


@agent_node("TS Automation Engineer", engine="claude+local-cli", abort_on_error=True)
def automation_engineer(state: QAState) -> Dict[str, Any]:
    ticket_id, run_id = state["ticket_id"], state["run_id"]
    automatable = [tc for tc in state.get("test_cases", []) if tc.get("automatable")]
    if not automatable:
        return {
            "execution_passed": False,
            "execution_logs": "[error] The test plan contains no automatable test cases - nothing was executed.",
            "errors": ["TS Automation Engineer: no automatable test cases in plan."],
        }

    # ---- 3a. Claude generates the Playwright spec --------------------------
    prompt = f"""TICKET_ID: {ticket_id}
Summary: {state.get('summary', '')}
Target URL: {state.get('target_url', '')}

## Triage analysis
```json
{json.dumps(state.get('triage_analysis', {}), indent=2)}
```

## Automatable test cases
```json
{json.dumps(automatable, indent=2)}
```

## Diff (for selectors, routes and copy text)
```diff
{truncate_middle(state.get('github_diff', ''), SETTINGS.max_diff_chars // 2)}
```"""
    answer = call_claude_text(
        state, "TS Automation Engineer",
        [SystemMessage(content=AUTOMATION_SYSTEM_PROMPT), HumanMessage(content=prompt)],
    )
    code = _validate_playwright_spec(extract_code_block(answer))
    CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent="TS Automation Engineer",
                      message=f"Generated spec: {len(code.splitlines())} lines")

    # ---- 3b. Git + pnpm + Playwright (serialised on the shared clone) -------
    logs: List[str] = []
    errors: List[str] = []
    spec_path: Optional[Path] = None
    branch, pushed, passed = "", False, False

    CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent="TS Automation Engineer",
                      message="Waiting for test repository lock")
    with REPO_LOCK:
        try:
            TEST_REPO.ensure_ready(logs)
            spec_path, branch, pushed = TEST_REPO.publish_spec(ticket_id, code, logs)
            CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent="TS Automation Engineer",
                              message=f"Spec on branch {branch} (pushed={pushed}); running Playwright")
            passed = TEST_REPO.run_playwright(spec_path, state.get("target_url", ""), logs)
        except CommandError as exc:
            if exc.result is not None:
                logs.append(exc.result.as_log())
            logs.append(f"[error] {exc}")
            errors.append(f"TS Automation Engineer: {exc}")
            passed = False

    return {
        "generated_typescript_code": code,
        "spec_file_path": str(spec_path) if spec_path else "",
        "git_branch": branch,
        "git_pushed": pushed,
        "execution_logs": "\n\n".join(logs),
        "execution_passed": passed,
        "errors": errors,
    }


# ----------------------------------------------------------------- Node 4 ----
SYNTH_SYSTEM_PROMPT = """You are a QA report writer. Convert raw Playwright / pnpm / git terminal logs
into a concise Markdown report for a Jira comment.

Output these sections exactly:
### Results
A Markdown table: | Test | Status | Duration | Notes | - one row per test that appears in the logs.
### Failures
For each failed test: the test title, the key assertion/error line (in `code`), and the most likely cause.
Write "None." if there are no failures.
### Infrastructure issues
git / pnpm / browser / timeout problems found in the logs, or "None.".
### Recommendations
2-5 short, actionable bullet points.

Rules: only report what is in the logs - never invent tests, numbers or errors. No preamble."""


def _fallback_report_body(state: QAState) -> str:
    """Deterministic report used when the local LLM is unavailable."""
    logs = state.get("execution_logs", "")
    failure_lines = [
        line.strip() for line in logs.splitlines()
        if re.search(r"(✘|\berror\b|failed|timed out|\[error\])", line, re.IGNORECASE)
    ][:40]
    body = ["### Results", "_Local report synthesizer unavailable - showing extracted log lines._", ""]
    body += ["### Failures / errors", "```", *(failure_lines or ["(none detected)"]), "```"]
    body += ["", "### Log tail", "```", logs[-4000:], "```"]
    return "\n".join(body)


@agent_node("Report Synthesizer", engine="qwen-local", abort_on_error=False)
def report_synthesizer(state: QAState) -> Dict[str, Any]:
    ticket_id, run_id = state["ticket_id"], state["run_id"]
    passed = bool(state.get("execution_passed")) and not state.get("aborted")
    logs = state.get("execution_logs", "")
    counts = parse_playwright_counts(logs)
    errors = state.get("errors", [])

    # The authoritative header is built in Python so the local model can never flip a verdict.
    header = [
        f"## QA Report - {ticket_id}",
        f"**Status:** {'✅ QA Passed' if passed else '❌ QA Failed'}  ",
        f"**Summary:** {state.get('summary', '')}  ",
        f"**Target:** {state.get('target_url', '')}  ",
        f"**Run ID:** `{run_id}` · **Finished:** {utc_now()}  ",
    ]
    if state.get("git_branch"):
        header.append(f"**Spec branch:** `{state['git_branch']}` (pushed: {'yes' if state.get('git_pushed') else 'no'})  ")
    if counts:
        header.append("**Playwright:** " + ", ".join(f"{v} {k}" for k, v in counts.items()) + "  ")
    header.append(f"**Cloud tokens used:** {BUDGET.run_usage(run_id):,}")

    triage = state.get("triage_analysis") or {}
    if triage:
        header += ["", f"**Risk level:** {triage.get('risk_level', 'n/a')} - {triage.get('feature_summary', '')}"]
        if triage.get("gaps_and_questions"):
            header += ["", "**Open questions for the team:**"] + [f"- {q}" for q in triage["gaps_and_questions"]]
    if errors:
        header += ["", "### Pipeline errors"] + [f"- {e}" for e in errors]

    if not logs.strip():
        body = "_No tests were executed in this run._"
    else:
        try:
            response = qwen_llm().invoke([
                SystemMessage(content=SYNTH_SYSTEM_PROMPT),
                HumanMessage(content=f"Overall verdict (authoritative): {'PASSED' if passed else 'FAILED'}\n\n"
                                     f"```\n{truncate_middle(logs, SETTINGS.max_exec_log_chars_for_llm)}\n```"),
            ])
            body = message_text(response).strip() or _fallback_report_body(state)
        except Exception as exc:  # noqa: BLE001 - Ollama down/timeout must not fail the run
            log.warning("[%s] Local LLM unavailable (%s) - using fallback report.", ticket_id, exc)
            CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent="Report Synthesizer",
                              message=f"Ollama unavailable, fallback report used: {type(exc).__name__}")
            body = _fallback_report_body(state)

    return {"final_report": "\n".join(header) + "\n\n" + body, "execution_passed": passed}


# ----------------------------------------------------------------- Node 5 ----
@agent_node("Closeout", engine="http", abort_on_error=False)
def closeout(state: QAState) -> Dict[str, Any]:
    """POST the final verdict to n8n, which updates Jira and imports the CSV into Xray."""
    ticket_id = state["ticket_id"]
    payload = {
        "ticket_id": ticket_id,
        "status": "QA Passed" if state.get("execution_passed") else "QA Failed",
        "report_markdown": state.get("final_report", ""),
        "xray_csv_content": state.get("xray_csv_content", ""),
        # Extra context - n8n can ignore it, but it helps idempotency and traceability.
        "metadata": {
            "run_id": state["run_id"],
            "git_branch": state.get("git_branch", ""),
            "git_pushed": state.get("git_pushed", False),
            "cloud_tokens_used": BUDGET.run_usage(state["run_id"]),
            "aborted": bool(state.get("aborted")),
            "errors": state.get("errors", []),
        },
    }
    url = state.get("n8n_callback_url", "")
    if not url:
        return {"callback_delivered": False, "errors": ["Closeout: no n8n_callback_url provided."]}

    try:
        response = _retrying_session().post(url, json=payload, timeout=SETTINGS.callback_timeout_s)
    except requests.RequestException as exc:
        return {"callback_delivered": False, "errors": [f"Closeout: n8n callback failed: {redact(str(exc))}"]}
    if response.status_code >= 400:
        return {
            "callback_delivered": False,
            "errors": [f"Closeout: n8n callback returned HTTP {response.status_code}: {response.text[:300]}"],
        }
    log.info("[%s] n8n callback delivered (%s): %s", ticket_id, response.status_code, payload["status"])
    return {"callback_delivered": True}


# =============================================================================
# 11. GRAPH COMPILATION
# =============================================================================


def _continue_or_report(next_node: str) -> Callable[[QAState], str]:
    """Route to `next_node` unless the run aborted, in which case jump to the report."""

    def router(state: QAState) -> str:
        return "report_synthesizer" if state.get("aborted") else next_node

    return router


def build_graph():
    graph = StateGraph(QAState)
    graph.add_node("intake_triager", intake_triager)
    graph.add_node("test_architect", test_architect)
    graph.add_node("automation_engineer", automation_engineer)
    graph.add_node("report_synthesizer", report_synthesizer)
    graph.add_node("closeout", closeout)

    graph.add_edge(START, "intake_triager")
    graph.add_conditional_edges(
        "intake_triager", _continue_or_report("test_architect"),
        {"test_architect": "test_architect", "report_synthesizer": "report_synthesizer"},
    )
    graph.add_conditional_edges(
        "test_architect", _continue_or_report("automation_engineer"),
        {"automation_engineer": "automation_engineer", "report_synthesizer": "report_synthesizer"},
    )
    graph.add_edge("automation_engineer", "report_synthesizer")
    graph.add_edge("report_synthesizer", "closeout")
    graph.add_edge("closeout", END)
    return graph.compile()


QA_GRAPH = build_graph()


# =============================================================================
# 12. RUN REGISTRY (in-memory) + BACKGROUND RUNNER
# =============================================================================


class RunRegistry:
    """Tracks run status for GET /qa/runs/{id} and prevents duplicate runs per ticket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, Dict[str, Any]] = {}

    def try_start(self, run_id: str, ticket_id: str) -> Optional[str]:
        """Register a run; returns the conflicting run_id if the ticket is already running."""
        with self._lock:
            for existing_id, run in self._runs.items():
                if run["ticket_id"] == ticket_id and run["status"] in {"queued", "running"}:
                    return existing_id
            self._runs[run_id] = {"run_id": run_id, "ticket_id": ticket_id, "status": "queued", "created_at": utc_now()}
            return None

    def update(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            self._runs.setdefault(run_id, {}).update(fields)

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run else None


RUNS = RunRegistry()


def execute_qa_run(initial_state: QAState) -> None:
    """Background task: run the full LangGraph pipeline for one ticket."""
    run_id, ticket_id = initial_state["run_id"], initial_state["ticket_id"]
    RUNS.update(run_id, status="running", started_at=utc_now())
    CONTROL_ROOM.emit(run_id, ticket_id, "run.started", summary=initial_state.get("summary", ""))
    try:
        final: QAState = QA_GRAPH.invoke(initial_state, config={"recursion_limit": 25})
        outcome = "QA Passed" if final.get("execution_passed") else "QA Failed"
        RUNS.update(
            run_id,
            status="completed",
            outcome=outcome,
            finished_at=utc_now(),
            git_branch=final.get("git_branch", ""),
            callback_delivered=final.get("callback_delivered", False),
            cloud_tokens_used=BUDGET.run_usage(run_id),
            errors=final.get("errors", []),
            final_report=final.get("final_report", ""),
        )
        CONTROL_ROOM.emit(run_id, ticket_id, "run.completed", outcome=outcome,
                          cloud_tokens_used=BUDGET.run_usage(run_id),
                          callback_delivered=final.get("callback_delivered", False))
    except Exception as exc:  # noqa: BLE001 - last-resort guard; nodes already catch their own errors
        log.exception("[%s] Run %s crashed", ticket_id, run_id)
        RUNS.update(run_id, status="crashed", finished_at=utc_now(), errors=[redact(str(exc))])
        CONTROL_ROOM.emit(run_id, ticket_id, "run.crashed", error=redact(str(exc)))
    finally:
        BUDGET.release(run_id)


# =============================================================================
# 13. FASTAPI APPLICATION
# =============================================================================

_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class QARunRequest(BaseModel):
    """Payload n8n sends after catching the Jira 'Ready for QA' webhook."""

    ticket_id: str = Field(..., examples=["SHOP-1234"], description="Jira issue key.")
    summary: str = Field(..., min_length=1, max_length=1000)
    description: str = Field(default="", description="Jira description incl. acceptance criteria.")
    github_diff: str = Field(default="", description="Unified diff of the source PR.")
    target_url: HttpUrl = Field(..., description="Base URL of the environment under test.")
    n8n_callback_url: HttpUrl = Field(..., description="n8n webhook that receives the final verdict.")
    azure_logs: Optional[str] = Field(default=None, description="Recent Azure App Service / App Insights logs.")
    db_logs: Optional[str] = Field(default=None, description="Recent database logs / slow-query output.")

    @field_validator("ticket_id")
    @classmethod
    def _safe_ticket_id(cls, value: str) -> str:
        # The ticket id becomes a git branch name and a file name: reject anything unsafe.
        value = value.strip()
        if not _TICKET_ID_RE.match(value) or ".." in value or value.endswith((".lock", ".")):
            raise ValueError("ticket_id must look like a Jira key (letters, digits, '-', '_', '.').")
        return value


class QARunAccepted(BaseModel):
    run_id: str
    ticket_id: str
    status: str
    status_url: str


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Shared-secret auth for n8n → service calls (enabled when QA_SERVICE_API_KEY is set)."""
    expected = SETTINGS.qa_service_api_key
    if expected and not (x_api_key and hmac.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key.")


app = FastAPI(
    title="Autonomous QA Multi-Agent Framework",
    version="1.0.0",
    description="Jira → LangGraph agents (Claude + local Qwen) → Playwright → n8n / Xray.",
)


@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness + configuration sanity check (never reveals secrets)."""
    repo_path = TEST_REPO.path
    return {
        "status": "ok",
        "time": utc_now(),
        "claude_model": SETTINGS.claude_model,
        "claude_configured": bool(SETTINGS.anthropic_api_key),
        "ollama": {"base_url": SETTINGS.ollama_base_url, "model": SETTINGS.ollama_model},
        "test_repo": {
            "path": str(repo_path) if repo_path else None,
            "is_git_clone": bool(repo_path and (repo_path / ".git").is_dir()),
            "push_enabled": SETTINGS.git_push_enabled,
            "github_token_configured": bool(SETTINGS.github_token),
        },
        "tools": {name: bool(shutil.which(name)) for name in ("git", "pnpm")},
        "paperclip_configured": bool(SETTINGS.paperclip_webhook_url),
        "budget": {"per_run": SETTINGS.max_cloud_tokens_per_run, "per_day": SETTINGS.max_cloud_tokens_per_day},
    }


@app.post(
    "/qa/runs",
    response_model=QARunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def start_qa_run(request: QARunRequest, background_tasks: BackgroundTasks) -> QARunAccepted:
    """
    Accept a ticket and run the pipeline asynchronously. Playwright runs can take many
    minutes, so n8n gets an immediate 202 and the verdict arrives via n8n_callback_url.
    """
    run_id = uuid.uuid4().hex
    conflict = RUNS.try_start(run_id, request.ticket_id)
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A QA run for {request.ticket_id} is already in progress (run_id={conflict}).",
        )

    env_logs = "\n\n".join(
        f"### {label}\n{content}" for label, content in (("Azure logs", request.azure_logs), ("DB logs", request.db_logs))
        if content
    )
    initial_state: QAState = {
        "run_id": run_id,
        "ticket_id": request.ticket_id,
        "summary": request.summary,
        "description": request.description,
        "github_diff": request.github_diff,
        "target_url": str(request.target_url).rstrip("/"),
        "n8n_callback_url": str(request.n8n_callback_url),
        "environment_logs": env_logs,
        "test_plan": "",
        "generated_typescript_code": "",
        "execution_logs": "",
        "execution_passed": False,
        "xray_csv_content": "",
        "final_report": "",
        "aborted": False,
        "errors": [],
    }
    # Sync function → FastAPI runs it in its threadpool after the response is sent.
    background_tasks.add_task(execute_qa_run, initial_state)
    CONTROL_ROOM.emit(run_id, request.ticket_id, "run.queued", summary=request.summary)
    return QARunAccepted(run_id=run_id, ticket_id=request.ticket_id, status="queued", status_url=f"/qa/runs/{run_id}")


@app.get("/qa/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_qa_run(run_id: str) -> Dict[str, Any]:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown run_id.")
    return run


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=os.getenv("HOST", "127.0.0.1"), port=_env_int("PORT", 8000), reload=False)
