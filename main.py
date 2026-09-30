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

Hybrid LLM routing (by task difficulty - see AGENT_ROUTES)
---------------------------------------------------------
* Intake Triager      → Claude Sonnet 5.5, effort medium (extraction + risk classification)
* Test Architect      → Claude Sonnet 5.5, effort high   (risk-based test design)
* TS Automation Eng.  → Claude Opus 5.5,   effort high   (hardest: runnable Playwright code
                        from a diff, plus a self-repair loop driven by Playwright's loader)
* Report Synthesizer  → local Ollama (Qwen 2.5-Coder) via the OpenAI-compatible endpoint
  at http://localhost:11434/v1 using `langchain-openai`. If Ollama is down, a
  deterministic Python fallback report is produced instead.
Structured output uses Claude's native `output_config.format` (forced tool use is not
supported on Opus 5.5). Every model, effort and output cap is overridable per agent.

Prompt caching
--------------
The Automation Engineer's large context (diff + plan + triage) carries a cache
breakpoint, so each repair turn reads it at 5% of the Opus input price instead of
paying for it again. Caches are per model, so they are enabled where a prefix is
actually re-sent (PROMPT_CACHE_AGENTS); a single-shot call would only pay the
1.25x cache-write premium.

Cost control & Control Room (Paperclip)
---------------------------------------
Every Claude call passes through `TokenBudget`, which refuses the call when the
per-run or per-day token cap is reached, or when Paperclip has paused the QA agent
(e.g. its monthly budget hard-stop). Each run is mirrored into Paperclip as an issue
with live comments, and every LLM call is booked on the agent's cost ledger.

Xray
----
The Test Architect's plan is returned both as an Xray CSV (manual fallback) and as
an Xray Cloud bulk-import JSON payload, which n8n imports automatically.
"""

from __future__ import annotations

import asyncio
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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Type, TypedDict
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
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
    # Per-agent routing (see AGENT_ROUTES): model id, effort and output cap per task.
    claude_triage_model: str = field(default_factory=lambda: _env_str("CLAUDE_TRIAGE_MODEL", "claude-sonnet-5-5"))
    claude_triage_effort: str = field(default_factory=lambda: _env_str("CLAUDE_TRIAGE_EFFORT", "medium"))
    claude_triage_max_tokens: int = field(default_factory=lambda: _env_int("CLAUDE_TRIAGE_MAX_TOKENS", 16_000))
    claude_architect_model: str = field(default_factory=lambda: _env_str("CLAUDE_ARCHITECT_MODEL", "claude-sonnet-5-5"))
    claude_architect_effort: str = field(default_factory=lambda: _env_str("CLAUDE_ARCHITECT_EFFORT", "high"))
    claude_architect_max_tokens: int = field(default_factory=lambda: _env_int("CLAUDE_ARCHITECT_MAX_TOKENS", 32_000))
    claude_automation_model: str = field(default_factory=lambda: _env_str("CLAUDE_AUTOMATION_MODEL", "claude-opus-5-5"))
    claude_automation_effort: str = field(default_factory=lambda: _env_str("CLAUDE_AUTOMATION_EFFORT", "high"))
    claude_automation_max_tokens: int = field(default_factory=lambda: _env_int("CLAUDE_AUTOMATION_MAX_TOKENS", 64_000))
    claude_timeout_s: int = field(default_factory=lambda: _env_int("CLAUDE_TIMEOUT_SECONDS", 600))
    # Optional JSON overriding/adding per-model USD-per-MTok prices (see MODEL_PRICING).
    claude_pricing_json: str = field(default_factory=lambda: _env_str("CLAUDE_PRICING_JSON"))

    # --- Prompt caching -----------------------------------------------------
    # Agents whose per-run context is marked cacheable. Only worth it where the same
    # prefix is re-sent (the Automation Engineer's spec-repair loop); a single-shot
    # call pays the cache-write premium with nothing to read it back.
    prompt_cache_agents: str = field(default_factory=lambda: _env_str("PROMPT_CACHE_AGENTS", "automation"))
    prompt_cache_ttl: str = field(default_factory=lambda: _env_str("PROMPT_CACHE_TTL", "5m"))  # "5m" | "1h"
    # How many times Opus may fix a spec that Playwright cannot load (0 disables the loop).
    spec_repair_attempts: int = field(default_factory=lambda: _env_int("SPEC_REPAIR_ATTEMPTS", 2))

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
    paperclip_api_url: str = field(default_factory=lambda: _env_str("PAPERCLIP_API_URL").rstrip("/"))
    paperclip_api_key: str = field(default_factory=lambda: _env_str("PAPERCLIP_API_KEY"))
    paperclip_project: str = field(default_factory=lambda: _env_str("PAPERCLIP_PROJECT"))  # name or UUID
    paperclip_enforce_budget: bool = field(default_factory=lambda: _env_bool("PAPERCLIP_ENFORCE_BUDGET", True))

    # --- Xray (JSON bulk import payload returned to n8n) --------------------
    xray_project_key: str = field(default_factory=lambda: _env_str("XRAY_PROJECT_KEY"))  # default: ticket prefix
    xray_test_folder: str = field(default_factory=lambda: _env_str("XRAY_TEST_FOLDER", "AI QA/{ticket_id}"))
    xray_requirement_link_type: str = field(default_factory=lambda: _env_str("XRAY_REQUIREMENT_LINK_TYPE", "Test"))
    xray_include_priority: bool = field(default_factory=lambda: _env_bool("XRAY_INCLUDE_PRIORITY", False))

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
            parts.append(block.get("text") or "")
    return "".join(parts)


@dataclass(frozen=True)
class TokenUsage:
    """
    Provider-reported token usage for one LLM response, with the prompt-cache split.

    `input_tokens` is the TOTAL prompt size (LangChain adds cache reads and writes back
    in); the cache fields break it down so each part can be priced at its own rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_read_tokens
                   - self.cache_write_5m_tokens - self.cache_write_1h_tokens)

    @property
    def budget_tokens(self) -> int:
        """Tokens counted against the local caps. Cache reads bill at 5-10% and are excluded."""
        return self.input_tokens - self.cache_read_tokens + self.output_tokens


