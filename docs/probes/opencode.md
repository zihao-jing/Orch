# Probe P4: OpenCode server

Run 2026-11-25 on macOS with `opencode v2.3.2` (`GET /api/info` returns `"version":"2.3.2"`).
Model for all turns: `alibaba-token-plan-cn/qwen3.8-flash`; second provider for the switch test:
`opencode/hy4-flash-free`. Scripts and temporary projects were in a scratch folder outside the repository.

## Setup used

- Own server: `OPENCODE_SERVER_PASSWORD=<random> opencode serve --hostname 127.0.0.1 --port <free port>`, started from a
  scratch folder. Clients use HTTP Basic auth, user `opencode`, the same password. No auth gives
  `401 {"_tag":"UnauthorizedError","message":"Authentication required"}`.
- The server has no data-directory isolation: it shares the user's OpenCode database (sessions, the job registry) with
  other servers. Probe sessions were created with `location.directory` in the scratch folder and deleted at the end
  (`DELETE /api/session/{id}`). Provider credentials were not read.
- Sessions are created by the client: `POST /api/session` with `{"model":{"id","providerID","variant"?},"location":
  {"directory":"/abs"},"title":"..","permissions":[{"action":"*","resource":"*","effect":"allow"}]}` returns
  `{"data":{"id":"ses_...","model":{..,"variant":"default"},...}}`. Without an allow rule, a shell tool call would ask
  for permission; `opencode run --auto` does the same thing. All request and response bodies are JSON, errors have
  `_tag`, `message`.
- A turn starts by `POST /api/session/{id}/prompt` (returns at once, 200). It does not wait for the turn. Events come on
  `GET /api/event` (SSE, all sessions on the server; filter on `data.sessionID`; `shell.*` events carry the session only
  in `info.metadata.sessionID`). The stream stayed open for 12 minutes of silence.

## Answers

| # | Question | Answer | Status |
|---|---|---|---|
| 1 | Message while a turn runs | Accepted at once (HTTP 200 with the message id, event `session.inbox.enqueued`), but handed to the model only at the next step boundary: after the running tool call finishes, or after the current text step ends. Same turn (one `execution`), no new turn. `delivery:"queue"` also joins the same execution, but only after the model's final step (it answers twice). The client may supply the id (`msg_...`); a repeated id is accepted without a second user message. A not yet delivered message can be listed and cancelled. | verified |
| 2 | Client disconnect, reconnect | The server runs turns and jobs independently of any connection. No replay for SSE: `Last-Event-ID` is ignored and `GET /api/experimental/session/{id}/log` returned only `log.synced`. Recover by reading `GET /api/session/{id}/message` (also mid-turn: running tool shown), `/api/session/active`, `/api/experimental/job`. | verified (replay endpoints verified absent) |
| 3 | Background jobs | `shell` tool with `background:true`; job list API; ended jobs vanish from the list; exit code in `shell.exited` event, `GET /api/shell/{id}` and a synthetic message; the server wakes the session itself, once per job end, also when the turn is running. No intermediate or final marker: check the job list when `session.execution.succeeded` arrives. A 12 minute job (exit 4) ended and woke the session after 724.6 s. Turn interrupt kills only the foreground tool; the background job survives and wakes the session. | verified |
| 4 | Switch model, variant, provider in one session | `POST /api/session/{id}/model` returns 204, session id and history kept, the next turn used the new model and knew the earlier content (ZEBRA test across `alibaba-token-plan-cn` -> `opencode` -> `alibaba-token-plan-cn`). The call does not validate the model, provider or variant; errors appear on the next turn. | verified |
| 5 | Variants and model list | `GET /api/model` returns every model with `variants:[{id,settings}]` (per model, may be empty), `providerID`, `modelID`, `enabled`, `limit`. `GET /api/provider` lists providers. | verified |
| 6 | Errors | Unknown session: 404 `SessionNotFoundError`; malformed id 400. Bad model/provider/variant: `session.execution.failed` with `error.type` `provider.no-route`. Provider key rejected: `provider.auth` (status 401). Model actually used: `session.step.started.data.model` and each assistant message's `model`. | verified |
| 7 | Terminal joins while another client is connected | Yes. `opencode --server URL --session ID` (password in `OPENCODE_SERVER_PASSWORD`) opened in a pty showed the history, rendered a turn started by an API client, and the API client saw the turn the terminal started. | verified (about 30 s, one pty) |
| 8 | Server restart | Sessions and history are kept (SIGTERM and SIGKILL). A running background job dies with the server in both cases, nothing wakes the session, and the job list keeps showing the dead job as `running` while `GET /api/shell/{id}` returns 404. | verified |
| 9 | `nohup ... &`; compaction | `nohup cmd &` in a foreground shell call returns at once, is not in the job list and never wakes the session. A per-session deny rule `{"action":"shell","resource":"*nohup*","effect":"deny"}` rejects it, but only simple forms (see below). Compaction: `POST /api/session/{id}/compact`, completes as one execution. | verified |

