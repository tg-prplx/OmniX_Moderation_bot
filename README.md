<img width="1280" height="720" alt="image" src="https://github.com/user-attachments/assets/851bd601-4a04-414f-a9e5-8e18bb50c606" />
# Spisdil Moderation Bot

Enterprise-ready Telegram moderation platform with layered AI enforcement, auditable rule management, and operator-friendly tooling.

---

## Why Spisdil?

- **Layered AI pipeline** that combines deterministic regex, OpenAI Omni moderation, and contextual GPT policies for high-precision enforcement.
- **Live rule synthesis** with GPT-assisted classification, scoped per chat or globally, plus admin UX for rapid governance.
- **Enterprise observability** via structured logging, incident persistence, and decision audits powered by SQLite (pluggable to other stores).
- **Secure by design**: minimal permissions, configurable timeouts, JSON-only model prompts, and guardrails for unknown actions.
- **Extensible architecture** for plugging in new moderation layers, storage backends, or messaging surfaces.

---

## System Architecture

```text
┌─────────────── Telegram Updates ────────────────┐
│                   (aiogram)                     │
└───────────────┬─────────────────────────────────┘
                │
        ModerationCoordinator.ingest()
                │
         ┌──────▼───────────┐
         │  MessageBatcher  │◄──── timers / size flush
         └──────▼───────────┘
         ┌──────▼───────────┐
         │ ModerationScheduler │── metrics / pause & resume
         └──────▼───────────┘
    ┌───────────── ModerationPipeline ─────────────┐
    │ 1. RegexLayer (ThreadPoolExecutor)           │
    │ 2. OmniModerationLayer (OpenAI /moderations) │
    │ 3. ChatGPTLayer (gpt-5-nano)                 │
    └───────────────▲───────────────▲──────────────┘
                    │               │
                    │               └─ Structured verdicts (with incident storage)
                    └─ Short circuit on decisive actions

                 PunishmentAggregator
                         │
               Telegram Decision Callback
```

Key services:

| Component | Responsibility |
|-----------|----------------|
| **MessageBatcher** | De-bounces incoming traffic by size/timeout without blocking the event loop. |
| **ModerationPipeline** | Orchestrates independent layers with deterministic JSON verdicts. |
| **RuleRegistry / RuleService** | Keeps an in-memory cache of rules per layer/chat, synchronised with storage and GPT-powered classification. |
| **StorageGateway (SQLite)** | Persists rules, incidents, and metadata for auditability. |
| **Telegram Moderation App** | aiogram integration that wires ingestion, decisions, and admin experience. |

---

## Moderation Layers

- **Regex Layer** — Deterministic detection using compiled patterns. Runs inside a thread pool, supports Unicode categories (`regex` package).
- **Omni Layer** — Calls OpenAI `omni-moderation-latest` for both text and images, with semaphore-controlled concurrency.
- **ChatGPT Layer** — GPT-5 contextual reasoning with strict JSON responses, enriched with active rule descriptions and attached media.

Every `ModerationVerdict` carries:

```python
ModerationVerdict(
    layer=LayerType.CHATGPT,
    rule_code="aa40099c-...",
    priority=ViolationPriority.SPAM,
    action=ActionType.DELETE,
    reason="Сообщение содержит рекламу продажи телефонов",
    details={"raw": {...}, "gpt_severity": "medium"}
)
```

The first non-`ActionType.NONE` verdict short-circuits the pipeline to keep latency low.

---

## Rule Management & Admin UX

### Group Commands

- `/addrule <action[:duration]> <scope:chat|global> [flags] <description>`
- `/removerule <rule_id>`
- `/listrules [chat|global]`

### Direct Messages (`/panel`)

- Inline keyboard to pick chats (including global scope).
- Status-aware prompts for `add`, `add-global`, `remove`, `list`, `help`, `cancel`.
- Arguments like `warn:10m реклама` or `mute 30m спам` are normalised automatically.

Rules are scoped per chat (`chat_id`), or global (`None`). The registry merges global and chat-specific rules transparently during evaluation.

---

## Deployment Quick Start

### Prerequisites

- Python ≥ 3.11
- OpenAI API key with access to `omni-moderation-latest`, `gpt-5-nano`, `gpt-5-mini`
- Telegram bot token (privacy mode **disabled** for group moderation)

### Install

