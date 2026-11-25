# Probe P2: Qoder CLI

Checked 2026-11-25 on macOS, `qodercli 1.1.97` (`qoder` and `qodercli` are the same binary; init event reports
`"qodercli_version":"1.1.97"`, `"protocol_version":"1.5.0"`). Model for all live tests: `Efficient` (cheapest tier found; about
0.1 to 0.2 credits per tiny turn, `total_cost_usd` is always 0). Every test ran in its own folder under
`/Users/fancy/.claude/jobs/4b93f1df/tmp/probes/p2/<name>/proj`.

"verified" = observed live in this probe. "code" = read from the CLI's embedded JavaScript strings, not observed.

## Summary

| # | Question | Answer | Status |
|---|---|---|---|
| 1 | Message while writing | Queued: taken in only after the running turn's `result`, then runs as its own turn. Not injected into the running turn, also not at a tool boundary. Between turns (only a job running) it is taken in at once. | verified |
| 1 | Receipt echo | Only with `--replay-user-messages`: a `{"type":"user",...,"isReplay":true}` line with the original text (new uuid; the uuid sent in is not kept). It appears when the message is taken in (start of its turn, right after the `system init` of that turn), not when it is written to the pipe. Match by text; there is no id round trip. | verified |
| 2 | Background job events | `system task_started`, `system background_tasks_changed` (full list of running tasks), then at the end `task_updated` (status), `background_tasks_changed`, `task_notification` (status `completed`/`failed`/`stopped`). Exit code only inside the `summary` text. | verified |
| 2 | Failure, several jobs | Failed job: `task_notification.status:"failed"`, summary `... failed with exit code 3`. Three jobs at once: three `task_started`, one wake turn per job, in end order. | verified |
| 2 | Self wakeup | Yes. Each ended job starts its own turn (`init`, synthetic user echo with `<task-notification>`, text, `result`). Works with stdin idle and from a message sent by another process. | verified |
| 2 | End of turn | One `result` line per turn (`subtype:"success"` or `error_during_execution`). No field says "job still running"; intermediate vs final must come from the job list (`background_tasks_changed.tasks` empty or not, or started minus notified). | verified |
| 3 | 12-minute job; can the cap be raised? | Default cap for a background job is 600000 ms (job stopped, status `failed`, summary `timed out after 600000 ms`). The Bash tool's own `timeout` argument raises it (documented: default 600000, max 7200000): a `sleep 780` job with `"timeout":1200000` ended normally (`completed`, output `LIMITOK`), with or without `BASH_*_TIMEOUT_MS` in the environment. The two environment variables alone did nothing for background jobs (7200000 each: still stopped at 600 s). | verified |
| 4 | Launcher exits | Agent, job and wake all continue; a new process wrote to the FIFO and read the output file from a saved offset. Killing only the CLI: SIGTERM kills the job (status `killed`), SIGKILL leaves the job running as an orphan with no record. | verified |
| 5 | Unknown session id | No JSON. Stderr text `Error resuming session: Invalid session identifier "<id>".`, exit code 42, process ends before reading stdin. Same error for a real id from another cwd (sessions are per cwd). | verified |
| 5 | Model switch in one session | Same session id, history kept, both ways: at resume (`--resume <id> --model Fast`) and live (send `/model <name>` as a user message). Live switch writes the model into the global `~/.qoder/settings.json`. | verified |
| 6 | Effort levels | Fixed global list in the error text of `--reasoning-effort`; no per-model list in `--list-models`, help or config. Unsupported combos are not rejected. | verified |
| 7 | Actual model | `assistant.message.model` and `message_start.message.model` carry the lowercase tier name (`efficient`, `fast`, `auto`); `init.model` carries what was asked (`Efficient`). Underlying vendor model is not exposed. | verified |
| 7 | "Started answering" | `system init` + replay echo = taken in; `stream_event message_start` (first model output); first `content_block_delta` `text_delta` = visible text. | verified |
| 8 | Prevent `nohup ... &` | `--disallowed-tools 'Bash(nohup:*)'` denies it, also inside `a && nohup ...`. Without it the command runs, leaves no job record and an orphan process. | verified |
| 9 | API error with a job | Reproduced with a local proxy that refused connections: 4 `system api_retry`, then a synthetic assistant text and `result error_during_execution`. The job kept running, the CLI recovered when the proxy came back, the job woke the agent. | verified |
| 9 | Invalid model | Not an error: falls back to `auto` with a printed notice (stream not distinguished). | verified |
| 10 | Interrupt without killing | Not possible in stream-json mode: the `interrupt` control frame is silently dropped. SIGINT aborts the turn and ends the process (job fate on SIGINT not tested; SIGTERM kills jobs). | verified |
| 10 | Compaction | `/compact [instructions]` as a user message works: `status compacting`, `compact_boundary`, `result` with `num_turns:0`. Same session id. | verified |
| 10 | Terminal joins the open session | No attach. A second process can `--resume` the same session id while the first is open (no lock error), but its turn is not visible in the first process's output and both append to the same history file. | verified |

