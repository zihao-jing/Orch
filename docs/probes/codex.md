# Probe P3: Codex CLI app-server

Version: `codex-cli 0.172.0` (macOS arm64, ChatGPT-account login), checked 2026-11-25. The table in
`assets/ORCH-ISSUES.md` says 0.171.0; everything below is 0.172.0.
Model used for all live turns: `gpt-5.6-luna`, effort `low` (cheap, listed by `model/list`).
Scripts and temporary projects: `/Users/fancy/.claude/jobs/4b93f1df/tmp/probes/p3` (not part of the repository).

## Summary

| # | Question | Answer | Status |
|---|---|---|---|
| 1a | Server stays up when the client leaves | Yes. One server ran 25+ minutes over dozens of connects and disconnects. | verified |
| 1b | Second client at the same time, same thread | Yes, any number of connections. Item and turn events reach a connection only after it calls `thread/resume` on the thread (or created it with `thread/start`). Others get only `thread/status/changed` and `thread/readState/changed`. | verified |
| 1c | Running turn when the client leaves | Keeps running, also with nobody connected (26 s turn, final message saved). | verified |
| 2 | Read what was missed | `thread/turns/list` (`itemsView: "full"`), `thread/items/list`, `thread/read`. Complete, including the final `agentMessage` of a turn that ended with nobody connected. Works on unloaded threads too. | verified |
| 3a | Message while a turn runs | `turn/steer` with `expectedTurnId`. Answer is only `{turnId}`; no echo of the id there. | verified |
| 3b | Matching a message id | `clientUserMessageId` is echoed as `clientId` on the `userMessage` item (`item/started` and `item/completed`, and in later reads). | verified |
| 3c | Message when idle | `turn/start`. A `turn/start` during a running turn also steers into the active turn (same turn id returned). `turn/steer` when idle fails: "no active turn to steer". | verified |
| 4a | Background job: start and list | Worker's shell tool with a short yield leaves the command running. Listed by `thread/backgroundTerminals/list`; entry disappears when the job ends. | verified |
| 4b | Exit code, success, failure, several | Not in the list. The `commandExecution` item gets `item/completed` with `exitCode`; status `completed` (0) or `failed` (non-zero). Two jobs at once worked. | verified |
| 4c | Session wakes itself when a job ends | **No.** No new turn, with a client connected (80 s) and without one. Only the `item/completed` for the job arrives (to subscribed connections), after `turn/completed`. | verified |
| 4d | Intermediate versus final turn end | `turn/completed` carries no marker. Call `thread/backgroundTerminals/list` after it: non-empty means intermediate. | verified |
| 5a | Job lives while the server lives | Only if the thread stays loaded. Default: an idle thread with no subscribed connection is unloaded about 60 s after the last one left, and the unload **kills its jobs** (`exitCode -1`, `failed`). With `-c thread_unload_delay_secs=86400` a job ended normally (exit 0) with nobody connected. | verified (61 s job; 12 min job: see 5c) |
| 5b | Survives a turn interrupt | Yes (`turn/interrupt` of a later turn; the earlier job and a job started inside the interrupted turn kept running). | verified |
| 5c | 12-minute job | Yes with `thread_unload_delay_secs=86400`: a 720 s job (started, client gone after 7 s, one turn started and interrupted at about 118 s, polled every 30 s by fresh connections) ended at about 725 s with `completed`, `exitCode 0` in its original turn. Without that setting it is killed at about 60 s (section 5). | verified |
| 6a | `thread/resume` with unknown id | Error -32600 `no rollout found for thread id <id>`; non-UUID: `invalid session id: ...`. | verified |
| 6b | Model or effort per turn | `turn/start` params `model`, `effort`. Sticky: becomes the thread's setting. Same thread id, history kept (a secret word survived). | verified |
| 7 | `model/list` and actual model | Lists `supportedReasoningEfforts` and `defaultReasoningEffort` per model. Actual model is in the `thread/start` and `thread/resume` responses (`model`, `reasoningEffort`) and `thread/settings/updated`. | verified |
| 8 | Error forms | `error` notification and failed turn carry `codexErrorInfo`; bad model gives `modelNotFound`. Others from the schema only. | partly verified |
| 9 | Terminal joins while Orch stays connected | Yes, `codex resume --remote unix://PATH <thread-id> "<prompt>"` in a pty; both see the same events. | verified |
| 10a | Prevent or detect `nohup ... &` | Nothing blocks it. `&`, `nohup` and `nohup ... &` all got the process killed when the command ended, not listed, no file written. Detect by command text and an empty job list. | verified (macOS; `setsid` does not exist there) |
| 10b | `thread/compact/start` | Works; result `{}`; a turn with one `contextCompaction` item; same thread id; history kept. | verified |

