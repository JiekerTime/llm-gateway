# llm-gateway

A lightweight LLM API gateway with multi-backend routing, automatic fallback, circuit breaking, token accounting, prompt logging, structured output, normalized streaming, and session-backed persona reuse.

## Features

- **Multi-backend support** — DeepSeek, Ollama, or any OpenAI-compatible API
- **Caller-based routing** — route different callers to different models via `config.yaml`
- **Automatic fallback** — if the primary backend is circuit-broken, try the next one
- **Circuit breaker** — per-backend failure tracking with configurable thresholds
- **Token accounting** — per-caller usage stats with cost estimation
- **Prompt logging** — every call logged to disk as JSON Lines
- **Structured output** — unified `response_format` for `text`, `json_object`, and `json_schema`
- **Streaming event gateway** — optional `stream=true` with normalized SSE events
- **Incremental JSON parsing** — best-effort `structured_partial` events during streaming
- **Session management** — optional SQLite-backed `session_id` with automatic history append/truncation
- **Role cards** — deterministic system-prompt assembly from reusable expert/persona dimensions
- **Concurrency control** — global semaphore to prevent upstream API rate limits

## Current Status

The current implementation already ships:

- unified sync + streaming `/chat`
- provider-normalized structured output and tool-call handling
- best-effort incremental JSON parsing over normalized stream events
- optional session-backed chat state with role-card driven expert personas
- `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}`
- `GET /role-cards`, `GET /role-cards/{name}`

## Near-Term Improvements

- real backend probes for `/health` and `/status`
- persisted historical queries for `/logs` and `/stats`
- first-token latency metrics for streaming calls
- broader contract coverage for retries, fallback, and backend normalization

## Quick Start

### Run locally

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY="sk-..."
uvicorn server:app --host 0.0.0.0 --port 8525
```

### Run with Docker

```bash
docker build -t llm-gateway .
docker run -d \
  --name llm-gateway \
  -p 8525:8525 \
  -v $(pwd)/logs:/app/logs \
  -e DEEPSEEK_API_KEY="sk-..." \
  llm-gateway
```

### Verify

```bash
# Health check (no auth required)
curl http://localhost:8525/health

# Chat call
curl -X POST http://localhost:8525/chat \
  -H "X-API-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}],"caller":"test/smoke"}'
```

---

## API Reference

### POST /chat

Main inference endpoint (OpenAI-compatible).

**Request:**
```json
{
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Summarize this article."}
  ],
  "model": "deepseek-v4-flash",
  "temperature": 0.7,
  "max_tokens": 800,
  "stream": false,
  "thinking": "disabled",
  "reasoning_effort": "high",
  "session_id": "sess_demo",
  "role_card": "gp-default-expert",
  "dimension_overrides": {
    "scenario": "You are advising on deployment design."
  },
  "response_format": {
    "type": "json_object"
  },
  "caller": "myapp/summarizer"
}
```

- **`model`** — optional, defaults to `default_model` in `config.yaml`
- **`caller`** — required, format `module/subtype`, used for routing and accounting
- **`stream`** — optional, when `true` the gateway returns `text/event-stream`
- **`thinking`** — optional DeepSeek V4 switch: `"enabled"` / `"disabled"`; the default config sends `"disabled"` to preserve old `deepseek-chat` behavior
- **`reasoning_effort`** — optional DeepSeek V4 thinking effort: `"high"` or `"max"`; compatibility values `"low"`, `"medium"`, and `"xhigh"` are accepted and mapped by DeepSeek
- **`session_id`** — optional, enables persistent history when present
- **`role_card`** — optional, injects a reusable persona; with `session_id` it also binds the session to that card
- **`dimension_overrides`** — optional per-request role-card overrides, e.g. `scenario`, `constraints`
- **`append_history`** — optional, defaults to `true`; when `false`, the turn is not written back into session history
- **`response_format`** — optional structured-output mode:
  - `{"type":"text"}` or omitted
  - `{"type":"json_object"}`
  - `{"type":"json_schema","json_schema":{"name":"...", "schema": {...}, "strict": true}}`
- Other fields are passed through to the backend LLM

**Response (200):**
```json
{
  "content": "The article discusses...",
  "model": "deepseek-v4-flash",
  "usage": {
    "prompt_tokens": 1234,
    "completion_tokens": 456,
    "total_tokens": 1690
  },
  "latency_ms": 1823,
  "response_format_type": "json_object",
  "session_id": "sess_demo",
  "role_card": "gp-default-expert",
  "caller": "myapp/summarizer",
  "backend": "deepseek"
}
```

### Session and Role Card Notes

- `role_card` without `session_id` is **stateless**: the card is compiled into a deterministic system prompt for that request only
- `session_id` enables SQLite-backed history reuse, so callers only need to send the new turn instead of full prior context
- new sessions must be created with a `role_card`, either explicitly via `POST /sessions` or implicitly on first `/chat` with `session_id + role_card`
- an existing session is bound to its original `role_card` and `caller`
- session history stores non-system messages only; the role-card system prompt is rebuilt each turn

### Structured Output Notes

- `openai_compat` backends currently support `text` and `json_object`
- `ollama` supports `text`, `json_object`, and `json_schema`
- If the routed backend chain cannot satisfy the requested `response_format`,
  the gateway returns HTTP 400 with `error="unsupported_response_format"`

### Streaming Notes

- `stream=true` returns Server-Sent Events with normalized gateway events
- Event types currently include:
  - `message_start`
  - `text_delta`
  - `tool_call_start`
  - `tool_args_delta`
  - `structured_partial`
  - `usage`
  - `done`
  - `error`
- Fallback is only attempted before the first streamed event is emitted
- Incremental JSON parsing is best-effort only: raw deltas always remain the
  source of truth if provider chunk formats change

**Streaming Example:**
```text
data: {"type":"message_start"}