## Details

### 1. Messages while a turn runs

Turn A: "run `sleep 20` in the foreground, then reply ALPHA". At 8 s:

```
POST /api/session/ses_X/prompt
{"text":"Additionally, put the word BANANA at the end of your final answer.","delivery":"steer"}
-> 200 {"data":{"id":"msg_212902895001Hq1ACe8qrY9bOo","sessionID":"ses_X","time":{"created":..},
        "type":"user","payload":{"text":"..."},"delivery":"steer"}}
```

Events (seconds since start):

```
1.1  session.inbox.enqueued   {inboxID:msg_...0938 (first prompt)}
1.1  session.execution.started
1.1  session.inbox.delivered  {inboxID:msg_...0938}
3.4  shell.created            {info:{id:sh_..,status:running,command:"sleep 20"}}
9.1  session.inbox.enqueued   {inboxID:msg_...2895, item:{type:user,delivery:steer}}     <- receipt, during the tool
23.5 shell.exited / session.tool.success / session.step.ended {finish:"tool-calls"}
23.5 session.inbox.delivered  {inboxID:msg_...2895}                                       <- model gets it here
25.1 session.step.started ... session.text.ended {text:"ALPHA\n\nBANANA"}
25.6 session.execution.succeeded
```

- Receipt: the POST response (200 and the id) and `session.inbox.enqueued`. Arrival at the model: `session.inbox.delivered`
  with the same `inboxID`. The `inboxID` is the message id; send `"id":"msg_<anything>"` to choose it (an id not starting
  with `msg_` gives 400 `Expected a string starting with "msg_"`). A second POST with the same id returned 200 and the
  same id and did not create a second user message (it was followed by an extra empty `idle` record).
- The user message appears in `GET .../message` only once delivered. Before that, `GET /api/session/{id}/inbox` lists it
  (`{"data":[{id,type:"user",payload:{text},delivery}]}`). `DELETE /api/session/{id}/inbox/{id}` -> 204, event
  `session.inbox.cancelled`; the message was then never delivered. Deleting a delivered id also returned 204 (no effect).
  So "did it arrive" = `GET /message/{id}` (200 / 404 `MessageNotFoundError`) or presence in `/inbox`.
- Text-only turn ("count 1 to 80"), steer at 3 s: the current step finished all 80 numbers (4.4 s), then
  `inbox.delivered`, then one more step answered "BANANA", one `execution.succeeded`. So a message cannot cut a
  running step; it is read at the next step.
- `delivery:"queue"` (default if omitted is `steer`): the first step ended with "ALPHA", then the queued message was
  delivered after that final step and a second step answered "ALPHA BANANA"; still one execution.
- Message to an idle session: `inbox.enqueued`, `execution.started`, `inbox.delivered` together (a new turn).

### 2. Disconnect and reconnect

- Client A subscribed to `/api/event`, started a turn (`sleep 12` foreground), dropped its socket after 5 s. The turn
  finished with no client connected: `GET /message` shows the assistant "DONE" and an `idle` record
  `{"type":"idle","outcome":"succeeded"}`.
- Reconnect tries: `GET /api/event` with `Last-Event-ID: evt_...` returned only `server.connected`; `GET
  /api/experimental/session/{id}/log?after=0&follow=false` (and follow=true, after=seq) returned only
  `{"type":"log.synced","aggregateID":"ses_X","seq":13}`. Events carry `id` (`evt_...`) and durable ones
  `durable:{aggregateID,seq}`, but no endpoint replayed them. Missed events must be rebuilt from state.
- State to read after a reconnect:
  - `GET /api/session/{id}` -> `model`, `outcome` (`succeeded|failed|interrupted` of the last turn), `time.busy` while a
    turn runs.
  - `GET /api/session/active` -> `{"data":{"ses_X":{"type":"running"}}}`, `{}` when idle.
  - `GET /api/session/{id}/message?limit=&order=asc|desc&cursor=&type=` -> `{"data":[..],"cursor":{"previous","next"}}`.
    Types: `user`, `assistant` (`content` parts `reasoning|text|tool`, tool has `state.status`), `synthetic`,
    `idle` (turn end record with `outcome`, `error`), `model-switched`, `compaction`. Newest first by default. In a running turn the
    assistant message already exists with the tool `running`.
  - `GET /api/experimental/job?sessionID=` for jobs.