## Recommended capability values

```
sends_while_writing = True    # turn/steer, or turn/start during a turn
reconnects          = True    # only with thread_unload_delay_secs large AND thread/resume after every reconnect
job_record          = True    # thread/backgroundTerminals/list plus item/completed(commandExecution).exitCode
shares_terminal     = True    # codex resume --remote unix://...
compacts            = True    # thread/compact/start
wakes_itself        = False   # not in the caps list; JOB-4 does not hold, JOB-5 is the only path
```

## Transport

`codex app-server --listen unix://PATH` speaks **WebSocket over the unix socket** (not JSON lines): plain HTTP/1.1
`GET / ... Upgrade: websocket`, answer `101 Switching Protocols`, then one JSON-RPC message per text frame (client
frames masked). The file at PATH is a symlink to `/private/tmp/codex-daemon-501/<hash>` (short-path workaround); the
server removes it on SIGTERM. `ws://IP:PORT` is also offered. Server started as

```
codex app-server --listen unix:///ABS/s.sock -c 'sandbox_mode="workspace-write"' -c 'approval_policy="never"' \
    -c thread_unload_delay_secs=86400
```

Handshake (all requests carry `id`; the notification has none):

```
-> {"id":1,"method":"initialize","params":{"clientInfo":{"name":"probe","title":"probe","version":"0"},"capabilities":{"experimentalApi":true}}}
<- {"id":1,"result":{"userAgent":"probe/0.172.0 (Mac OS ...)","codexHome":"...","platformFamily":"unix","platformOs":"macos"}}
-> {"method":"initialized"}
```

`experimentalApi: true` was set in every run; whether the job list needs it was not tested.

Create, send:

```
-> thread/start  {"cwd":ABS,"model":"gpt-5.6-luna","sandbox":"workspace-write","approvalPolicy":"never","config":{"model_reasoning_effort":"low"}}
<- {"result":{"thread":{"id":TID,...},"model":"gpt-5.6-luna","reasoningEffort":"low","approvalPolicy":"never","sandbox":{...},...}}
-> turn/start    {"threadId":TID,"input":[{"type":"text","text":"..."}]}      (optional "clientUserMessageId","model","effort")
<- {"result":{"turn":{"id":TURN,"status":"inProgress",...}}}
```

Typical notifications of one turn (shortened; noise such as `mcpServer/startupStatus/updated`,
`thread/tokenUsage/updated`, `account/rateLimits/updated`, `turn/moderationMetadata` omitted):

```
thread/status/changed {threadId, status:{type:"active",activeFlags:[]}}
turn/started          {threadId, turn:{id,status:"inProgress"}}
item/started|completed userMessage {clientId: <clientUserMessageId>|null}
item/started|completed reasoning
item/started          commandExecution {command:"/bin/zsh -lc '...'", processId, source:"unifiedExecStartup", status:"inProgress"}
item/commandExecution/outputDelta {itemId, delta}
item/completed        commandExecution {status:"completed"|"failed", exitCode, aggregatedOutput, durationMs}
item/started agentMessage {phase:"commentary"|"final_answer"} ; item/agentMessage/delta ; item/completed agentMessage {text}
thread/readState/changed
thread/status/changed {status:{type:"idle"}}
turn/completed        {threadId, turn:{id,status:"completed"|"interrupted"|"failed",error}}
```

The final reply of a turn is the `agentMessage` with `phase:"final_answer"` (commentary messages come earlier in the
same turn). Thread status types: `notLoaded`, `idle`, `active`, `systemError`.

## 1. Server, several clients, subscription

* Client A created the thread and ran a turn (`sleep 25`); client B connected during it and called
  `thread/loaded/list` (`{"data":[TID]}`), `thread/read`, then `thread/resume {"threadId":TID}`. After A closed its
  socket, B received every remaining notification up to `turn/completed`. The turn was not affected.
