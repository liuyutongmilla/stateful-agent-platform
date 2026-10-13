# Stateful AI Agent Platform

An agent that carries out administrative work on a person's behalf, built so that three things hold:

1. **A run can always be picked up where it stopped.** Cancel it, drop the connection, or `kill -9` the server; the work already done is kept.
2. **The agent can never do more than the person asking.** Every tool call is authorized in SpiceDB as that person.
3. **The loop is bounded.** A run makes at most 10 reasoning iterations, then reports what it did.

LangGraph · React · WebSockets · Model Context Protocol · SpiceDB

```
 React console ──one WebSocket, many sessions──▶ Gateway ──▶ Agent runtime (LangGraph)
   seq numbers, replay,                           hub with      intake → plan → [reason ⇄ act] → review → finalize
   acked commands                                 per-session   checkpoint after every node (SQLite)
                                                  replay buffer        │ MCP tools/call
                                                                       │ _meta: signed caller context
                                                                       ▼
                                                              Admin tool server (MCP, 22 tools)
                                                                       │ CheckPermission(user:<caller>, …)
                                                                       ▼
                                                                    SpiceDB
```

## Run it

Requires Python 3.11+ and Node 20+. No API key and no SpiceDB are needed for the default setup: it uses a
deterministic offline model and an in-memory evaluator of the same SpiceDB schema.

```bash
make setup
make build          # builds the console into frontend/dist
make dev-backend    # http://localhost:8000 serves the API and the console
```

For frontend work, run `make dev-frontend` alongside it and open http://localhost:5173.

Things to try in the console:

| Type this | As | What it shows |
|---|---|---|
| `run a health check` | Alice | Three reasoning turns, parallel tool calls, the five stages |
| `restart search-api` | Alice | Approval before a destructive tool |
| `restart api-gateway` | Dave | SpiceDB denies it: Dave maintains `search-api` only |
| `restart search-api` | Carol | The agent is never offered the tool (auditor is read-only) |
| `loop forever` | anyone | The 10-iteration limit |
| `stream 300`, then **Stop**, then **Resume run** | anyone | Resume from a checkpoint |
| `stream 300`, then **Drop the connection** | anyone | Reconnect and replay with no lost or repeated tokens |

### With real SpiceDB, a hosted model, or both

```bash
# SpiceDB (any reachable instance)
SPICEDB_ENDPOINT=localhost:50051 SPICEDB_TOKEN=<preshared key> make dev-backend

# A hosted model through LangChain
pip install -e "backend[anthropic]"
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... make dev-backend     # LLM_MODEL overrides the default

# Everything as separate services (SpiceDB + MCP server + platform)
cp .env.example .env && docker compose up --build
```

## How it works

### Agent runtime: `backend/app/agent/`

`graph.py` defines the workflow as a LangGraph state machine with five stages. `execute` is a ReAct loop of two
nodes, `reason` (one model call) and `act` (run the tool calls it asked for).

- **Bounded loop.** The iteration count lives in graph state. `reason` refuses to call the model once it reaches
  `AGENT_MAX_ITERATIONS` (default 10) and routes to `review`. `finalize` then writes its message without another
  model call, so the bound on loop invocations is strict. Planning is one additional call per run.
- **Checkpoints.** The graph runs with `AsyncSqliteSaver` and `durability="sync"`: the checkpoint for a node is on
  disk before the next node starts. State holds the conversation, plan, permitted tools, iteration count and review.
- **Resume.** `runtime.py` runs each request as a background task. If it stops early, the session is marked
  `interrupted` (also at startup, for anything left `running` by a crash) and `resume` re-enters the graph at the
  first unfinished node.
- **Approval.** `act` raises a LangGraph `interrupt` before any destructive tool. The pending decision is part of
  the checkpoint, so it survives a restart too.
- **Exactly-once side effects.** A node that is cut off mid-flight runs again from its start on resume. The
  tool-call id is sent to the tool server as an idempotency key, so a repeated mutating call returns the first
  result instead of acting twice.

### Gateway: `backend/app/gateway/`

One WebSocket carries any number of sessions. The protocol is documented at the top of `ws.py`.

- Each session is a channel with its own sequence numbers. The hub keeps the last 4,096 frames per channel.
- On reconnect the client sends the last `seq` it saw. The server replays what was missed, or sends a full
  snapshot rebuilt from the checkpoint if the client is too far behind or the server restarted.
- Runs publish to the hub whether or not anyone is connected, so a dropped socket never stops a run.
- Commands (`send`, `approve`, `resume`, `cancel`) carry a client id and are acknowledged. A client resends
  anything unacknowledged after reconnecting, and the server applies each id once.
