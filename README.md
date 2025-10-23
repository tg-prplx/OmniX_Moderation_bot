# Spisdil Moderation Bot

Modular [aiogram](https://docs.aiogram.dev) bot for intelligent moderation. The system is built around an asynchronous three-layer pipeline (regex → omni-moderation → GPT) and batch-oriented message ingestion.

## Architecture

- **MessageBatcher** aggregates incoming `MessageEnvelope`s, flushes by size or timeout, and works without blocking the event loop.
- **ModerationPipeline** is an ordered collection of layers with short-circuiting. Each layer is independent, supports warmup, and emits `ModerationVerdict`.
  - `RegexLayer` is CPU bound, runs inside a `ThreadPoolExecutor`, and caches compiled patterns.
  - `OmniModerationLayer` is I/O bound, calls `OpenAI /moderations`, and limits concurrency via semaphore.
  - `ChatGPTLayer` is the most expensive layer on `gpt-5-nano`, uses a deterministic JSON prompt for contextual analysis.
- **RuleRegistry & RuleService** manage active rules, use `RuleSynthesisClient` (gpt-5-mini) to classify and generate patterns, and stay in sync with storage.
- **Storage** provides a `StorageGateway` abstraction with a `SQLite` implementation that persists rules, incidents, and verdict history.
- **PunishmentAggregator** selects the final action using layer priority and violation severity.
- **ModerationScheduler** orchestrates batching, enforces resource limits, pauses layers during overload, persists results, and triggers punishment callbacks.
- **ModerationCoordinator** is the aiogram-facing facade. It wires the modules, handles lifecycle, and exposes administrative operations.

```
Telegram Updates → aiogram handler → ModerationCoordinator.ingest()
    → MessageBatcher → ModerationScheduler → ModerationPipeline (Regex → Omni → GPT)
    → PunishmentAggregator → Storage / decision callback → Telegram API
```

## Getting Started

1. Install dependencies:
   ```bash
   pip install -e .
   ```
2. Provide configuration via `.env` or environment variables:
   ```
   SPISDIL_TELEGRAM_TOKEN=123:abc
   SPISDIL_OPENAI__API_KEY=sk-proj-…
   SPISDIL_BATCH__MAX_BATCH_SIZE=50
   SPISDIL_STORAGE__SQLITE_PATH=moderation.db
   ```
3. Use the built-in aiogram integration (recommended):
   ```python
   import asyncio
   from spisdil_moder_bot import TelegramModerationApp
   from spisdil_moder_bot.config import BotSettings

   async def main():
       settings = BotSettings()  # loads env / .env automatically
       app = TelegramModerationApp(settings)
       await app.run()

   if __name__ == "__main__":
       asyncio.run(main())
   ```
4. The bot exposes chat commands for administrators:
  - `/addrule <action[:duration]> <scope:chat|global> <description>` – classify via gpt-5-mini and save (e.g. `mute:10m` or `ban:7d`).
  - `/removerule <rule_id>` – delete a rule.
  - `/listrules [chat|global]` – render the active rules affecting the chat.
  - В личке используй `/panel`, затем текстовые команды (`list`, `add [duration]`, `add-global [duration]`, `remove`) без спама в группе.
5. Or simply run:
   ```bash
   python run_bot.py
   ```
   This script loads `BotSettings`, starts the `TelegramModerationApp`, and shuts down gracefully.
6. **Important:** disable Bot Privacy Mode via BotFather (`/mybots → Bot → Bot Settings → Group Privacy → Turn off`). В противном случае Telegram не будет доставлять боту обычные сообщения в группах, и будут проходить только команды.

If you need lower-level control (e.g. embedding into another framework), you can still instantiate `ModerationCoordinator` directly and push `MessageEnvelope`s yourself.

## Punishment Durations

- Actions `mute` и `ban` принимают опциональный суффикс длительности (`30s`, `10m`, `2h`, `3d`).
- В DM можно писать `add mute 30m описание` или `add mute:30m описание`.
- Если длительность не указана, mute длится 15 минут, ban — бессрочный.

## Logging & Observability

`structlog` is configured globally and every stage publishes structured events:

- batching (`batcher_message_enqueued`, `batcher_flush`)
- pipeline flow (`pipeline_process_message_start`, `short_circuit`)
- layer activity (`regex_match`, `omni_flagged`, `chatgpt_violation`, `chatgpt_fallback_verdict`)
- OpenAI calls (`openai_request`, `openai_response`, image moderation logs)
- scheduler and telegram actions (`scheduler_decision`, `telegram_decision`)

Tune verbosity via `--log-cli-level` when running tests or by adjusting the Python logging level in your application entry point.

## Rule Scopes & Storage

- Rules now carry an optional `chat_id`. Global rules (`chat_id=None`) apply to every chat, while chat-scoped rules override/extend them.
- `RuleRegistry` merges global + chat-specific rules transparently during evaluation.
- SQLite schema persists `chat_id`, enabling full state recovery after restarts.
- The rule service exposes `add_rule`, `remove_rule`, and `list_rules` for programmatic access; aiogram command handlers build on top of these calls.

## Image & Media Moderation

- `MessageEnvelope` tracks base64-encoded photo payloads collected from Telegram.
- `OmniModerationLayer` now inspects both text and images, creating source-aware verdicts (`source=text|image`, `image_url` or `fallback_reason` are logged).
- `OmniModerationClient.classify_image()` calls the same OpenAI moderation endpoint with `input_image` payloads, so you can extend it to videos/documents by adapting the collector.
- Failures fall back to warnings and remain visible in logs instead of silently dropping content.

## Resilience and Parallelism

- Regex layer is isolated in a thread pool; the event loop remains responsive.
- Omni and GPT layers throttle concurrent API calls; API or parsing failures are logged without stopping the pipeline.
- `ModerationScheduler.pause_layer()` temporarily disables a layer (e.g., on HTTP 429) and `resume_layer()` brings it back.
- Graceful shutdown flushes pending batches before exiting.

## Storage and Audit

- `SQLiteStorage` provisions tables for rules and incidents. Metadata is serialized as JSON.
- Swap in a PostgreSQL (or other) backend by implementing the `StorageGateway` interface.
- Structured logging via `structlog` produces JSON events for traceability.

## Extensibility

- Add new moderation layers by subclassing `ModerationLayer` and wiring them into the pipeline.
- Fetch rules from external systems by providing custom `StorageGateway` implementations.
- Integrate alternative AI providers by supplying adapters with the same interface as the existing OpenAI clients.

## Testing

- Components are dependency-injected, which makes mocking external services straightforward.
- Unit suites cover batching, per-layer behaviour (including images and fallback decisions), rule registry scoping, scheduler flow, and fallback safety.
- Integration suite (`tests/integration/test_live_openai.py`) can be switched on with `RUN_LIVE_TESTS=1` to call real OpenAI APIs and exercise the full regex → omni → GPT stack plus rule synthesis.