* With **nobody** connected: A closed after `item/started commandExecution`; 35 s later C connected; the thread was in
  `thread/loaded/list`, `thread/turns/list` showed the turn `completed` with the command output and final message
  `FINISHED`.
* Subscription is per connection and per thread. Test with connections C (started a turn on a loaded thread with
  `turn/start`, no resume), D (nothing) and E (`thread/resume`): in 8 s C and D saw 2 and 4 notifications
  (`thread/status/changed`, `thread/readState/changed`, also for other threads), E saw all 8 item events. After the
  turn, D and C received `thread/status/changed` idle but **no `turn/completed`**; only E did. `thread/start`
  subscribes the creating connection. `thread/resume` on a loaded thread is cheap and returns the thread with its
  current `status`, `model`, `reasoningEffort` plus `initialTurnsPage`. It also sends a `deprecationNotice` about
  `includeTurns`.
* So an adapter must call `thread/resume` right after every connect and keep its connection open; the cheap
  turn-end signal that needs no subscription is `thread/status/changed` to `idle`.
* A connection sees `thread/status/changed` of **all** threads on the server; filter by `threadId`.
* `thread/unsubscribe {"threadId"}` exists (not used).

## 2. Reading what was missed

```
-> thread/turns/list {"threadId":TID,"itemsView":"full","sortDirection":"asc","limit":N,"cursor":null}
<- {"data":[{"id","status","error","items":[userMessage, reasoning, commandExecution, agentMessage,...],"startedAt","completedAt"}],"nextCursor","backwardsCursor"}
-> thread/items/list {"threadId":TID,"turnId":TURN?,"limit":2,"cursor":null|"<string>"|{"type":"item","itemId":ID}}
<- {"data":[{"turnId","item":{...},"startedAtMs","completedAtMs"}],"nextCursor"}
-> thread/read {"threadId":TID,"includeTurns":true}     # includeTurns is deprecated: prefer turns/list
```

* `itemsView` is `notLoaded` | `summary` (default) | `full`. Default direction of `turns/list` is descending; of
  `items/list` ascending.
* `items/list` after an anchor `{"type":"item","itemId":...}` returns the items after it (needs `turnId`); a bare id
  string is rejected (`invalid cursor`). This gives an exact resume position after a restart (store the last item id
  and turn id; `nextCursor` strings also work).
* `thread/read` without `includeTurns` returns metadata only (`turns: []`, `status`, `preview`, `updatedAt`).
  `thread/read`, `turns/list` and `items/list` work on an **unloaded** thread and do not load it.
* Complete: the final message of a turn that ended with nobody connected was present, and `commandExecution` items
  held `aggregatedOutput`, `exitCode`, `durationMs`.
* A background command that ends after the turn is written into its **original** turn (it is the last item of that
  turn), not a new one.

## 3. Messages while a turn runs

```
-> turn/steer {"threadId":TID,"expectedTurnId":TURN,"clientUserMessageId":"cu-steer-1","input":[{"type":"text","text":"..."}]}
<- {"result":{"turnId":TURN}}
notif item/started userMessage {clientId:"cu-steer-1"} ; item/completed userMessage ...     (same turn, no new turn/started)
```

* The steered text was used in the same turn (final reply `SECOND STEERED`).
* Wrong turn id: `{"error":{"code":-32600,"message":"expected active turn id \`nope\` but found \`<id>\`"}}`.
* Idle: `{"error":{"code":-32600,"message":"no active turn to steer"}}`.
* The acknowledgement carries no message id. The proof of arrival is the `userMessage` item with `clientId` equal to
  the `clientUserMessageId` sent (also visible later in `thread/turns/list`, which makes `arrived(message_id)`
  answerable after a restart). The id can be any string ("cu-1"); a terminal-typed message gets a UUID there.
* `turn/start` while a turn is running did not start a second turn: it returned the active turn's id and the text went
  in like a steer (`userMessage` with `clientId:"cu-second"`).