- `session.execution.succeeded|failed|interrupted` is the live turn end; the `idle` message is the durable one.

### 3. Background jobs

Start (the model must call the shell tool with `background:true`; the tool result tells it not to poll and that it will be
resumed). Job list:

```
GET /api/experimental/job[?sessionID=ses_X][&status=running|completed|error|cancelled][&kind=shell][&descendants=true]
-> {"data":[{"kind":"shell","id":"sh_21291f9b..","sessionID":"ses_X","status":"running",
             "title":"sleep 25; exit 3","time":{"started":1795607886262},
             "shellID":"sh_21291f9b..","command":"sleep 25; exit 3"}]}
```

- The list is global to the database (jobs of other sessions and servers show up), so filter by `sessionID`. Only
  running jobs are listed; an ended job leaves the list at once (`?status=completed` returned `[]`). The list can also hold ghost entries (restart, item 8).
- End of job (exit 3, 25 s):
  ```
  shell.exited         {id:sh_..,exit:3,status:"exited"}        (event; shell.* events are not session-tagged)
  session.inbox.enqueued {item:{type:"synthetic",delivery:"steer",payload:{text:"<shell id=.. state=\"completed\"
                          command=\"sleep 25; exit 3\">\n(no output)\n\nExit code: 3\n</shell>"}}}
  session.execution.started / session.inbox.delivered ... session.text.ended ... session.execution.succeeded
  ```
  and the same as a `synthetic` message with `metadata:{source:"shell",shellID,jobID,state:"completed",exit:3}`.
  `GET /api/shell/{id}` -> `{"status":"exited","exit":3,"pid":..,"file":".../sh_...out","time":{"started","completed"}}`;
  `status:"killed"` for a killed foreground tool. A failed job (exit 3, 5) has the same shape with a nonzero exit: OpenCode treats it as
  `completed`, the exit code is the only failure signal. (The job list's own status enum has `error`; not seen.)
- Several jobs (8 s exit 0, 14 s exit 5, 20 s exit 0 with output, started in three calls of one step): all three listed
  as `running` after 4 s; the turn ended at 6.0 s (`execution.succeeded`, jobs still running = intermediate end).
  Each job end produced its own wake turn: 12.1 s, 18.1 s, 24.1 s; the first two wake turns ended with the other jobs
  still in the list (intermediate), the last with an empty list (final). No merge of wake-ups when ends are some seconds apart.
- Job end while a turn is running (background `sleep 6; exit 2`, then foreground `sleep 14`): `shell.exited` at 10.3 s,
  `inbox.enqueued` (synthetic) at once, `inbox.delivered` only when the tool finished (20.2 s); the model's summary named exit code 2. No extra turn.
- Intermediate vs final: nothing in the events says which. Rule that works: on `session.execution.succeeded` for session S,
  `GET /api/experimental/job?sessionID=S`: any `running` -> intermediate, none -> final. Race seen: the job leaves the list at the same
  instant the wake turn starts, so the check at the end of a turn is consistent.
- 12 minute job (`sleep 720; exit 4`): turn ended at 7.9 s with `STARTED`; job listed `running` throughout; `shell.exited`
  exit 4 at 724.6 s, synthetic message, wake turn answered "completed with exit code 4" at 726.7 s. The SSE connection
  stayed open all that time. (No time limit was met up to 12 minutes.)
- Interrupt: `POST /api/session/{id}/interrupt` (query `resume` exists) -> `{"interrupted":true}` (`false` when idle). A
  turn with a background `sleep 30; touch f` and a foreground `sleep 60`: at 14 s interrupt -> `shell.exited
  {status:"killed"}` for the foreground command (process gone), `session.step.failed {error:{type:"aborted"}}`,
  `session.execution.interrupted {reason:"user"}`, `idle` record `outcome:"interrupted"`. The background job stayed in the
  list, finished at 34.8 s (file `f` created) and woke the session. So interrupt is safe for jobs.
- `nohup`: `nohup sleep 33 > /dev/null 2>&1 &` run as a foreground call: `shell.created` and `shell.exited` (exit 0) within the same
  instant, job list empty, process still running after the turn ended, no wake-up. With session permissions
  `[{action:"shell",resource:"*nohup*",effect:"deny"}, {action:"shell",resource:"*&",effect:"deny"}]` the call fails with
  `session.tool.failed {error:{type:"permission.rejected",message:"Permission denied: shell"}}` and the model reports it.
  The match is a glob on the whole command text: `echo hi`, `echo a && echo b`, `(sleep 3 > /dev/null 2>&1 &)` and
  `sleep 2 & wait` were NOT denied under `*nohup*`, `*&`, `*setsid*`. So prevention is partial; detection by
  process is still needed for the rest. Also `shell` background has no documented time limit here.

### 4. Switching model, variant, provider

```
POST /api/session/ses_X/model   {"model":{"id":"hy4-flash-free","providerID":"opencode"}}   -> 204 (no body)
POST /api/session/ses_X/model   {"model":{"id":"qwen3.8-flash","providerID":"alibaba-token-plan-cn","variant":"none"}} -> 204
GET  /api/session/ses_X  -> data.model = {"id","providerID","variant"}   ("default" when no variant was given)
```

- Same session id; history kept; message list gets a record `{"type":"model-switched","model":{..},"previous":{..}}`.
- Turn 1 on `alibaba-token-plan-cn/qwen3.8-flash` ("remember ZEBRA"), then switched to `opencode/hy4-flash-free`: the turn
  answered "ZEBRA" (step model reported `opencode/hy4-flash-free`). Switch to `alibaba-token-plan/qwen3.8-flash` variant `low`:
  the turn failed with `provider.auth` (that provider has a key the API rejects); the failure is recorded as the turn's
  `idle` with `outcome:"failed"` and `error`. Switch back to `alibaba-token-plan-cn` variant `none`: answered "ZEBRA".
- The call does not check anything: `nonexistent-model`, `nonexistent-provider` and `variant:"bogus"` all returned 204 and
  were stored in the session. They fail on the next turn (item 6). The smoke test in MOD-7 therefore cannot rely on this call
  and needs a real turn in a temporary session. A model switch while a turn is running was not tested.

### 5. Variants and models

```
GET /api/model -> {"location":{..},"data":[{"id":"qwen3.8-flash","modelID":"qwen3.8-flash","providerID":"alibaba-token-plan-cn",
  "name":..,"capabilities":{"tools":true,"input":["text"],..},"variants":[{"id":"none"},{"id":"low"},{"id":"medium"},{"id":"xhigh"}] (each
  with "settings":{..}), "cost":[..],"enabled":true,"status":"active","limit":{"context":..,"output":..}}, ...]}
GET /api/model/default, GET /api/provider ([{id,integrationID,name,activation,package,settings}]), GET /api/provider/{id}
```

- 148 models on this server from 4 providers (`alibaba-token-plan`, `alibaba-token-plan-cn`, `opencode`, `zenmux`). Variant ids differ per
  model: `qwen3.8-flash`: none, low, medium, xhigh; `qwen3.7-plus`: none, high, max; `glm-5.2`: high, max; `kimi-k2.6` (cn): none, thinking;
  `opencode/tidepool-alpha-free`: minimal..max; some models have `[]`.
- `settings` (provider and variant) can contain API keys; an adapter must drop `settings` before showing or saving anything.
- `enabled:true` does not mean the key works (`alibaba-token-plan` is listed and enabled, but its calls fail with `provider.auth`).
- `variants` in the model list are listed per model and per provider; the effort choice for the dashboard is `variants[].id`.

### 6. Errors and actual model

```
404 {"_tag":"SessionNotFoundError","sessionID":"ses_..","message":"Session not found: ses_.."}      (all session endpoints, incl. /wait, /compact, /inbox)
400 {"_tag":"InvalidRequestError","message":"Expected a string starting with \"ses\"\n  at [\"sessionID\"]","kind":"Params"}
400 {"_tag":"InvalidRequestError","message":"Missing key\n  at [\"text\"]","kind":"Payload"}
404 {"_tag":"MessageNotFoundError",..}   404 {"_tag":"ShellNotFoundError",..}
session.execution.failed {"error":{"type":"provider.no-route","message":"Model unavailable: nonexistent-provider/qwen3.8-flash"}}
session.execution.failed {"error":{"type":"provider.no-route","message":"Variant unavailable for alibaba-token-plan-cn/qwen3.8-flash: bogus"}}
session.execution.failed / session.step.failed {"error":{"type":"provider.auth","message":"Invalid API-key provided...","status":401,"response":{..}}}
```

- Turn failures arrive as events, not as the POST response (POST prompt stays 200). `session.step.failed` precedes
  `session.execution.failed`. `provider.auth` includes the provider's response body; shorten it before logging. A transient
  provider error (`session.retry.scheduled`, 429 or 5xx) could not be provoked and was not seen.
- Through the CLI, `opencode run --server URL --session ses_<unknown>` does not fail: it creates a session with that id and answers.
  Over HTTP the same id gives 404. For "session lost" detection (SES-2), use the HTTP 404 on `GET /api/session/{id}`.
- Actual model: `session.step.started.data.model {id,providerID,variant}` per step and `model` on each assistant message
  (`GET .../message`); the session's `model` is the selection for the next turn.

### 7. Terminal

`opencode --server http://127.0.0.1:PORT --session ses_X` run with `OPENCODE_SERVER_PASSWORD` set, in a pty (120x40), cwd = session
directory:
- After 7 s the screen showed the session's earlier messages ("Reply with the single word PONG." / "PONG"), the model name and the
  session title.
- While it was open and an SSE client was connected, an API client posted a prompt; the terminal showed the question and answer
  `TERMTEST`.
- A line typed in the terminal started a turn; the API client received `session.inbox.enqueued` (payload text) and
  `session.text.ended "FROMTUI"`, `execution.succeeded`.
- It printed `Switched agent to Build` and used agent `build`. The pty exited cleanly on SIGTERM. Not tested: terminal open while a
  tool or job runs, two terminals, the TUI's effect on the session model.
- `opencode --help` (v2.3.2) flags: `--server`, `--session/-s`, `--continue`, `--prompt`, `--auto`, `--standalone`.

### 8. Server restart

Server restarted twice with a session whose background job (`sleep 45; touch f` / `sleep 40; touch f`) was running:
SIGTERM to the server, then a new `opencode serve` on the same port; and SIGKILL, then restart.
- Session and messages readable after the restart (`GET /api/session/{id}/message` unchanged).
- Job process: gone 3 s after SIGTERM, gone 2 s after SIGKILL (`pgrep` empty), marker file `f` never created, no wake-up message
  (the session stayed idle for 70+ s).
- The job list still returned the dead job as `status:"running"` after restart and kept doing so (also seen from the other
  server); `GET /api/shell/{id}` -> 404 `ShellNotFoundError`. Detection of a lost job: job listed `running` but `/api/shell/{id}` is
  404 (or the server's `pid` from `GET /api/info` changed since the job began). The ghosts disappeared only when their sessions were deleted.
- A job of another server on the same database (12 minute job) was not affected by restarting the second server.

### 9. Compaction

```
POST /api/session/ses_X/compact  {}  -> 200 {"data":{"id":"msg_..","type":"compaction","delivery":"steer",..}}
POST /api/experimental/session/ses_X/wait {} -> 204 when idle (6.6 s)
events: session.inbox.enqueued -> session.execution.started -> session.compaction.started {reason:"manual"}
        -> session.compaction.delta {text} xN -> session.compaction.ended {text:<summary>,model:{..}} -> session.execution.succeeded
GET .../message: {"type":"compaction","status":"completed","summary":"## Objective ..","metadata":{"keepID":"msg_.."},"tokens":{..}}
```

The session works afterwards (it answered a question about its first request). The summary is produced by the session's current model. A
compaction while a turn runs was not tested (a `delivery` field exists, as for prompts).

## Capability values for the Adapter

| Capability | Value | Because |
|---|---|---|
| `sends_while_writing` | true (applied at the next step, not mid-step) | items 1; Orch should treat `delivered` as the receipt and `execution.succeeded` as the turn end |
| `reconnects` | true for the server and the session state; false for missed events | server survives the client; no SSE replay, rebuild from `/message`, `/session/active`, jobs |
| `job_record` | true (job list API, experimental; ended jobs only through `shell.exited`, `/api/shell/{id}` and the synthetic message) | item 3; dead jobs after a server restart are shown as running |
| `shares_terminal` | true | item 7 |
| `compacts` | true | item 9 |

Adapter notes:
- One server per project or per Orch (`serve`), one session per agent. Keep the pid and URL; on reconnect check `GET /api/info`
  and `/api/session/{id}`. A server restart kills jobs and the wake-up chain: treat all jobs of that server as `lost`.
- Turn end: `session.execution.succeeded|failed|interrupted` with `data.sessionID`; error class from `error.type`
  (`provider.auth`, `provider.no-route`; others not seen).
- `catalog()`: `GET /api/model`, keep `providerID`, `modelID`, `variants[].id`, drop `settings`.
- `terminal_command()`: `opencode --server URL --session ID` with `OPENCODE_SERVER_PASSWORD` in the environment (never in argv).

## Not tested

Model switch during a running turn; rate-limit or overload errors; terminal during a running tool or job; a job longer than 12 minutes;
more than one server restart with a pending wake-up; behaviour when two servers share one session; per-agent system prompt and
`OPENCODE_CONFIG_CONTENT` against `serve` (today's backend sets it per turn).