def token_usage(message: Any) -> TokenUsage:
    """Extract LangChain's normalised `usage_metadata` from an AIMessage (zeros if absent)."""
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    write_5m = int(details.get("ephemeral_5m_input_tokens") or 0)
    write_1h = int(details.get("ephemeral_1h_input_tokens") or 0)
    generic_write = int(details.get("cache_creation") or 0)  # older SDKs: no TTL split
    if generic_write and not (write_5m or write_1h):
        if SETTINGS.prompt_cache_ttl == "1h":
            write_1h = generic_write
        else:
            write_5m = generic_write
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(details.get("cache_read") or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


# USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-09-30). Override or add models with CLAUDE_PRICING_JSON, e.g.
# {"claude-opus-5-5": {"input": 4, "output": 20, "cache_write_5m": 5, "cache_write_1h": 8, "cache_read": 0.2}}
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "claude-fable-5-1":  {"input": 10.0, "output": 50.0, "cache_write_5m": 12.50, "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-opus-5-5":   {"input": 4.0,  "output": 20.0, "cache_write_5m": 5.00,  "cache_write_1h": 8.0,  "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0,  "output": 10.0, "cache_write_5m": 2.50,  "cache_write_1h": 4.0,  "cache_read": 0.20},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_write_5m": 1.25,  "cache_write_1h": 2.0,  "cache_read": 0.10},
}
if SETTINGS.claude_pricing_json:
    try:
        MODEL_PRICING.update(json.loads(SETTINGS.claude_pricing_json))
    except (ValueError, TypeError) as _exc:
        logging.getLogger("qa.config").warning("Ignoring invalid CLAUDE_PRICING_JSON: %s", _exc)


def price_cents(model: str, usage: TokenUsage) -> float:
    """Exact (fractional) cost of one call in US cents, pricing each cache tier separately."""
    prices = MODEL_PRICING.get(model) or MODEL_PRICING.get(re.sub(r"-\d{8}$", "", model))
    if prices is None:
        # Unknown model: price it like the most expensive known model rather than as free.
        prices = max(MODEL_PRICING.values(), key=lambda p: p["output"])
        logging.getLogger("qa.budget").warning("No price for model %r - using the highest known rate.", model)
    usd = (
        usage.uncached_input_tokens * prices["input"]
        + usage.cache_write_5m_tokens * prices["cache_write_5m"]
        + usage.cache_write_1h_tokens * prices["cache_write_1h"]
        + usage.cache_read_tokens * prices["cache_read"]
        + usage.output_tokens * prices["output"]
    ) / 1_000_000
    return usd * 100


def paperclip_timestamp() -> str:
    """ISO-8601 UTC with a 'Z' suffix - Paperclip's zod `.datetime()` rejects '+00:00' offsets."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
# 4. PAPERCLIP CONTROL ROOM (REST client) + TOKEN BUDGET (financial guard)
# =============================================================================

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

# Triage risk level → Paperclip issue priority (Paperclip: critical | high | medium | low).
_RISK_TO_PAPERCLIP_PRIORITY = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}


class PaperclipError(RuntimeError):
    """A Paperclip API call failed (network error or HTTP >= 400)."""


class PaperclipClient:
    """
    Minimal synchronous client for Paperclip's REST API (`<base>/api/...`).

    Authenticates as the QA agent with an agent API key (`Authorization: Bearer`).
    An agent key may read its own record (`/agents/me`), create and update issues in
    its company, comment on them, and report its *own* cost events - exactly the
    surface this service needs.
    """

    def __init__(self, base_url: str, api_key: str, timeout_s: int = 10) -> None:
        self.base_url = base_url
        self.enabled = bool(base_url and api_key)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._timeout_s = timeout_s
        self._local = threading.local()  # requests.Session is not thread-safe: one per thread

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = _retrying_session(total_retries=2)
        return session

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                timeout_s: Optional[int] = None) -> Any:
        url = f"{self.base_url}/api{path}"
        try:
            response = self._session().request(
                method, url, json=body, headers=self._headers, timeout=timeout_s or self._timeout_s
            )
        except requests.RequestException as exc:
            raise PaperclipError(f"{method} {path}: {redact(str(exc))}") from exc
        if response.status_code >= 400:
            raise PaperclipError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None


class ControlRoom:
    """
    Mirrors every QA run into the Paperclip Control Room:

        run.started       → issue created: "[TICKET] summary", in_progress, assigned to the QA agent
        triage.completed  → issue priority set from the triage risk level
        agent.* / log     → issue comments (the live execution feed on the board)
        llm.usage         → cost event (tokens + cost_cents) on the agent's ledger
        run.completed     → issue moved to done (QA Passed) or blocked (QA Failed) + final report
        run.crashed       → issue moved to blocked + error

    Delivery happens on ONE background worker, so events stay in order and a slow or
    offline Paperclip can never block or fail a QA run. The only synchronous call is
    `budget_block_reason()`, because the budget must be checked *before* money is spent.
    Every event is also written to the service log, with or without Paperclip.
    """

    _COMMENT_EVENTS = {"agent.completed", "agent.failed", "agent.skipped", "agent.log"}
    _AGENT_CACHE_TTL_S = 30.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = PaperclipClient(settings.paperclip_api_url, settings.paperclip_api_key)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paperclip")
        self._log = logging.getLogger("qa.paperclip")
        self._lock = threading.Lock()
        self._identity: Optional[Dict[str, str]] = None      # {"agent_id", "company_id", "name"}
        self._project_id: Optional[str] = None
        self._issues: Dict[str, str] = {}                     # run_id → Paperclip issue id
        self._agent_cache: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
        self._budget_check_skip_until = 0.0  # back-off after Paperclip was unreachable

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    # ------------------------------------------------------------ identity --
    def _get_me(self, fresh: bool = False) -> Dict[str, Any]:
        """GET /agents/me (cached briefly): status, pauseReason, budget and runtimeConfig."""
        fetched_at, agent = self._agent_cache
        if not fresh and agent is not None and time.monotonic() - fetched_at < self._AGENT_CACHE_TTL_S:
            return agent
        agent = self.client.request("GET", "/agents/me", timeout_s=5) or {}
        self._agent_cache = (time.monotonic(), agent)
        return agent

    def _ensure_identity(self) -> Dict[str, str]:
        """Discover the agent id and company id from the API key (no IDs to configure)."""
        with self._lock:
            if self._identity:
                return self._identity
        me = self._get_me(fresh=True)
        identity = {
            "agent_id": str(me.get("id") or ""),
            "company_id": str(me.get("companyId") or ""),
            "name": str(me.get("name") or ""),
        }
        if not identity["agent_id"] or not identity["company_id"]:
            raise PaperclipError("/agents/me returned no id/companyId - is PAPERCLIP_API_KEY an *agent* API key?")
        project_id = self._resolve_project(identity["company_id"])
        with self._lock:
            self._identity, self._project_id = identity, project_id
        return identity

    def _resolve_project(self, company_id: str) -> Optional[str]:
        """PAPERCLIP_PROJECT may be a project UUID or its display name."""
        wanted = self.settings.paperclip_project.strip()
        if not wanted:
            return None
        if _UUID_RE.match(wanted):
            return wanted
        projects = self.client.request("GET", f"/companies/{company_id}/projects") or []
        if isinstance(projects, dict):  # tolerate paginated / wrapped responses
            projects = projects.get("items") or projects.get("projects") or projects.get("data") or []
        for project in projects:
            if isinstance(project, dict) and str(project.get("name", "")).strip().lower() == wanted.lower():
                return project.get("id")
        self._log.warning("Paperclip project %r not found - issues will be created without a project.", wanted)
        return None

    # -------------------------------------------------------------- events --
    def emit(self, run_id: str, ticket_id: str, event: str, **data: Any) -> None:
        loggable = {k: v for k, v in data.items() if k != "final_report"}
        self._log.info("[%s] %s %s", ticket_id, event, json.dumps(loggable, default=str)[:500])
        if self.enabled:
            self._pool.submit(self._deliver, run_id, ticket_id, event, data)

    def _deliver(self, run_id: str, ticket_id: str, event: str, data: Dict[str, Any]) -> None:
        try:
            self._handle(run_id, ticket_id, event, data)
        except PaperclipError as exc:
            self._log.warning("[%s] Paperclip %s not recorded: %s", ticket_id, event, exc)
        except Exception:  # noqa: BLE001 - the Control Room must never affect a QA run
            self._log.exception("[%s] Unexpected error delivering %s to Paperclip", ticket_id, event)

    def _handle(self, run_id: str, ticket_id: str, event: str, data: Dict[str, Any]) -> None:
        identity = self._ensure_identity()
        company_id = identity["company_id"]

        if event == "run.started":
            body: Dict[str, Any] = {
                "title": f"[{ticket_id}] {data.get('summary', '')}".strip()[:250],
                "description": (
                    f"Autonomous QA run for Jira **{ticket_id}**.\n\n"
                    f"- Target: {data.get('target_url', '')}\n"
                    f"- Run ID: `{run_id}`\n"
                    f"- Started: {utc_now()}"
                ),
                "status": "in_progress",          # Paperclip requires an assignee for in_progress
                "priority": "medium",             # refined after triage
                "assigneeAgentId": identity["agent_id"],
                "idempotencyKey": run_id,         # safe to retry: replays return the same issue
                "allowDuplicate": True,           # re-runs of a ticket reuse the same title
            }
            if self._project_id:
                body["projectId"] = self._project_id
            issue = self.client.request("POST", f"/companies/{company_id}/issues", body) or {}
            if issue.get("id"):
                with self._lock:
                    self._issues[run_id] = issue["id"]
            return

        with self._lock:
            issue_id = self._issues.get(run_id)

        if event == "llm.usage":
            self._post_cost_event(identity, issue_id, data)
            return
        if issue_id is None:  # issue creation failed or Paperclip was offline at run start
            return

        if event == "triage.completed":
            priority = _RISK_TO_PAPERCLIP_PRIORITY.get(str(data.get("risk_level", "")).lower())
            if priority:
                self.client.request("PATCH", f"/issues/{issue_id}", {"priority": priority})
        elif event in self._COMMENT_EVENTS:
            self.client.request("POST", f"/issues/{issue_id}/comments", {"body": self._comment_text(event, data)})
        elif event in {"run.completed", "run.crashed"}:
            passed = event == "run.completed" and data.get("outcome") == "QA Passed"
            comment = data.get("final_report") or f"**Run crashed:** {data.get('error', 'unknown error')}"
            self.client.request(
                "PATCH", f"/issues/{issue_id}",
                {"status": "done" if passed else "blocked", "comment": comment},
            )
            with self._lock:
                self._issues.pop(run_id, None)

    @staticmethod
    def _comment_text(event: str, data: Dict[str, Any]) -> str:
        agent = data.get("agent", "Agent")
        if event == "agent.completed":
            return (f"✅ **{agent}** finished in {data.get('duration_s', '?')}s "
                    f"({data.get('engine', '')}; cloud tokens so far: {data.get('cloud_tokens_run_total', 0):,})")
        if event == "agent.failed":
            return f"❌ **{agent}** failed: {data.get('error', 'unknown error')}"
        if event == "agent.skipped":
            return f"⏭️ **{agent}** skipped: {data.get('reason', '')}"
        return f"**{agent}:** {data.get('message', '')}".strip() or "(empty log line)"

    def _post_cost_event(self, identity: Dict[str, str], issue_id: Optional[str], data: Dict[str, Any]) -> None:
        body: Dict[str, Any] = {
            "agentId": identity["agent_id"],       # agents may only report their own costs
            "provider": data["provider"],
            "biller": data["provider"],
            "billingType": data.get("billing_type", "metered_api"),
            "model": data["model"],
            "inputTokens": int(data.get("input_tokens", 0)),
            "cachedInputTokens": int(data.get("cached_input_tokens", 0)),
            "outputTokens": int(data.get("output_tokens", 0)),
            "costCents": int(data.get("cost_cents", 0)),
            "occurredAt": data.get("occurred_at") or paperclip_timestamp(),
        }
        if issue_id:
            body["issueId"] = issue_id
        if self._project_id:
            body["projectId"] = self._project_id
        self.client.request("POST", f"/companies/{identity['company_id']}/cost-events", body)

    # -------------------------------------------------------------- budget --
    def budget_block_reason(self) -> Optional[str]:
        """
        Why Paperclip forbids further spending (agent paused, e.g. by its budget hard-stop,
        or monthly budget used up), or None. Fails OPEN when Paperclip is unreachable -
        the service's own per-run / per-day caps still apply in that case.
        """
        if not (self.enabled and self.settings.paperclip_enforce_budget):
            return None
        if time.monotonic() < self._budget_check_skip_until:
            return None
        try:
            me = self._get_me()
        except PaperclipError as exc:
            # Don't pay the connection-retry delay before every Claude call during an outage.
            self._budget_check_skip_until = time.monotonic() + self._AGENT_CACHE_TTL_S
            self._log.warning("Paperclip unreachable for budget check (%s) - local caps only for %.0fs.",
                              exc, self._AGENT_CACHE_TTL_S)
            return None
        status = str(me.get("status") or "")
        if status in {"paused", "terminated"}:
            return f"Paperclip agent is {status} (reason: {me.get('pauseReason') or 'not given'})."
        budget = int(me.get("budgetMonthlyCents") or 0)
        spent = int(me.get("spentMonthlyCents") or 0)
        if budget > 0 and spent >= budget:
            return f"Paperclip monthly budget used up (${spent / 100:,.2f} of ${budget / 100:,.2f})."
        return None

    # -------------------------------------------------------------- health --
    def health(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"configured": False}
        try:
            me = self._get_me(fresh=True)
            identity = self._ensure_identity()
        except PaperclipError as exc:
            return {"configured": True, "reachable": False, "error": str(exc)}

        warnings: List[str] = []
        runtime_config = me.get("runtimeConfig")
        heartbeat: Dict[str, Any] = {}
        if isinstance(runtime_config, dict) and isinstance(runtime_config.get("heartbeat"), dict):
            heartbeat = runtime_config["heartbeat"]
        # Mirrors Paperclip's isHeartbeatWakeOnDemandEnabled(): first key set wins, default true.
        wake_value: Any = next(
            (heartbeat[k] for k in ("wakeOnDemand", "wakeOnAssignment", "wakeOnOnDemand", "wakeOnAutomation")
             if heartbeat.get(k) is not None),
            True,
        )
        wake_on_demand: Any = "unknown" if runtime_config is None else str(wake_value).lower() not in {"false", "0", "no"}
        heartbeat_enabled: Any = "unknown" if runtime_config is None else str(heartbeat.get("enabled", False)).lower() in {"true", "1", "yes"}
        if wake_on_demand is True:
            warnings.append("Turn OFF 'Wake on demand' for this agent - otherwise Paperclip tries to run it "
                            "every time a QA issue is assigned to it.")
        if heartbeat_enabled is True:
            warnings.append("Turn OFF the heartbeat timer for this agent - n8n starts runs, not Paperclip.")
        if self.settings.paperclip_project and not self._project_id:
            warnings.append(f"PAPERCLIP_PROJECT {self.settings.paperclip_project!r} was not found.")
        return {
            "configured": True,
            "reachable": True,
            "agent": identity["name"],
            "agent_status": me.get("status"),
            "company_id": identity["company_id"],
            "project_id": self._project_id,
            "wake_on_demand": wake_on_demand,
            "heartbeat_enabled": heartbeat_enabled,
            "budget_monthly_cents": me.get("budgetMonthlyCents"),
            "spent_monthly_cents": me.get("spentMonthlyCents"),
            "enforce_budget": self.settings.paperclip_enforce_budget,
            "warnings": warnings,
        }


class BudgetExceededError(RuntimeError):
    """Raised before a cloud LLM call when a token cap or the Paperclip budget is exhausted."""


class TokenBudget:
    """
    Thread-safe cloud spend accountant.

    * `ensure_available` runs BEFORE every Claude call. It refuses the call once the
      per-run or per-day token cap is reached, or once Paperclip has paused the agent
      or its monthly budget is used up - this is what stops runaway spending loops.
    * `record` runs AFTER every call with the provider-reported usage, prices it and
      reports it to Paperclip's cost ledger.
    A single call can overshoot a cap by at most `CLAUDE_MAX_TOKENS` output tokens
    plus its prompt, so size the caps with that headroom in mind.
    """

    def __init__(self, settings: Settings, control_room: ControlRoom) -> None:
        self._per_run_cap = settings.max_cloud_tokens_per_run
        self._per_day_cap = settings.max_cloud_tokens_per_day
        self._control_room = control_room
        self._lock = threading.Lock()
        self._run_usage: Dict[str, int] = {}
        self._cent_carry: Dict[str, float] = {}  # sub-cent remainders, so small calls aren't lost
        self._run_cost_cents: Dict[str, float] = {}
        self._run_cache_read: Dict[str, int] = {}
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
        reason = self._control_room.budget_block_reason()  # outside the lock: it may do HTTP
        if reason:
            raise BudgetExceededError(reason)

    def record(self, run_id: str, ticket_id: str, agent: str, usage: TokenUsage, model: str) -> None:
        exact_cents = price_cents(model, usage)
        with self._lock:
            self._roll_day()
            self._run_usage[run_id] = self._run_usage.get(run_id, 0) + usage.budget_tokens
            self._day_usage += usage.budget_tokens
            self._run_cost_cents[run_id] = self._run_cost_cents.get(run_id, 0.0) + exact_cents
            self._run_cache_read[run_id] = self._run_cache_read.get(run_id, 0) + usage.cache_read_tokens
            carried = self._cent_carry.get(run_id, 0.0) + exact_cents
            cost_cents = int(carried)
            self._cent_carry[run_id] = carried - cost_cents
            run_total, day_total = self._run_usage[run_id], self._day_usage
        self._control_room.emit(
            run_id, ticket_id, "llm.usage",
            agent=agent, provider="anthropic", model=model, billing_type="metered_api",
            # Paperclip's ledger: inputTokens = billed-at-full-or-write-rate, cached = cache reads.
            input_tokens=usage.input_tokens - usage.cache_read_tokens,
            cached_input_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
            output_tokens=usage.output_tokens, cost_cents=cost_cents,
            run_total_tokens=run_total, run_cap=self._per_run_cap,
            day_total_tokens=day_total, day_cap=self._per_day_cap,
        )

    def run_cost_usd(self, run_id: str) -> float:
        with self._lock:
            return self._run_cost_cents.get(run_id, 0.0) / 100

    def run_cache_read_tokens(self, run_id: str) -> int:
        with self._lock:
            return self._run_cache_read.get(run_id, 0)

    def run_usage(self, run_id: str) -> int:
        with self._lock:
            return self._run_usage.get(run_id, 0)

    def release(self, run_id: str) -> int:
        """Forget a finished run's counters (the daily total is kept) and return its final usage."""
        with self._lock:
            self._cent_carry.pop(run_id, None)
            self._run_cost_cents.pop(run_id, None)
            self._run_cache_read.pop(run_id, None)
            return self._run_usage.pop(run_id, 0)


CONTROL_ROOM = ControlRoom(SETTINGS)
BUDGET = TokenBudget(SETTINGS, CONTROL_ROOM)


# =============================================================================
# 5. LLM CLIENTS (hybrid routing)
# =============================================================================


class LLMOutputError(RuntimeError):
    """Raised when an LLM answer is refused, truncated, or cannot be parsed."""


@dataclass(frozen=True)
class AgentRoute:
    """Which Claude model an agent runs on, how hard it thinks, and whether it caches."""

    key: str
    label: str
    model: str
    effort: str          # output_config.effort: low | medium | high | xhigh | max
    max_tokens: int      # includes adaptive-thinking tokens, so leave headroom
    cache_context: bool  # mark the per-run context block with cache_control


def _cache_enabled(key: str) -> bool:
    agents = {a.strip().lower() for a in SETTINGS.prompt_cache_agents.split(",")}
    return "all" in agents or key in agents


# Routing by difficulty. Opus 5.5 gets the hardest job: writing Playwright code that
# must run against an application it has only seen through a diff, and repairing it
# from loader errors. Triage (extraction + risk classification) and test design are
# reasoning over text, where Sonnet 5.5 is strong at half the price.
AGENT_ROUTES: Dict[str, AgentRoute] = {
    "triage": AgentRoute("triage", "Intake Triager", SETTINGS.claude_triage_model,
                         SETTINGS.claude_triage_effort, SETTINGS.claude_triage_max_tokens, _cache_enabled("triage")),
    "architect": AgentRoute("architect", "Test Architect", SETTINGS.claude_architect_model,
                            SETTINGS.claude_architect_effort, SETTINGS.claude_architect_max_tokens,
                            _cache_enabled("architect")),
    "automation": AgentRoute("automation", "TS Automation Engineer", SETTINGS.claude_automation_model,
                             SETTINGS.claude_automation_effort, SETTINGS.claude_automation_max_tokens,
                             _cache_enabled("automation")),
}


_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
if SETTINGS.prompt_cache_ttl not in {"5m", "1h"}:
    logging.getLogger("qa.config").warning("PROMPT_CACHE_TTL=%r is not '5m' or '1h' - using 5m.",
                                          SETTINGS.prompt_cache_ttl)
for _route in AGENT_ROUTES.values():
    if _route.effort not in _VALID_EFFORTS:
        logging.getLogger("qa.config").warning(
            "CLAUDE_%s_EFFORT=%r is not one of %s - the API will reject it.",
            _route.key.upper(), _route.effort, sorted(_VALID_EFFORTS))
    if _route.model not in MODEL_PRICING:
        logging.getLogger("qa.config").warning(
            "No price known for %s (%s) - add it via CLAUDE_PRICING_JSON for accurate cost tracking.",
            _route.model, _route.label)


@functools.lru_cache(maxsize=None)
def claude_llm(route_key: str) -> ChatAnthropic:
    """One client per agent route. Cloud 'analytical brain' used by agents 1-3."""
    if not SETTINGS.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")
    route = AGENT_ROUTES[route_key]
    # No temperature/top_p: Claude 5.x models think adaptively (always on for Opus 5.5)
    # and depth is steered with `effort` instead.
    return ChatAnthropic(
        model=route.model,
        api_key=SETTINGS.anthropic_api_key,
        max_tokens=route.max_tokens,
        reasoning_effort=route.effort,
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


def cache_control() -> Dict[str, str]:
    """Explicit prompt-cache breakpoint. 5m writes cost 1.25x input, 1h writes 2x; reads 0.05-0.1x."""
    control: Dict[str, str] = {"type": "ephemeral"}
    if SETTINGS.prompt_cache_ttl == "1h":
        control["ttl"] = "1h"
    return control


def context_message(route_key: str, text: str) -> HumanMessage:
    """
    The large per-run context (ticket, diff, plan) as the first user turn. For routes
    with caching enabled it carries a cache breakpoint, so every later request that
    re-sends the same system prompt + context (repair turns, retries) reads it from
    cache instead of paying full input price. Caches are per model and need at least
    512 tokens on Opus/Sonnet 5.5 (4,096 on Haiku 4.5); shorter prompts just aren't cached.
    """
    block: Dict[str, Any] = {"type": "text", "text": text}
    if AGENT_ROUTES[route_key].cache_context:
        block["cache_control"] = cache_control()
    return HumanMessage(content=[block])


def _check_stop_reason(route: AgentRoute, message: Any) -> None:
    stop_reason = (getattr(message, "response_metadata", None) or {}).get("stop_reason")
    if stop_reason == "refusal":
        raise LLMOutputError(f"{route.label}: {route.model} declined the request (stop_reason=refusal).")
    if stop_reason == "max_tokens":
        raise LLMOutputError(
            f"{route.label}: output hit max_tokens={route.max_tokens} (thinking included) - "
            f"raise CLAUDE_{route.key.upper()}_MAX_TOKENS or lower the effort."
        )


def invoke_claude(state: "QAState", route_key: str, messages: List[BaseMessage]) -> AIMessage:
    """Budget-guarded free-text Claude call; returns the AIMessage so it can be replayed."""
    route = AGENT_ROUTES[route_key]
    BUDGET.ensure_available(state["run_id"])
    response = claude_llm(route_key).invoke(messages)
    BUDGET.record(state["run_id"], state["ticket_id"], route.label, token_usage(response), route.model)
    _check_stop_reason(route, response)
    return response


def call_claude_structured(
    state: "QAState", route_key: str, messages: List[BaseMessage], schema: Type[BaseModel]
) -> BaseModel:
    """
    Budget-guarded Claude call returning a validated Pydantic object via Claude's native
    structured outputs (`output_config.format`). Forced tool use - LangChain's default
    method - is rejected by Opus 5.5 and not forced on Sonnet 5.5, so it isn't used.
    `include_raw=True` keeps the raw AIMessage so usage is still billed.
    """
    route = AGENT_ROUTES[route_key]
    BUDGET.ensure_available(state["run_id"])
    runnable = claude_llm(route_key).with_structured_output(schema, method="json_schema", include_raw=True)
    result = runnable.invoke(messages)
    raw = result.get("raw")
    BUDGET.record(state["run_id"], state["ticket_id"], route.label, token_usage(raw), route.model)
    _check_stop_reason(route, raw)
    parsed = result.get("parsed")
    if parsed is None:
        raise LLMOutputError(f"{route.label}: could not parse structured output: {result.get('parsing_error')}")
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
    xray_tests_json: List[Dict[str, Any]]  # Xray Cloud bulk-import payload (automatic import by n8n)
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
    # Resolve the executable through PATH/PATHEXT so Windows shims such as
    # `pnpm.cmd` are found without resorting to shell=True.
    resolved = shutil.which(args[0], path=merged_env.get("PATH"))
    if resolved:
        args = [resolved, *args[1:]]
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
            "GCM_INTERACTIVE": "never",  # Git Credential Manager (Windows/macOS): no login pop-ups
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

    def prepare_branch(self, ticket_id: str, logs: List[str]) -> str:
        """
        Sync the base branch and create (or reset) `qa/{ticket_id}` from it.

        Remote problems (fetch/pull) are logged and tolerated so offline runs still work;
        a failed checkout raises CommandError because the tree would be in an unknown state.
        """
        assert self.path is not None
        base = self.settings.test_repo_base_branch
        branch = f"qa/{ticket_id}"

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

        # `-B` is the idempotent form of `checkout -b`: re-runs of a ticket reset its branch.
        result = self.git("checkout", "-B", branch, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError(f"git checkout -B {branch} failed", result)
        return branch

    def write_spec(self, ticket_id: str, code: str, logs: List[str]) -> Path:
        """Write the spec file (confined to the repository) with LF line endings."""
        assert self.path is not None
        spec_dir = (self.path / self.settings.test_spec_dir).resolve()
        if self.path != spec_dir and self.path not in spec_dir.parents:
            raise CommandError(f"TEST_SPEC_DIR escapes the repository: {self.settings.test_spec_dir}")
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / f"{ticket_id}.spec.ts"
        spec_path.write_text(code, encoding="utf-8", newline="\n")  # LF on every OS
        logs.append(f"[info] wrote {spec_path.relative_to(self.path).as_posix()} ({len(code):,} bytes)")
        return spec_path

    def commit_and_push(self, ticket_id: str, spec_path: Path, branch: str, logs: List[str]) -> bool:
        """
        Commit the spec (skipped when identical to HEAD) and push the branch.
        Returns True if pushed. Commit failures raise; push failures are tolerated.
        """
        assert self.path is not None
        rel_spec = spec_path.relative_to(self.path).as_posix()
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

        # A rejected push of our own qa/* branch is retried with --force-with-lease,
        # which only overwrites what we last fetched.
        if not self.settings.git_push_enabled:
            logs.append("[info] GIT_PUSH_ENABLED=false - skipping git push.")
            return False
        result = self.git("push", "-u", "origin", branch, check=False)
        logs.append(result.as_log())
        if not result.ok and ("rejected" in result.stderr or "non-fast-forward" in result.stderr):
            result = self.git("push", "--force-with-lease", "-u", "origin", branch, check=False)
            logs.append(result.as_log())
        if not result.ok:
            logs.append("[warn] git push failed - spec is committed locally only. Check GITHUB_TOKEN scopes.")
        return result.ok

    # ----------------------------------------------------------- pnpm / PW --
    def _pw_env(self, target_url: str) -> Dict[str, str]:
        return {
            "CI": "1",               # Playwright: no interactive HTML report server, retries per config
            "FORCE_COLOR": "0",
            "NO_COLOR": "1",
            "BASE_URL": target_url,  # consumed by the generated spec and playwright.config.ts
            "PLAYWRIGHT_BASE_URL": target_url,
        }

    def install_dependencies(self, target_url: str, logs: List[str]) -> bool:
        """`pnpm install` (+ optional browser install). Returns False if tests cannot run."""
        assert self.path is not None
        if shutil.which("pnpm") is None:
            logs.append("[error] pnpm is not installed or not on PATH (try: corepack enable pnpm).")
            return False
        env = self._pw_env(target_url)
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
        return True

    def list_tests(self, spec_path: Path, target_url: str) -> CommandResult:
        """
        `playwright test <spec> --list`: loads and transpiles the spec and collects its
        tests without launching a browser. Syntax errors, bad imports and a spec with
        no tests all fail here in seconds - the signal for the repair loop.
        """
        assert self.path is not None
        rel_spec = spec_path.relative_to(self.path).as_posix()
        return run_command(
            ["pnpm", "exec", "playwright", "test", rel_spec, "--list"],
            cwd=self.path, timeout_s=120, env=self._pw_env(target_url),
        )

    def run_playwright(self, spec_path: Path, target_url: str, logs: List[str]) -> bool:
        """`pnpm exec playwright test <spec>`. Returns True only if all tests pass."""
        assert self.path is not None
        rel_spec = spec_path.relative_to(self.path).as_posix()
        extra = self.settings.playwright_extra_args.split() if self.settings.playwright_extra_args else []
        test = run_command(
            ["pnpm", "exec", "playwright", "test", rel_spec, *extra],
            cwd=self.path, timeout_s=self.settings.playwright_timeout_s, env=self._pw_env(target_url),
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


def xray_project_key(ticket_id: str) -> str:
    """Jira project the Xray tests are created in: XRAY_PROJECT_KEY, else the ticket's prefix."""
    return SETTINGS.xray_project_key or ticket_id.rsplit("-", 1)[0]


def build_xray_tests_json(plan: TestPlan, ticket_id: str) -> List[Dict[str, Any]]:
    """
    Build the body for Xray Cloud's bulk test import
    (POST https://xray.cloud.getxray.app/api/v2/import/test/bulk).

    One Manual test per test case, with its steps, filed in a per-ticket folder of the
    Xray test repository and linked to the Jira story with the "Test" link type - so the
    story's Xray coverage panel lights up automatically.
    """
    folder = SETTINGS.xray_test_folder.replace("{ticket_id}", ticket_id).strip("/")
    tests: List[Dict[str, Any]] = []
    for index, case in enumerate(plan.test_cases, start=1):
        test_id = case.test_id.strip() or f"TC-{index:03d}"
        description = [f"Generated by the autonomous QA framework for {ticket_id} ({test_id}).",
                       f"Type: {case.kind} / {case.category}. Automated: {'yes' if case.automatable else 'no'}."]
        if case.preconditions:
            description.append(f"Preconditions: {case.preconditions}")
        if case.acceptance_criteria_refs:
            description.append("Covers: " + "; ".join(case.acceptance_criteria_refs))
        fields: Dict[str, Any] = {
            "summary": f"[{ticket_id}] {test_id} {case.summary}"[:250],
            "project": {"key": xray_project_key(ticket_id)},
            "description": "\n".join(description),
            "labels": sorted({_label(l) for l in ("ai-generated", case.kind, case.category, ticket_id) if l.strip()}),
        }
        if SETTINGS.xray_include_priority:  # off by default: priority names differ between Jira sites
            fields["priority"] = {"name": case.priority}
        test: Dict[str, Any] = {
            "testtype": "Manual",
            "fields": fields,
            "steps": [
                {"action": step.action, "data": step.data, "result": step.expected_result}
                for step in case.steps
            ],
            "update": {
                "issuelinks": [{
                    "add": {
                        "type": {"name": SETTINGS.xray_requirement_link_type},
                        "outwardIssue": {"key": ticket_id},
                    }
                }]
            },
        }
        if folder:
            test["xray_test_repository_folder"] = folder
        tests.append(test)
    return tests


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
        state, "triage",
        [SystemMessage(content=TRIAGE_SYSTEM_PROMPT), context_message("triage", prompt)],
        TriageAnalysis,
    )
    CONTROL_ROOM.emit(state["run_id"], state["ticket_id"], "triage.completed",
                      risk_level=analysis.risk_level, criteria=len(analysis.acceptance_criteria))
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
        state, "architect",
        [SystemMessage(content=ARCHITECT_SYSTEM_PROMPT), context_message("architect", prompt)],
        TestPlan,
    )
    assert isinstance(plan, TestPlan)
    return {
        "test_plan": render_test_plan_markdown(plan),
        "test_cases": [case.model_dump() for case in plan.test_cases],
        "xray_csv_content": build_xray_csv(plan, state["ticket_id"]),
        "xray_tests_json": build_xray_tests_json(plan, state["ticket_id"]),
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


SPEC_REPAIR_PROMPT = """Playwright could not load the spec you wrote. This is the output of
`playwright test {spec} --list`, which only loads and collects the tests (no browser was started):

```
{error}
```

Fix the problem and return the COMPLETE corrected file in a single ```typescript block.
Change only what is needed to make the spec load; keep the test ids, titles and intent."""


def _parse_spec(answer: AIMessage) -> Tuple[Optional[str], Optional[str]]:
    """Extract and structurally validate the spec: (code, None) or (None, problem)."""
    try:
        return _validate_playwright_spec(extract_code_block(message_text(answer))), None
    except LLMOutputError as exc:
        return None, str(exc)


@agent_node("TS Automation Engineer", engine="claude+local-cli", abort_on_error=True)
def automation_engineer(state: QAState) -> Dict[str, Any]:
    ticket_id, run_id = state["ticket_id"], state["run_id"]
    target_url = state.get("target_url", "")
    agent = AGENT_ROUTES["automation"].label
    automatable = [tc for tc in state.get("test_cases", []) if tc.get("automatable")]
    if not automatable:
        return {
            "execution_passed": False,
            "execution_logs": "[error] The test plan contains no automatable test cases - nothing was executed.",
            "errors": ["TS Automation Engineer: no automatable test cases in plan."],
        }

    # ---- 3a. Opus writes the Playwright spec -------------------------------
    # The context below is the large, stable part of every request in this node. It
    # carries a prompt-cache breakpoint, so each repair turn re-reads it from cache.
    prompt = f"""TICKET_ID: {ticket_id}
Summary: {state.get('summary', '')}
Target URL: {target_url}

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
    messages: List[BaseMessage] = [SystemMessage(content=AUTOMATION_SYSTEM_PROMPT),
                                   context_message("automation", prompt)]
    answer = invoke_claude(state, "automation", messages)

    # ---- 3b. Validate → repair → commit → run (serialised on the shared clone) --
    logs: List[str] = []
    errors: List[str] = []
    code: Optional[str] = None
    spec_path: Optional[Path] = None
    branch, pushed, loaded, passed = "", False, False, False
    max_repairs = max(0, min(SETTINGS.spec_repair_attempts, 3))  # 1 + 3 cache breakpoints max
    repairs = 0

    CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=agent, message="Waiting for test repository lock")
    with REPO_LOCK:
        try:
            TEST_REPO.ensure_ready(logs)
            branch = TEST_REPO.prepare_branch(ticket_id, logs)
            deps_ok = TEST_REPO.install_dependencies(target_url, logs)

            while True:
                candidate, problem = _parse_spec(answer)
                if candidate is not None:
                    code = candidate
                    spec_path = TEST_REPO.write_spec(ticket_id, code, logs)
                    if not deps_ok:
                        break  # cannot validate or run without node_modules
                    listing = TEST_REPO.list_tests(spec_path, target_url)
                    logs.append(listing.as_log())
                    if listing.ok:
                        loaded = True
                        break
                    problem = truncate_middle(f"{listing.stdout}\n{listing.stderr}".strip(), 6000)

                if repairs >= max_repairs:
                    break
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=agent,
                                  message=f"Spec did not load - repair attempt {repairs + 1}/{max_repairs}")
                rel_spec = f"{SETTINGS.test_spec_dir}/{ticket_id}.spec.ts"
                repair_block: Dict[str, Any] = {"type": "text",
                                                "text": SPEC_REPAIR_PROMPT.format(spec=rel_spec, error=problem)}
                if AGENT_ROUTES["automation"].cache_context:
                    repair_block["cache_control"] = cache_control()  # next repair reuses this turn too
                messages += [answer, HumanMessage(content=[repair_block])]
                try:
                    answer = invoke_claude(state, "automation", messages)
                except (BudgetExceededError, LLMOutputError) as exc:
                    errors.append(f"{agent}: repair stopped - {exc}")
                    break
                repairs += 1

            if spec_path is not None:
                pushed = TEST_REPO.commit_and_push(ticket_id, spec_path, branch, logs)
            if loaded:
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=agent,
                                  message=f"Spec loads (repairs: {repairs}); branch {branch} "
                                          f"(pushed={pushed}); running Playwright")
                passed = TEST_REPO.run_playwright(spec_path, target_url, logs)
            elif deps_ok:
                message = f"Spec still fails to load after {repairs} repair attempt(s) - tests not executed."
                logs.append(f"[error] {message}")
                errors.append(f"{agent}: {message}")
        except CommandError as exc:
            if exc.result is not None:
                logs.append(exc.result.as_log())
            logs.append(f"[error] {exc}")
            errors.append(f"{agent}: {exc}")
            passed = False

    return {
        "generated_typescript_code": code or "",
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
    cache_reads = BUDGET.run_cache_read_tokens(run_id)
    header.append(f"**Cloud cost:** ${BUDGET.run_cost_usd(run_id):,.4f} · {BUDGET.run_usage(run_id):,} tokens"
                  + (f" + {cache_reads:,} read from prompt cache" if cache_reads else "") + "  ")
    header.append("**Models:** " + ", ".join(f"{r.label} → `{r.model}` ({r.effort})" for r in AGENT_ROUTES.values())
                  + f", Report Synthesizer → `{SETTINGS.ollama_model}` (local)")

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
            local_usage = token_usage(response)
            CONTROL_ROOM.emit(run_id, ticket_id, "llm.usage", agent="Report Synthesizer",
                              provider="ollama", model=SETTINGS.ollama_model, billing_type="fixed",
                              input_tokens=local_usage.input_tokens, output_tokens=local_usage.output_tokens,
                              cost_cents=0)
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
        "xray_tests_json": state.get("xray_tests_json", []),
        # Extra context - n8n can ignore it, but it helps idempotency and traceability.
        "metadata": {
            "run_id": state["run_id"],
            "git_branch": state.get("git_branch", ""),
            "git_pushed": state.get("git_pushed", False),
            "cloud_tokens_used": BUDGET.run_usage(state["run_id"]),
            "cloud_cost_usd": round(BUDGET.run_cost_usd(state["run_id"]), 4),
            "cache_read_tokens": BUDGET.run_cache_read_tokens(state["run_id"]),
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
    CONTROL_ROOM.emit(run_id, ticket_id, "run.started", summary=initial_state.get("summary", ""),
                      target_url=initial_state.get("target_url", ""))
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
            cloud_cost_usd=round(BUDGET.run_cost_usd(run_id), 4),
            errors=final.get("errors", []),
            final_report=final.get("final_report", ""),
        )
        CONTROL_ROOM.emit(run_id, ticket_id, "run.completed", outcome=outcome,
                          cloud_tokens_used=BUDGET.run_usage(run_id),
                          callback_delivered=final.get("callback_delivered", False),
                          final_report=final.get("final_report", ""))
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: verify the Paperclip connection once and surface misconfiguration early."""
    if CONTROL_ROOM.enabled:
        status_ = await asyncio.to_thread(CONTROL_ROOM.health)
        if not status_.get("reachable"):
            log.warning("Paperclip not reachable at startup: %s", status_.get("error"))
        else:
            log.info("Paperclip connected as agent %r (company %s)", status_.get("agent"), status_.get("company_id"))
            for warning in status_.get("warnings", []):
                log.warning("Paperclip: %s", warning)
    else:
        log.info("Paperclip not configured (PAPERCLIP_API_URL / PAPERCLIP_API_KEY) - events go to the log only.")
    yield


app = FastAPI(
    lifespan=lifespan,
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
        "claude_routes": {
            key: {"model": r.model, "effort": r.effort, "max_tokens": r.max_tokens, "prompt_cache": r.cache_context}
            for key, r in AGENT_ROUTES.items()
        },
        "prompt_cache_ttl": SETTINGS.prompt_cache_ttl,
        "spec_repair_attempts": SETTINGS.spec_repair_attempts,
        "claude_configured": bool(SETTINGS.anthropic_api_key),
        "ollama": {"base_url": SETTINGS.ollama_base_url, "model": SETTINGS.ollama_model},
        "test_repo": {
            "path": str(repo_path) if repo_path else None,
            "is_git_clone": bool(repo_path and (repo_path / ".git").is_dir()),
            "push_enabled": SETTINGS.git_push_enabled,
            "github_token_configured": bool(SETTINGS.github_token),
        },
        "tools": {name: bool(shutil.which(name)) for name in ("git", "pnpm")},
        "paperclip": CONTROL_ROOM.health(),
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
        "xray_tests_json": [],
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