- Each connection has a bounded send queue. A client that falls too far behind is disconnected and catches up
  through replay, rather than growing server memory.

### Tools and authorization: `backend/app/mcp_server/`, `backend/app/authz/`

22 administrative tools (users, roles, feature flags, services, incidents, API keys, usage, audit log) are served
over MCP. Every one passes through a single guard, `admin_tool` in `server.py`:

1. **Authenticate the caller.** The runtime mints a 60-second signed token for each call naming the user,
   organization, session, tool and tool-call id, and sends it in the MCP request `_meta`. The server rejects a
   missing, forged, expired, or wrong-tool token.
2. **Check SpiceDB** as `user:<caller>` against the organization, or against the specific service for
   service-scoped tools. The model is in `authz/schema.zed`.
3. **Audit** the decision, allowed or denied.

The agent has no identity of its own to fall back on. At the start of each run it asks the tool server which tools
the caller may use and offers only those to the model; the server-side check still runs on every call.

Role-changing tools write SpiceDB relationships, and checks are fully consistent, so a revoked role stops working
on the next call. The `owner` role cannot be granted or revoked through tools.

### Console: `frontend/src/`

`connection.ts` implements the client half of the protocol; `reducer.ts` turns events into view state and is
idempotent by message and tool-call id, so replays that overlap a snapshot are harmless.

## Verification

```bash
make test                                         # 70 tests; 16 more skip without SpiceDB
SPICEDB_BIN=/path/to/spicedb make test            # all 86, against a real SpiceDB
make loadtest
```

What the suite establishes, and where:

| Claim | Test |
|---|---|
| Exactly 10 model calls in the loop, then a clean finish | `test_agent.py::test_react_loop_stops_after_ten_model_invocations` |
| A checkpoint at each of the five stages | `test_agent.py::test_a_checkpoint_is_persisted_at_every_stage` |
| Resume after a model failure repeats only the failed step | `test_agent.py::test_failed_run_resumes_…` |
| Resume after `SIGKILL` of the server process | `test_processes.py::test_run_survives_sigkill_…` |
| Reconnecting three times mid-answer loses and repeats nothing | `test_gateway.py::test_reconnect_mid_stream_…` |
| Caller identity reaches the authorization check | `test_agent.py::test_caller_identity_reaches_…` |
| Every tool fails closed without a valid caller context | `test_mcp_tools.py` (run against both backends) |
| The in-memory evaluator agrees with SpiceDB on every check | `test_spicedb.py` |
| Tools work over HTTP to a separate MCP process | `test_processes.py::test_tools_work_over_streamable_http_…` |

### Load test

`loadtest/run.py` starts a server process and drives 50 sessions over 10 WebSocket connections. Each session runs
a tool-using request and then streams a 400-token answer. A second phase kills every connection twice mid-stream.

Measured on a shared 2-vCPU Linux container, client and server on the same host:

| Phase | Sessions intact | Tokens | p50 | p95 | p99 | Reconnects | Gaps |
|---|---|---|---|---|---|---|---|
| Steady | 50 / 50 | 25,305 | 3.2 ms | 7.9 ms | 11.6 ms | 0 | 0 |
| Chaos | 50 / 50 | 25,305 (899 replayed) | 3.0 ms | 8.2 ms | 10.1 ms | 20 | 0 |

Read these numbers for what they are:

- Latency is from the moment the server produces a token to the moment the client receives it. It measures the
  platform's delivery path, not a model: the server uses the mock model, which emits a token every 15 ms.
- It is loopback. Real network latency adds to it.
- It is one run on one machine. Run it on your own hardware before quoting a figure.

## Limits worth knowing

- **Single process.** The hub and run registry are in memory. Running several instances would need a shared event
  log (Redis streams, for example), sticky routing or a shared run registry, and Postgres for checkpoints.
- **Demo sign-in.** `/api/login` issues a token for any seeded user with no password. Set `DEMO_LOGIN=0` and put
  real authentication in front before exposing this anywhere.
- **Default secret.** `PLATFORM_SECRET` has a development default. Set your own.
- **The admin data is seeded and in memory.** The tools are real MCP tools with real authorization; what they
  administer is a stand-in.
- **Hosted models are adapter-tested only.** The LangChain adapter is tested against a scripted model. It has not
  been run against a live provider in this repository's test suite.
- **The Docker setup has not been built here.** `Dockerfile` and `docker-compose.yml` were written without a
  Docker daemon available; the same split deployment is covered by `test_processes.py` using local processes.
- **The in-memory authorizer** supports only what `schema.zed` uses (unions and arrows). It refuses schemas with
  intersections or exclusions instead of evaluating them wrongly.
- A session whose run keeps failing on resume has no "discard" action; start a new session.