data: {"type":"text_delta","delta":"{"}

data: {"type":"structured_partial","structured_target":"message","structured_value":{"title":null}}

data: {"type":"done","stop_reason":"stop","backend":"deepseek","model":"deepseek-v4-flash"}
```

**Error (502 — all backends failed):**
```json
{
  "error": "all_backends_failed",
  "detail": "deepseek: circuit open; ollama: timeout after 300s",
  "caller": "myapp/summarizer"
}
```

---

### GET /health

No authentication required.

```json
{"status": "ok"}
```

---

### GET /stats

Token usage statistics. Requires API Key.

Query params:
- `since` — ISO 8601 timestamp or duration like `24h`, `7d` (default: `24h`)

```json
{
  "period": "2026-04-09T13:00:00+00:00Z / 2026-04-10T13:00:00+00:00Z",
  "total": {
    "calls": 238,
    "prompt_tokens": 98234,
    "completion_tokens": 28901,
    "total_tokens": 127135,
    "cache_hit_tokens": 61440,
    "estimated_cost_usd": 0.0581
  },
  "by_caller": [
    {
      "caller": "myapp/summarizer",
      "calls": 84,
      "prompt_tokens": 45123,
      "completion_tokens": 12345,
      "total_tokens": 57468,
      "cache_hit_tokens": 32768
    }
  ]
}
```

---

### GET /logs

Recent call records. Requires API Key.

Query params:
- `limit` — default 20, max 100
- `caller` — filter by caller prefix
- `since` — ISO 8601 timestamp

```json
{
  "logs": [
    {
      "ts": "2026-04-10T13:55:01Z",
      "caller": "myapp/summarizer",
      "session_id": "sess_demo",
      "role_card": "gp-default-expert",
      "model": "deepseek-v4-flash",
      "backend": "deepseek",
      "stream": true,
      "response_format_type": "json_object",
      "prompt_tokens": 1234,
      "completion_tokens": 456,
      "cache_hit_tokens": 768,
      "queue_wait_ms": 37,
      "tool_call_count": 0,
      "latency_ms": 1823,
      "status": "ok"
    }
  ]
}
```

---

### GET /usage/sources

Source-level usage analysis from persisted per-call logs. Use this when call
volume spikes and you need to identify which app/caller caused it. Requires API
Key.

Query params:
- `since` — ISO 8601 timestamp or duration like `24h`, `7d` (default: `24h`)
- `caller` — optional caller prefix filter, e.g. `gp/`
- `limit` — default 20, max 100

```json
{
  "period": "2026-04-27T00:00:00+00:00Z / 2026-04-27T12:00:00+00:00Z",
  "total": {
    "calls": 238,
    "prompt_tokens": 98234,
    "completion_tokens": 28901,
    "total_tokens": 127135,
    "cache_hit_tokens": 61440
  },
  "by_service": [
    {
      "service": "group-portrait",
      "calls": 184,
      "prompt_tokens": 80123,
      "completion_tokens": 21000,
      "total_tokens": 101123,
      "cache_hit_tokens": 55000,
      "avg_latency_ms": 1823.5,
      "first_seen": "2026-04-27T00:10:00+00:00Z",
      "last_seen": "2026-04-27T11:58:00+00:00Z",
      "top_callers": [
        {"caller": "gp/weekly-health", "total_tokens": 62000}
      ]
    }
  ],
  "by_source": [
    {
      "source": "gp",
      "calls": 184,
      "prompt_tokens": 80123,
      "completion_tokens": 21000,
      "total_tokens": 101123,
      "cache_hit_tokens": 55000,
      "avg_latency_ms": 1823.5,
      "first_seen": "2026-04-27T00:10:00+00:00Z",
      "last_seen": "2026-04-27T11:58:00+00:00Z",
      "top_callers": [
        {"caller": "gp/weekly-health", "total_tokens": 62000}
      ]
    }
  ],
  "by_caller": [],
  "by_model": [],
  "by_backend": [],
  "by_role_card": [],
  "by_session": [],
  "recent_heavy_calls": []
}
```

### GET /usage

Built-in visual usage dashboard. Open it in a browser, enter the API key, then
filter by time window and caller prefix. The page calls `/usage/sources` and
shows source/caller/model/role-card rankings plus recent heavy calls.

---

### Session APIs

Requires API Key.

```json
POST /sessions
{
  "session_id": "sess_demo",
  "role_card": "gp-default-expert",
  "caller": "myapp/summarizer",
  "dimension_overrides": {
    "scenario": "You are advising on deployment design."
  }
}
```

- `GET /sessions?caller=myapp/`
- `GET /sessions/{session_id}`
- `DELETE /sessions/{session_id}`

### Role Card APIs

Requires API Key.

- `GET /role-cards`
- `GET /role-cards/{name}` returns the card plus a compiled `system_prompt_preview`

---

## Project Structure

```
llm-gateway/
├── server.py
├── llm_gateway/
│   ├── app.py
│   ├── config.py
│   ├── backends/
│   │   ├── base.py
│   │   ├── deepseek.py
│   │   └── ollama.py
│   ├── core/
│   │   ├── circuit_breaker.py
│   │   ├── incremental_json.py
│   │   ├── prompt_logger.py
│   │   ├── role_card_registry.py
│   │   ├── router.py
│   │   ├── session_manager.py
│   │   └── token_store.py
│   └── models/
│       ├── request.py
│       ├── response.py
│       ├── role_card.py
│       ├── session.py
│       └── stats.py
├── data/
│   └── role_cards/
├── tests/
│   ├── test_response_format.py
│   ├── test_role_cards.py
│   └── test_sessions.py
├── SESSION_DESIGN.md
├── config.yaml
├── requirements.txt
├── Dockerfile
├── deploy.sh
└── README.md
```

---

## Configuration

All configuration lives in `config.yaml`. Key sections:

```yaml
port: 8525
api_key: "change-me"           # X-API-Key header value