```bash
git clone https://github.com/your-org/spisdil-moder-bot.git
cd spisdil-moder-bot
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Configure

Create `.env` (or provide env vars directly):

```dotenv
SPISDIL_TELEGRAM_TOKEN=123456:ABCDEF
SPISDIL_OPENAI__API_KEY=sk-proj-...
SPISDIL_OPENAI__BASE_URL=https://api.openai.com/v1
SPISDIL_BATCH__MAX_BATCH_SIZE=50
SPISDIL_BATCH__MAX_DELAY_SECONDS=0.5
SPISDIL_LAYERS__REGEX_WORKERS=6
SPISDIL_LAYERS__OMNI_CONCURRENCY=8
SPISDIL_LAYERS__CHATGPT_CONCURRENCY=2
SPISDIL_STORAGE__SQLITE_PATH=moderation.db
SPISDIL_LOGGING__LEVEL=INFO
SPISDIL_LOGGING__USE_JSON=false
```

### Run

```bash
python run_bot.py
```

The runner loads `BotSettings`, starts the aiogram polling loop, and handles graceful shutdown (flushes batches, closes OpenAI clients, releases the bot session).

---

## Configuration Reference

| Setting | Env Variable | Default | Notes |
|---------|--------------|---------|-------|
| Telegram token | `SPISDIL_TELEGRAM_TOKEN` | — | Required for bot authentication. |
| OpenAI API key | `SPISDIL_OPENAI__API_KEY` | — | Required; supports project-scoped keys. |
| OpenAI base URL | `SPISDIL_OPENAI__BASE_URL` | `https://api.openai.com/v1` | Override for proxies. |
| OpenAI timeout | `SPISDIL_OPENAI__TIMEOUT_SECONDS` | `15` | Seconds per request. |
| Regex workers | `SPISDIL_LAYERS__REGEX_WORKERS` | `6` | Thread pool size for regex layer. |
| Omni concurrency | `SPISDIL_LAYERS__OMNI_CONCURRENCY` | `8` | Parallel moderation API calls. |
| ChatGPT concurrency | `SPISDIL_LAYERS__CHATGPT_CONCURRENCY` | `2` | Parallel GPT requests. |
| Batch size | `SPISDIL_BATCH__MAX_BATCH_SIZE` | `50` | Max envelopes per flush. |
| Batch delay | `SPISDIL_BATCH__MAX_DELAY_SECONDS` | `0.5` | Flush timeout in seconds. |
| Scheduler batches | `SPISDIL_SCHEDULER__CONCURRENT_BATCHES` | `4` | Pipeline back-pressure control. |
| SQLite path | `SPISDIL_STORAGE__SQLITE_PATH` | `moderation.db` | Replace when using other storage. |
| Logging level | `SPISDIL_LOGGING__LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR. |
| JSON logging | `SPISDIL_LOGGING__USE_JSON` | `false` | Set `true` for collectors (ELK, Loki, etc.). |

---

## Operations & Observability

- **Structured logs** via `structlog` (`openai_request`, `chatgpt_violation`, `scheduler_decision`, `telegram_decision`, etc.). Forward them to SIEM for audit trails.
- **Incidents table** (`moderation_incidents`) records every violation with rule ID, layer, action, and metadata.
- **Back-pressure**: the scheduler can pause layers (e.g., after repeated 429/5xx) without losing messages.
- **Health checks**: wrap `run_bot.py` with a supervisor (systemd, PM2) and monitor log heartbeats (`aiogram.dispatcher Start polling`, `moderation_coordinator_started`).

Backups: snapshot `moderation.db*` files or switch to PostgreSQL by implementing `StorageGateway` and pointing the coordinator to it.

---

## Security & Compliance

- **Principle of least privilege**: the bot only requires message read/delete permissions in Telegram groups; no admin operations beyond moderation actions.
- **OpenAI data**: requests send minimal context (chat metadata + text/images). No conversation history is retained; responses are deterministic JSON.
- **PII handling**: user identifiers stay within structured logs/storage for auditability. Apply retention policies by rotating SQLite or plugging in managed databases.
- **Rule governance**: all rule CRUD operations are logged (`rule_add_requested`, `rule_registry_added`, `rule_removed`). Administrators must use DM panel or commands, providing traceability.
- **Config secrets**: store `.env` securely (e.g., GitHub Actions secrets or Vault). `BotSettings` reads from env only—no hard-coded credentials.

For responsible disclosure, see [`SECURITY.md`](SECURITY.md).

---

## Testing & Quality

- Unit tests cover batching, layer behaviour, rule registry scoping, scheduler flow, and Telegram decision hooks.
- Integration test (`tests/integration/test_live_openai.py`) can call real OpenAI endpoints when `RUN_LIVE_TESTS=1` is set.
- Use `python -m compileall` (already part of CI) to catch syntax errors across modules.
- Recommended CI tasks:
  ```bash
  ruff check .
  mypy spisdil_moder_bot
  pytest
  ```

---

## Extending the Platform

- **New layers**: subclass `ModerationLayer`, register it in `ModerationPipeline`, and add configuration knobs under `BotSettings.layers`.
- **Storage backends**: implement `StorageGateway` for PostgreSQL, DynamoDB, etc., and update dependency wiring in `ModerationCoordinator`.
- **Additional platforms**: reuse the pipeline with other messaging adapters (e.g., Slack, Discord) by implementing ingestion and decision callbacks.
- **Custom punishments**: extend `PunishmentAggregator` or plug in alternate decision engines (e.g., escalation to human moderators).

---

## Project Roadmap

- ✅ Multi-layer moderation with text + image support.
- ✅ GPT-driven rule synthesis & admin DM experience.
- 🔄 Planned: Web dashboard for rule analytics, pluggable metrics exporters, fine-tuned model support, and RBAC for multi-operator teams.

Contributions welcome—see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Support & Contact

- File issues or pull requests on GitHub.
- For security concerns, follow the steps in [`SECURITY.md`](SECURITY.md).
- Enterprise inquiries: open a discussion or reach out via the contact details you include in your GitHub organisation.

Happy moderating! 🛡️