* `thread/queue/add {"threadId","clientUserMessageId","input"}` queues a message that starts its own turn after the
  current one (`thread/queue/changed`, then `turn/started`, `userMessage clientId:"cu-q1"`). `thread/queue/list`
  shows it. The schema also says `activeTurnNotSteerable` (`review`, `compact`) is returned when steering is not
  possible; the compact case was not hit.
* Observed once: `thread/compact/start` sent while a turn was in progress ended that turn at once (`turn/completed`
  status `interrupted`) and then ran the compaction; do not compact with a turn running.

## 4. Background terminal jobs

Worker side: Codex has no separate "background" flag the worker sets; its shell tool (`exec_command`, unified exec)
returns after a yield time and leaves the process running. A prompt such as "call your shell tool with a very short
yield_time_ms (500) so you do not wait" produced exactly that. The `commandExecution` item has
`source:"unifiedExecStartup"` and a `processId` for ordinary foreground commands too. The turn ended with the process
still running.

```
-> thread/backgroundTerminals/list {"threadId":TID}      (optional "cursor","limit")
<- {"data":[{"itemId":"exec-...","processId":"11288","command":"sh -c 'sleep 40; echo BGDONE; exit 3'","cwd":"...","osPid":null,"cpuPercent":null,"rssKb":null}],"nextCursor":null}
-> thread/backgroundTerminals/terminate {"threadId","processId"}     (schema; not used)
-> thread/backgroundTerminals/clean {"threadId"}                      (schema; not used)
```

* The list shows only running jobs; a finished job disappears. `osPid` was `null` in all runs.
* End of a job (exit 3, 40 s): `item/completed commandExecution {status:"failed", exitCode:3, processId:"11288"}`,
  arriving after the turn had completed, to subscribed connections only. Exit 0 gives `status:"completed"`.
  Two jobs in one turn (exit 0 after 15 s, exit 2 after 25 s) were both listed, then both recorded with the right
  exit codes (checked via `turns/list` after reading with nobody connected).
* **No self wakeup.** Job ended 38 s after the turn ended with A connected and subscribed: nothing but
  `item/completed`. With nobody connected: same, `thread/turns/list` shows the finished jobs inside the old turn
  and the thread `idle`. The model is never told. Orch must send the "job finished" message itself (JOB-5 becomes the
  normal path). The completion item is the trigger to use (`JobEnded` with `exitCode`), and for a missed
  completion read the turn's items.
* **Intermediate or final**: `turn/completed` has no marker. After it (or after `thread/status/changed` idle) call
  `thread/backgroundTerminals/list`: non-empty = intermediate (`waiting for job`), empty = final.
* Jobs killed by a thread unload (see 5) are recorded as `status:"failed"`, `exitCode:-1`, never as a missing item,
  so `lost` can be told from a real exit code by `-1` plus an unload (the exit code of a real signal kill was not
  tested).

## 5. Job lifetime

* Default server: after the last subscribed connection left, `thread/loaded/list` dropped the thread after about 56 to 61 s
  (with `thread_unload_delay_secs=20`: within 15 to 20 s). The running job was
  killed at that moment: process gone, item `failed`, `exitCode:-1`. A job running does **not** keep the thread
  loaded. Reproduced three times (jobs of 241 s, 51 s, 720 s).