backends:
  deepseek:
    type: openai_compat        # uses openai SDK (AsyncOpenAI)
    base_url: "https://api.deepseek.com"
    api_key_env: "DEEPSEEK_API_KEY"
    default_model: "deepseek-v4-flash"
    default_thinking: "disabled"
    timeout: 120
    priority: 1                # 1 = highest (primary)

  ollama:
    type: ollama
    base_url: "http://localhost:11434"
    default_model: "qwen2.5:7b"
    timeout: 300
    priority: 2                # fallback

routing_rules:
  - caller_pattern: "*"
    backend: "deepseek"
    model: "deepseek-v4-flash"

fallback_chain:
  - deepseek
  - ollama

circuit_breaker:
  failure_threshold: 5
  recovery_timeout_s: 60

concurrency:
  max_concurrent: 20

sessions:
  enabled: true
  db_path: "data_runtime/sessions.db"
  default_ttl_hours: 24
  cleanup_interval_s: 3600

role_cards:
  card_dir: "data/role_cards"
  cards:
    gp-default-expert:
      display_name: "GP Expert"
      system_prompt: "You are a long-lived expert persona inside group-portrait."
```

See `config.yaml` for the full annotated configuration.

---

## Runtime Notes

- Startup loads `config.yaml`, initializes all backends, router, per-backend circuit breakers, token store, prompt logger, role-card registry, and optional session manager.
- All model calls are protected by the global `asyncio.Semaphore(cfg.concurrency.max_concurrent)`.
- All endpoints except `/health` require `X-API-Key`.
- Session persistence is SQLite-backed at `sessions.db`, while prompt and token logs are append-only JSON Lines.
- Fallback is primary backend first, then the configured `fallback_chain`, with HTTP 502 returned only after every candidate fails.

### `/status` Example

Requires API Key.

```json
{
  "status": "ok",
  "uptime_s": 4123,
  "routing_rules": 1,
  "calls_total": 238,
  "calls_last_1h": 17,
  "backends": {
    "deepseek": {
      "status": "ok",
      "model": "deepseek-v4-flash",
      "circuit_state": "closed",
      "consecutive_failures": 0,
      "max_concurrent": 15,
      "active_calls": 0
    }
  }
}
```

### Testing

The repo includes unit coverage for:

- request/response format validation
- prompt log metadata and caller filtering
- incremental JSON parsing for streamed structured output
- role-card prompt compilation
- session binding, truncation, and persistence behavior

Run the current test suite with:

```bash
python3 -m unittest discover -s tests -q
```

---

## Deployment

### Docker (recommended)

```dockerfile
FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8525
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8525"]
```

### Remote deploy via deploy.sh

The included `deploy.sh` supports two modes:

```bash
./deploy.sh            # full rebuild (image + container)
./deploy.sh hotpatch   # Python/YAML files only, no image rebuild
```

Edit the configuration variables at the top of `deploy.sh` to match your environment (SSH target, Docker network, data directory, etc.).

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | Yes (if using DeepSeek) | DeepSeek API key |

---

## Requirements

```
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.0
httpx>=0.27
openai>=1.30
pyyaml>=6.0
python-multipart>=0.0.9
```

---

## License

MIT
