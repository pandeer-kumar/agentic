# OpsAgent

Ask operational questions in plain English across Jira, Zendesk, xMatters and other MCP servers. The agent
works out which systems to use, fetches and cleans the data, computes results with SQL, and returns answers,
charts and reports. Every session, query and step is recorded in PostgreSQL.

## Run it

Requires Python 3.11+ and Docker.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
docker compose up -d
```

Without the Compose plugin, start the same database with:

```bash
docker run -d --name agentic-postgres --restart unless-stopped -e POSTGRES_USER=agentic -e POSTGRES_PASSWORD=agentic_dev -e POSTGRES_DB=agentic -p 127.0.0.1:5433:5432 -v agentic_pgdata:/var/lib/postgresql/data postgres:16-alpine
```

Start the mock model and the app in two terminals, then open http://127.0.0.1:8000.

```bash
.venv/bin/opsagent-mock-azure
```

```bash
.venv/bin/opsagent
```

Run the tests with Postgres running:

```bash
.venv/bin/python -m pytest
```

## Use real Azure OpenAI

Set these in `.env`, then restart `opsagent`. The mock is no longer needed.

```
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
AZURE_OPENAI_API_VERSION=<api-version-your-resource-supports>
```

The mock follows fixed rules and proves the plumbing only. Judge answer quality with a real deployment.

## How a query flows

1. `resolve_intent` matches the question against `config/knowledge/intents.md` to find systems and board keys.
2. `fetch_data` calls the MCP tool and follows pagination.
3. The cleaner flattens, trims, redacts and de-duplicates records, then stores them in a per-session DuckDB table.
4. The model receives a compact profile, not the raw rows, and computes figures with read-only SQL.
5. `create_chart` and `write_report` save artifacts. Each step is written to PostgreSQL.

## Configure

| To change | Edit |
|---|---|
| MCP servers, credentials, allowed tools | `config/servers.yaml` |
| Intents, Jira board keys, fetch recipes | `config/knowledge/intents.md` |
| Field selection and pagination per system | `config/cleaners.yaml` |

Replace the mock entries in `servers.yaml` with real servers. Tool names and arguments in `intents.md`, and field
paths in `cleaners.yaml`, must then match what those servers return. A system with no cleaner profile uses the
generic cleaner.

## Verify a query

The trace panel shows every event. The same data is available from the API and SQL:

- `GET /api/sessions/{id}` lists a session's queries, status and tokens.
- `GET /api/sessions/{id}/events` returns the full session timeline.
- `GET /api/queries/{id}` returns one query's events and artifacts.

```sql
SELECT seq, type, name, status, duration_ms, payload
FROM events WHERE query_id = '<query-id>' ORDER BY seq;
```

Raw payloads are not stored. Events record their size, record counts and a SHA-256 hash.

## Safety and limits

- Only tools matching a server's `allow` list can run, so write tools stay blocked by default.
- SQL is limited to one `SELECT` with file and network access disabled.
- Emails, phone numbers and common secret patterns are redacted before data reaches the model or traces.
  Pattern-based redaction can miss values, so review it before using regulated data.
- The API has no authentication and binds to localhost. Add auth before exposing it to a network.