## Commands

Open CLI (what the adapter would run):

```
mkfifo in.fifo
# a process opens the FIFO read-write and passes it as stdin, then may exit; stdout and stderr go to files
qodercli -p --input-format stream-json --output-format stream-json --include-partial-messages \
  --replay-user-messages --model Efficient --permission-mode bypass_permissions \
  [--resume <session_id>] [--disallowed-tools 'Bash(nohup:*)']   < in.fifo > out.jsonl 2> err.txt
```

`--replay-user-messages` needs both stream-json flags; `--include-partial-messages` needs `-p`. The launcher (a Python
`subprocess.Popen(..., start_new_session=True)` with an `O_RDWR` fd on the FIFO) exited right after start and the CLI went on.
A message is one line:

```
{"type":"user","message":{"role":"user","content":"text"}}
```

The first session id appears in the first `system init`, only after the first message is read (no output at all before it).
`--session-id <uuid>` chooses it up front (used by Orch's `QoderBackend` today); not needed with the open mode.
`--resume` only finds sessions of the same cwd (`~/.qoder/projects/<cwd-with-dashes>/<id>.jsonl`).

stdin EOF is also safe: with `qodercli ... < msg.json` the CLI finished the turn, waited for the running job, ran the wake
turn and exited 0. With no job it exits after the turn.

## Events (shortened)

Per turn, in order:

```
{"type":"system","subtype":"init","model":"Efficient","session_id":"396b9e2e-...","qodercli_version":"1.1.97","permissionMode":"bypassPermissions",...}
{"type":"system","subtype":"artifacts_update","artifacts":[],...}
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"say hi"}]},"session_id":"...","uuid":"11af1d80-...","isReplay":true}   # only with --replay-user-messages
{"type":"system","subtype":"hook_diagnostics",...}
{"type":"stream_event","event":{"type":"message_start","message":{"model":"efficient",...}}}
{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"thinking",...}}}
{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"ok"}}}
{"type":"assistant","message":{"model":"efficient","content":[{"type":"text","text":"ok"}],...}}
{"type":"stream_event","event":{"type":"message_delta","delta":{"stop_reason":"end_turn"}}}
{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"ok","stop_reason":"end_turn","session_id":"...","total_credits":0.2057,"usage":{...zeros...},"modelUsage":{"efficient":{...,"credits":0.2057}},"usage":{...,"context_usage_ratio":0.12}}
```

Notes: token counts in `usage` are 0; use `total_credits` and `context_usage_ratio`. The `init` line is repeated at the start of every
turn (including wake turns), so it is a "turn taken in" marker. Tool calls are `assistant` content blocks of type `tool_use`, results are
`user` lines with `tool_result` (these are not replay echoes; they have no `isReplay`).

### Message while writing (Q1)

Commands: send `Run bash: sleep 12; echo S1 ...`, six seconds later send `[m2] also say PING`. Observed order in `out.jsonl`:

```
init, user(echo m1), message_start ... tool_use(Bash) ..., user(tool_result "S1"), assistant text "S1 done.", result(num_turns:2)
init, user(echo "[m2] also say PING ..." isReplay), message_start ..., assistant text "S1 done. PING", result(num_turns:1)
```

The second message was written to the FIFO during the tool call; nothing in the stream acknowledged it until the next turn's
`init` + echo, which came right after the first `result`. The same happened with a pure text turn (no tools). So `sends_while_writing` is
true at the pipe level (nothing is lost, order kept) but the CLI runs it as a queued next turn; Orch cannot see a receipt before the
turn ends. Control frames `cancel_async_message` (cancel a queued message by `message_uuid`) exist in code but headless mode drops
control frames (see below), so queued messages cannot be withdrawn.

With only a job running (no turn), a message is taken in at once: `init`, echo, `result "Four"` came while `sleep 40` ran, and the job's
wake still followed.

### Background jobs (Q2)

The model starts a job with the Bash tool's `run_in_background:true`. A foreground Bash call that runs long is also moved to the
background automatically ("Command was automatically moved to background (ID: ...)", seen after a few seconds for `sleep 25`) and then
appears in the same job record (`task_type:"local_bash"`), so a turn cannot end while a foreground command is running; it ends with the
job listed instead.

```
{"type":"system","subtype":"task_started","task_id":"b9mz8nbkj","tool_use_id":"call_54932f...","description":"Background job B: sleep 14, echo JB, exit 3","task_type":"local_bash"}
{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"b9kd5doog","task_type":"local_bash","description":"..."},{"task_id":"bdrp2hala",...}]}
{"type":"user","message":{"content":[{"type":"tool_result","content":"Command running in background with ID: b9kd5doog. Output is being written to: /var/folders/.../tasks/b9kd5doog.output ..."}]}}
... result "started" (turn 1 ends with 3 jobs running) ...
# job ends:
{"type":"system","subtype":"task_updated","task_id":"b9mz8nbkj","patch":{"status":"failed","end_time":1795607936431}}
{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"bdrp2hala",...}]}
{"type":"system","subtype":"init",...}                                  # wake turn
{"type":"user","message":{"content":[{"type":"text","text":"<task-notification>...<status>failed</status><summary>Background command \"...\" failed with exit code 3</summary></task-notification>"}]},"isReplay":true,"isSynthetic":true}
{"type":"system","subtype":"task_notification","task_id":"b9mz8nbkj","status":"failed","output_file":".../tasks/b9mz8nbkj.output","summary":"Background command \"...\" failed with exit code 3"}
assistant "JB finished, exit code 3." ; result success
```

Statuses seen: `completed` (exit 0), `failed` (non-zero, exit code only in `summary`), `stopped` (job killed by CLI shutdown; `task_updated.patch.status` was `killed`).
The output file of a job is `$TMPDIR/qoder-cli-<uid>/<cwd-with-dashes>/<session_id>/tasks/<task_id>.output`, readable with a plain file read.

Several jobs: the three jobs (8 s, 14 s exit 3, 20 s) each produced a separate wake turn, in finishing order, and
`background_tasks_changed.tasks` shrank 3, 2, 1, 0. Wake turns are `isSynthetic:true` in the echo, so they can be told from user messages.

Job ending while a turn is running (w1: `sleep 8` job, 700-line writing turn): `task_updated` and `background_tasks_changed tasks:[]`
appear in the middle of the turn, the turn ends with its own `result`, and only then the wake turn follows (`init`, echo, `task_notification`,
text, `result`). So at that `result` the record is already empty (a "final" by JOB-2) yet a wake turn is about to start.

`ScheduleWakeup`, `CronCreate`, `Monitor`, `TaskStop` appear in the init tool list. Not tested.

### Limits for long jobs (Q3)

Test: `Start one background Bash job (run_in_background=true) with no timeout set: sleep 720; echo J12` in an open CLI, nothing else sent.
Result at +600 s exactly:

```
{"type":"system","subtype":"task_updated","task_id":"b5e8qw72g","patch":{"status":"failed","end_time":1795609175212}}
{"type":"system","subtype":"task_notification","task_id":"b5e8qw72g","status":"failed","summary":"Background command \"Sleep 12 minutes then print J12\" timed out after 600000 ms. The command was stopped."}
```

So the documented-in-code limit is a hard 10-minute cap (the code reads `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS`; strings also say a foreground command that exceeds its timeout is "moved to background"). The CLI killed the process; the wake turn followed
and the agent saw an empty output file. A control run with both variables at 15000 ms and a 40 s job: job completed normally, so the variables did not apply to background jobs in this build. Whether the model's own `timeout` argument can raise the cap was not tested.
#### Follow-up: raising the cap (three open sessions at the same time, job `sleep 780; echo LIMITOK; exit 0`, started 20:22)

| Run | Environment | Tool input sent by the model | Result |
|---|---|---|---|
| a | `BASH_DEFAULT_TIMEOUT_MS=7200000 BASH_MAX_TIMEOUT_MS=7200000` | `{"command":"sleep 780; echo LIMITOK; exit 0","description":"...","run_in_background":true}` (no timeout) | Stopped at 600 s (20:22:13 to 20:32:13): `task_updated.patch.status:"failed"`, summary `Background command "..." timed out after 600000 ms. The command was stopped.` No `LIMITOK`. |
| b | same as a | `{"command":"sleep 780; echo LIMITOK; exit 0","description":"Sleep 13 minutes then print LIMITOK","run_in_background":true,"timeout":1200000}` | Ended normally after about 780 s (`end_time` 20:35:14): status `completed`, output file `LIMITOK`. |
| c | none | `{"command":"sleep 780; echo LIMITOK; exit 0","description":"...","timeout":1200000,"run_in_background":true}` | Same as b: `completed` at 20:35:14, output `LIMITOK`. |

Documentation found in the CLI (embedded code, quoted):

- Bash tool description given to the model: "For a non-PTY command started in the background, a positive `timeout` sets the time limit in milliseconds (default ${S1}, max ${D1}). A command moved to the background after starting in the foreground gets the default limit from that point. Bash attempts to stop the command at the limit and reports the outcome in a task notification." with constants `Ek=120000` (foreground default), `_k=600000` (foreground max), `S1=600000` (background default), `D1=7200000` (background max, 2 hours).
- `timeout` parameter schema: "Time limit in milliseconds for non-PTY Bash (foreground default 120000, max 600000). A command started in the background uses a positive timeout (default 600000, max 7200000)."
- `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` are read only by the foreground limit function (they replace `Ek` and `_k`; the cap is `max(env max, default)`); the background limit is `min(timeout, D1)` if a positive `timeout` was given, else `S1`. This matches run a. `--help` has no timeout or background option.
- A foreground command that exceeds the cap or runs long is moved to the background and then gets the default 600 s limit from that point (so a long foreground command also dies at 10 minutes unless it was started in the background with a `timeout`).

So the adapter must make the worker pass `timeout` (up to 7200000 ms, i.e. 2 hours) in every long background Bash call; the prompt should say so, and Orch can check the `tool_use` input for `run_in_background:true` with a `timeout` large enough. Longer than 2 hours needs chaining.

Adapter consequence (default settings): a worker needing more than 10 minutes must restart or chain jobs; Orch should treat `failed` with `timed out after 600000 ms` as a timeout, not a crash (JOB-7).

### Launcher gone, new process, kill (Q4)

- The launcher process exited after `Popen`; the CLI (own session, parent pid 1) kept reading the FIFO. Each later message came from a new short-lived
  process (`send.sh`: open FIFO `O_RDWR`, write one line, close) and was answered. Output was read with `python3 sum.py out.jsonl <offset>`
  where the offset is the byte size of `out.jsonl` before sending; the file is append-only JSON lines.
- A message sent from a new process while a job ran was answered at once and the job still woke the agent afterwards.
- Job process tree: `qodercli` -> `/bin/zsh -c ... eval 'sleep 133; echo JK'` (own process group) -> `sleep`.
- `kill -TERM <cli pid>`: the CLI killed the job first and emitted `task_updated killed`, `task_notification status:"stopped"`, a wake turn that was aborted, and
  `{"type":"result","subtype":"error_during_execution","errors":["Operation aborted"],"terminal_reason":"aborted_streaming","origin":{"kind":"task-notification"}}`, then exited.
- `kill -KILL <cli pid>`: the `zsh` and `sleep` stayed alive with ppid 1 (verified, then killed by me). Nothing records them any more.
- `--resume` after SIGKILL: same session id, history kept. The agent had not been told the job was lost; it read the empty output file and said the job was "still
  running". The job is not restarted. So a lost job must be found by Orch (pid/output file), not by the CLI.

### Resume, errors, model switch (Q5)

```
$ qodercli -p --output-format stream-json --resume 00000000-0000-4000-8000-000000000000 "hi"
Error resuming session: Invalid session identifier "00000000-0000-4000-8000-000000000000".
  Searched current project and same-repo worktrees: ...
  Use --list-sessions to see available sessions.
exit 42      (all on stderr, stdout empty)
```

In open mode (`--input-format stream-json`) the same text and exit code come before any message is read. A valid id from another cwd gives the same error.

Switch at resume (`--resume 368d4b0c-... --model Fast`): `init.model:"Fast"`, `init.session_id` unchanged, the model answered a question about
the earlier turns (history kept), `message_start.model:"fast"`.

Switch live: a user message `/model Efficient` produced the non-JSON stdout line `Model set to Efficient (persisted)`, a `result` with
`num_turns:0`, and the next turn's `init.model` and `message.model` were `efficient` with the same `session_id`. Caution: "persisted" means it wrote
`{"model":{"name":"efficient"}}` into the user's global `~/.qoder/settings.json` (see Side effects). A control frame
`{"type":"control_request","request_id":"r1","request":{"subtype":"set_model","model":"Efficient"}}` (the SDK form, in code with optional `reasoningEffort`) got no response and no effect.
`--model` on the command line is documented as "for the current session" and was not seen to persist (not tested against the settings file).

### Effort levels (Q6)

```
$ qodercli --reasoning-effort bogus -p hi
Error: Invalid reasoning effort: bogus. Valid values are: auto, none, low, medium, high, xhigh, max, ultracode.
```

- `qodercli --list-models` prints only names (15 lines: Auto, Ultimate, Fast, Efficient, Sonus, Qwen4-Max, Qwen3.8-Max, Qwen3.8-Flash, Kimi-K3.1-Pro, Kimi-K2.8-Preview, GLM-5.3,
  GLM-5.3-Flash, DeepSeek-V4-Pro, DeepSeek-Flash, MiniMax-M3.1-Flash); `-o json` does nothing there. `--help` gives no levels.
- The CLI code has a second list in another path: "Valid efforts: disabled, off, none, low, medium, high, xhigh, max" (code), and per-model fields `reasoning_efforts` /
  `supportedEfforts` from the server call `/algo/api/v2/model/list` (code). The headless CLI does not print them. So levels are per model on the server side but only the fixed list is visible from the command line.
- `--reasoning-effort low` and `ultracode` with `Efficient` were both accepted (answer `ok`), so an unsupported level is not an error (no way to tell if it was applied).
- Config: `~/.qoder/settings.json` has `model.name` and (code) `model.reasoningEffort` and `model.preferences.<model>.reasoning.effort`; not edited by me.

### Model and "started answering" (Q7)

`init.model` echoes the request (`Efficient`, after a `/model` switch `efficient`). The model reported by the API is in `message_start.message.model`
and `assistant.message.model` (`efficient`, `fast`). For an unknown name: a line (output was merged, stdout or stderr unknown) `Model "NoSuchModel" is not available right now; using "auto" instead. Use /model to choose another model.` and
`message.model:"auto"` (so MOD-5 "show the model the CLI reports" works, and this fallback is a silent substitution Orch should catch by comparing). Real vendor models behind the tier names are not shown.
A failed turn has a synthetic assistant message with `"model":"<synthetic>"`.

Proofs of "started answering" (in order): `system init` (turn begins), replay echo `user` line (message taken in), `stream_event message_start` (model started), `text_delta` (visible
text). With `--include-partial-messages` off, only whole `assistant` lines arrive.

### nohup (Q8)

Without a rule: `nohup sleep 40 > /dev/null 2>&1 & echo launched` in the foreground Bash tool returned `launched`, no `task_started` was emitted, and an orphan `sleep 40` ran (verified).
Detection therefore means: a Bash `tool_use` input containing `nohup`, `setsid`, `disown` or a trailing `&` while no `task_started` follows for that `tool_use_id`.
Prevention with a flag:

```
--disallowed-tools 'Bash(nohup:*)'
tool_result is_error:true  "Error: Command denied by cliArg rule: nohup sleep 30 > /dev/null 2>&1 & echo launched"
tool_result is_error:true  "Error: Command denied by cliArg rule: echo pre && nohup sleep 31 > /dev/null 2>&1 & echo launched"
```

Both were denied and no process started. Other spellings (`setsid`, `disown`, `( cmd & )`) not tested.

### API error with a job running (Q9)

Qoder honors `HTTPS_PROXY`/`HTTP_PROXY` for model traffic. A small CONNECT proxy (own process, port 18991) was started, a CLI was opened through it, a 100 s job started, the proxy was killed,
a message was sent:

```
{"type":"system","subtype":"api_retry","attempt":1,"max_retries":4,"retry_delay_ms":507,"error_status":null,"error":"unknown"}   # attempts 1-4, delays 507, 1163, 2468, 4360 ms
{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"ECONNREFUSED: Unable to connect. Is the computer able to access the url?"}]}}
{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["ECONNREFUSED: Unable to connect. Is the computer able to access the url?"],"terminal_reason":null,"api_error_status":null}
```

The job kept running. With the proxy restarted, the next message worked in the same process and the job's wake turn followed normally (job ended while the proxy was up; the earlier failed turn did not lose it).
The CLI retries by itself (`--max-model-request-retries`, 4 retries by default) before failing the turn, so Orch's own retry count starts after these.
Not reproducible without real billing/rate limits: billing, rate limit, auth, overload. From the CLI's code (not observed) the error classes it recognizes are
`model_not_found`, `rate_limit` (429, "rate limit"), `authentication_failed` (401/403), `billing_error` (402), `max_output_tokens`, plus `server_error`/`overloaded_error`/`internal_server_error`/`service_unavailable`/`gateway_timeout`;
result subtypes in the schema: `error_during_execution`, `error_max_turns`, `error_max_budget_usd`, `error_max_structured_output_retries`; `result` also has an optional `error_message`. The adapter should classify by `errors[]`/assistant text, since `api_error_status` was null here.

### Interrupt, compaction, terminal (Q10)

- Interrupt: `{"type":"control_request","request_id":"c1","request":{"subtype":"interrupt"}}` written during a 900-line writing turn: no response and the turn went on to the end (twice).
  The code explains it: headless logs `[headless] dropped non-permission control frame`; only permission responses are read from stdin.
  `kill -INT <pid>` during writing: the turn ended with `result error_during_execution` and the process exited (assistant text "Model response interrupted. Completed output is preserved...").
  Stopping a turn therefore ends the process; `--resume` brings the session back. With a running job, `kill -TERM` killed the job too; SIGINT with a job was not tested.
- Compaction: `/compact keep it short` as a user message in a resumed session:
  ```
  {"type":"system","subtype":"status","status":"compacting"} ... {"status":null}
  {"type":"system","subtype":"compact_boundary","compact_metadata":{"trigger":"manual","pre_tokens":29895,"post_tokens":692,"messages_summarized":8,"duration_ms":10712,...}}
  {"type":"result","subtype":"success","result":"","num_turns":0}
  ```
  Automatic compaction was not triggered (context too small).
- Terminal join: a second `qodercli -p --resume <id>` on a session whose first process was open ran without any error or lock and answered (`second`); the first process's output stream showed nothing of it.
  There is no attach command for local sessions (`--remote-session` and `--remote-control` are for Qoder cloud remote sessions, not tested). Joining a terminal would mean a second CLI on the same session (against REC-8) with
  forked history, so Orch has to stop the headless process first (which also kills jobs) and resume in the terminal.

## Differences from Claude's stream (from `orch/backends.py`, not a live comparison)

`QoderBackend` already inherits Claude's parser, and the lines above match the same envelope (`system`, `stream_event`, `assistant`, `user`, `result`). Differences seen:
`init` repeats every turn (not once per process), `usage` tokens are zeros and cost is in `total_credits`, `init.model` is the display name while the API model is lower case, a non-JSON line for `/model`
(`Model set to ...`) and for the invalid-model fallback, resume errors are plain text with exit 42 (no JSON), control frames other than permission replies are dropped, and the job exit code is only in `summary` text.
Option names differ as in `QoderBackend.build_command` (`--permission-mode bypass_permissions`, `--reasoning-effort`, `--list-models`).

## Side effects and cleanup

- Sending `/model Efficient` (Q5, live switch) changed the user's global `~/.qoder/settings.json` key `model` to `{"name":"efficient"}` (file mtime 20:04). I did not know the earlier value and did not change it back. The other keys (`enabledPlugins`, `security`, `permissions`) were not touched by me. An adapter must not use `/model`
  for live switching unless that persistence is acceptable; `--model` at resume is the safe path.
- Status at the end of the follow-up: `~/.qoder/settings.json` was not read or changed again by me; the `model.name` value written by `/model` is still there and the user decides about it. No `/model` or other config-writing command was sent in the follow-up.
- Qoder also wrote its usual session files under `~/.qoder/projects/...` for each probe folder.
- All processes started by this probe were recorded by pid and stopped at the end.

## Recommended capability values

| Capability | Value | Why |
|---|---|---|
| `sends_while_writing` | `true` at the pipe level, but "taken in" only at the next turn; set `receipt = "echo_after_turn"` (use `--replay-user-messages`, match by text) | The line is accepted and ordered; the CLI starts it after the current `result`. The echo marks "delivered" at turn start; `answered` = first `message_start` of that turn. MSG-5's 5-minute check must allow for a long running turn first. |
| `reconnects` | `true` | FIFO read-write plus output file with a saved offset worked from new processes; agent and job survive the launcher. The CLI itself must stay alive (SIGKILL/SIGTERM of the CLI ends the job record). |
| `job_record` | `true` (stream events `task_started`, `background_tasks_changed`, `task_updated`, `task_notification`) | Exit code only in `summary` text; failed = `status:"failed"`. Orphans (`nohup`) have no record: block with `--disallowed-tools 'Bash(nohup:*)'`. |
| `shares_terminal` | `false` | No attach; a second process on the same session is unlocked and invisible to the first. |
| `compacts` | `true` via the user message `/compact [instructions]` | `compact_boundary` event confirms. |
| `interrupt` | `false` without ending the process | Control frame ignored in headless mode. |
| `live_model_switch` | prefer restart with `--resume <id> --model <name>` (same session id, history kept); `/model` works live but writes global config | |
| `effort_catalog` | fixed list `none, low, medium, high, xhigh, max` (plus `auto`, `ultracode` accepted); per-model support not listed, treat as "unknown, not an error" | |
