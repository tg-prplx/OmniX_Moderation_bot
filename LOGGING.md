# Logging Configuration

## Colored Console Output

The bot uses colored, human-readable console logs by default with ANSI escape sequences.

**Example output:**
```
[10:47:20] INFO     omni_flagged | rule_id=4cc63011... | category=harassment | message_id=6236
[10:47:20] ERROR    chatgpt_invalid_json | error=Empty response | message_id=6238
```

## Enabling DEBUG Logs

To see detailed debugging information (including GPT request/response details), set the logging level to DEBUG:

### Option 1: Environment Variable
```bash
SPISDIL_LOGGING__LEVEL=DEBUG python run_bot.py
```

### Option 2: .env File
Add to your `.env` file:
```
SPISDIL_LOGGING__LEVEL=DEBUG
```

### Option 3: JSON Format
For machine-readable logs (useful for log aggregation):
```bash
SPISDIL_LOGGING__USE_JSON=true python run_bot.py
```

## Debug Events to Look For

When debugging GPT layer issues, look for these events:

- `chatgpt_request` - Request sent to GPT
- `chatgpt_response_received` - Response received with finish_reason, tokens, content_length
- `chatgpt_json_parsed` - JSON successfully parsed with violation and category
- `chatgpt_invalid_json` - JSON parsing failed with error details
- `chatgpt_response_truncated` - Response exceeded max_tokens limit

## Color Scheme

- 🟢 **INFO** - Green
- 🔵 **DEBUG** - Cyan
- 🟡 **WARNING** - Yellow
- 🔴 **ERROR** - Red
- 🟣 **CRITICAL** - Magenta

**Data colors:**
- Timestamps: Dark gray
- Event names: Bright cyan
- Keys: Blue
- Numbers: Bright yellow
- Strings: Bright green
- Separators: Dim gray

## Disabling Colors

Colors are automatically disabled if output is redirected to a file or pipe. You can also force JSON format:

```bash
SPISDIL_LOGGING__USE_JSON=true python run_bot.py > output.log
```

## Adjusting Library Log Levels

The bot automatically sets appropriate log levels for noisy libraries:
- `httpx` - WARNING (only shows errors)
- `aiogram` - INFO (shows telegram events)

To see all HTTP requests, edit `spisdil_moder_bot/logging/events.py` and change:
```python
logging.getLogger("httpx").setLevel(logging.DEBUG)
```