* Config key `thread_unload_delay_secs` (seconds; found in the binary strings and accepted by `-c`; "must be a
  non-negative integer", "is too large" above an unknown limit; `100000000000` was accepted at startup). With
  `86400` a 61 s job finished with nobody connected (`completed`, `exitCode:0`, thread still loaded 100 s later).
  The thread then stays loaded; memory cost of keeping it is not measured.
* An alternative is a keeper connection that stays subscribed (`thread/resume`); a 99 s subscribed connection kept a
  241 s job alive, and it died 60 s after that connection closed.
* An unloaded thread is reloaded by `thread/resume` from its rollout file; history is intact but the jobs are gone.
* `turn/interrupt {"threadId","turnId"}` -> `{}`. After interrupting a turn that was waiting on `sleep 60`, the job
  stayed listed and finished normally; the interrupted turn is `interrupted`. A job started in an earlier turn also
  survived.
* 12-minute job: see table; the thread stayed loaded (`loaded=True` at every poll), the list showed the job at every poll until it ended, and no wakeup turn followed.

## 6. Resume, model and effort

```
-> thread/resume {"threadId":"01a21293-0000-7000-8000-000000000000"}  <- error -32600 "no rollout found for thread id ..."
-> thread/resume {"threadId":"not-a-uuid"}  <- error -32600 "invalid session id: invalid character: expected an optional prefix of `urn:uuid:` ..."
-> turn/start {"threadId":TID,"model":"gpt-6-luna","effort":"medium","input":[...]}
notif thread/settings/updated {threadId, threadSettings:{...}}    # sent when the override changes settings
```

After the override `thread/read` showed `model:"gpt-6-luna"`, `reasoningEffort:"medium"`, same id; the next turn
without parameters kept them (sticky), and it still knew the secret word given in turn 1. `thread/resume` response
returns `model` and `reasoningEffort`, so the actual setting can be read after any reconnect. Unknown model in a
turn override is not rejected by `turn/start`; it fails inside the turn (section 8) and the bad model stays on
the thread until a valid override is sent. The server-wide `-c model=...` (a different model) was overridden by the `thread/start` `model` parameter, so the
model can be set per thread. `turn/settings/update` also exists
for a running turn (not used).

## 7. Model list

```
-> model/list {}     (optional "includeHidden","limit","cursor","modelProvider")
<- {"data":[{"id","model","displayName","isDefault","hidden","defaultReasoningEffort","supportedReasoningEfforts":[{"reasoningEffort":"low",...}],"inputModalities","serviceTiers",...}],"nextCursor":null}
```

On this account: `gpt-6.2-sol` (default), `gpt-6.2-astra`, `gpt-6.1-terra`, `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`
with efforts low, medium, high, xhigh, max, ultra; `gpt-6-luna` and `gpt-5.6-luna` with low, medium, high, xhigh, max;
`gpt-5.6-sol` and `gpt-5.6-terra` as the first group. Actual model: `thread/start`/`thread/resume` responses and
`thread/settings/updated`; `model/rerouted` is a notification in the schema (not seen). An `agentMessage` item does not
name its model.

## 8. Errors

Bad model (`turn/start` `model:"nonexistent-model-x"`) answers normally at first, then:

```
thread/status/changed {status:{type:"systemError"}}
error          {error:{message:"The 'nonexistent-model-x' model is not supported when using Codex with a ChatGPT account.","codexErrorInfo":"modelNotFound","additionalDetails":null},threadId,turnId,willRetry:false}
turn/completed {turn:{id,status:"failed",error:{message,codexErrorInfo:"modelNotFound"}}}
```

Bad effort (`"bogus"`) also starts the turn and fails: `error` with an OpenAI `invalid_request_error`
`[reasoning.effort] [invalid_enum_value]` in the message, `turn/completed` `failed`. Compaction with a bad model:
the same `modelNotFound` inside a `contextCompaction` turn. A thread in `systemError` takes the next `turn/start`
normally. Failed turns appear in `thread/turns/list` with `status:"failed"` and only the user item.

`codexErrorInfo` values in the 0.172.0 schema (string unless noted): `contextWindowExceeded`, `sessionBudgetExceeded`,
`usageLimitExceeded`, `rateLimitExceeded`, `flexUnavailable`, `serverOverloaded`, `modelNotFound`,
`policyCapacityUnavailable`, `cyberPolicy`, `bioPolicy`, `misalignmentPolicyViolation`, `tooManyDenials`,
`internalServerError`, `unauthorized`, `badRequest`, `threadRollbackFailed`, `sandboxError`, `other`,
`inferenceHookDenied`; objects `httpConnectionFailed`, `responseStreamConnectionFailed`,
`responseStreamDisconnected`, `responseTooManyFailedAttempts` (each `{httpStatusCode}`),
`activeTurnNotSteerable {turnKind: review|compact}`; any other string or object is allowed. Only `modelNotFound`
(and the effort `badRequest`-like message) was seen live; usage limit, rate limit and context errors were **not
triggered** (not cheap or not safe to trigger). Suggested mapping, unverified: `usageLimitExceeded`,
`sessionBudgetExceeded` -> billing; `rateLimitExceeded`, `serverOverloaded`, `internalServerError`, the four
`response*`/`http*` forms -> transient; `unauthorized` -> billing/missing login; `modelNotFound` -> other (reject
choice); `contextWindowExceeded` -> session problem. `account/rateLimits/updated` notifications carry
`primary`/`secondary` `usedPercent`, `windowDurationMins`, `resetsAt` (readable by `account/rateLimits/read`, not
called). Request-level errors (unknown thread, wrong turn id) are JSON-RPC `error` objects with code -32600.

## 9. Terminal joining

```
cd PROJ && codex resume --remote unix:///ABS/s.sock -c 'model="gpt-6-luna"' <THREAD-ID> "Reply with the single word TUIWORD."
```

Run in a pty (Python `pty.fork`, 120x40) for 25 s while Orch's connection B had called `thread/resume`:

* The TUI attached to the same thread and showed its whole history, then ran the prompt. B received the turn
  (`turn/started`, `userMessage` with a UUID `clientId` set by the TUI, `agentMessage TUIWORD`, `turn/completed`),
  plus `thread/name/updated` (`"Reply with TUIWORD"`: the TUI names the thread).
* The TUI also created a second, empty thread (`thread/started`, `environments: []`) on the server.
* While the TUI was open, B's own `turn/start` was accepted and ran (`ORCHWORD`), and the TUI showed it live (the
  server side there is no lock between the two; whether the TUI displayed the ORCHWORD turn was not captured).
  The TUI was open at that moment.
* The command can include `-c model=...`; other flags of `codex resume` (`--remote-auth-token-env` only for ws) were
  not needed on a unix socket. Interactive typing was not tested. The TUI process was started by the probe and killed
  (SIGTERM); the server and thread kept running.

## 10. `nohup`, `&`, compaction

* `nohup sh -c 'sleep 30; echo late > f' > /dev/null 2>&1 &`, `(sleep 5; echo late > f) &` and
  `setsid sh -c '...' &` (no `setsid` on macOS, so effectively a failed command) were each run through the shell
  tool: `commandExecution` `completed`, `exitCode 0`, the job list stayed **empty**, and the file was never written:
  the process is killed when the command's session ends, in the default workspace-write sandbox (zsh also printed
  `nice(5) failed: operation not permitted` for `nohup`). So `nohup ... &` neither survives nor shows up. Nothing in
  the protocol blocks it. Detection: an `item/completed commandExecution` whose `command` contains `nohup`, `disown`,
  `setsid` or ends with `&`, with an empty job list afterwards. Prevention can only be the prompt, or an exec policy
  rule (not tested; would be a global or project file, not changed here).
* `thread/compact/start {"threadId"}` -> `{}`. Then `thread/status/changed` active, `turn/started`,
  `item/started contextCompaction`, `item/completed contextCompaction` (3 s), `turn/completed completed`. The turn lists
  one `contextCompaction` item and no `agentMessage`. Thread id unchanged, earlier turns still readable, the next turn
  still knew the earlier secret word. No `thread/compacted` notification was seen in this run.

## Differences from docs/redesign.md

1. JOB-4 (CLI wakes its own session when a job ends) is **false for Codex**: nothing wakes the model, neither with a
   client connected nor without. JOB-5 (Orch sends "job finished") is the only path and needs a connection with
   `turn/start`/`thread/queue/add` on the thread. `ORCH-ISSUES.md` marks this "Not tested".
2. REC-9 / 3.2 ("jobs keep running while the server runs, read the thread again"): by default Codex unloads an idle,
   unsubscribed thread after about 60 s and **kills its jobs**. Needs `thread_unload_delay_secs` set high in the
   `-c` of `codex app-server`, or a keeper connection.
3. Events only reach connections that called `thread/resume`; Orch must resume after each reconnect (even the
   connection that issued `turn/start` gets no item events without it).
4. `reconnects` also depends on reading through `thread/turns/list`/`items/list`; no output file is needed because the
   server keeps the history, but `read_pos` should be (turn id, last item id) rather than a byte offset.
5. Exit codes are not in the job list; they come from the completion item. Jobs killed by unload show `-1`.
6. Steer acknowledgement has no message id; match by `clientId` on the `userMessage` item. `turn/start` during a turn
   is already a steer, so `sends_while_writing` needs no extra mode.
7. Version in the ORCH-ISSUES table (0.171.0) is stale.
