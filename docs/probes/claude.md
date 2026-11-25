# Probe P1: Claude Code CLI in stream-json mode

Version tested: `claude` 2.1.332 (Claude Code), macOS, 2026-11-25. Model: `haiku` (resolves to `claude-haiku-5-5`), `--effort low`. One short check of a model change used `sonnet` (`claude-sonnet-5-5`).
Everything below was observed live unless a row says "not verified". Event lines are shortened: ids cut to `xxxx..yyyy`, long text cut with `...`, paths shortened, noisy events (`thinking_tokens`, `rate_limit_event`, `stream_event`, and `system/init` unless relevant) dropped. Scripts and raw recordings were kept outside the repository.

## Summary

| # | Question | Answer | Status |
|---|---|---|---|
| 1a | Message sent while the agent writes: taken in during the turn or after? | Both, depending on where the agent is. While a tool runs: `command_lifecycle queued` at once, the text enters the same turn at the next tool-result boundary and the agent used it in that turn's final reply. While the model streams plain text: `queued` at once, but it starts as a separate turn after the current `result` | verified |
| 1b | How does it show? Does `--replay-user-messages` echo receipt? Can the id be matched? | Yes. Put a `uuid` in the input line. `command_lifecycle` events carry it as `command_uuid` (`queued` immediately, `started`, `completed`/`cancelled`), and the echoed `{"type":"user","isReplay":true,"uuid":<same>}` appears when the text is actually taken into the conversation | verified |
| 2a | Background job events | `system/task_started` (+ `background_tasks_changed` listing running task ids) at start. At end `task_updated` (status), `task_notification` (status `completed`/`failed`/`stopped`, `summary` text containing `exit code N`, `output_file`), `background_tasks_changed` (shorter list). Exit code exists only inside `summary` text | verified |
| 2b | Failure and several jobs | Non-zero exit gives status `failed` and summary `failed with exit code 3`. Three jobs at different times gave three separate wake turns. Two jobs ending in the same second gave one wake turn for both. A job that ends while a turn is running is delivered inside that turn (no extra wake turn) | verified |
| 2c | Does the session wake itself? | Yes, every time the job ends while the session is idle, with no input. Wake turns have no `command_lifecycle` and the notification text is not echoed; the turn starts with `system/init` | verified |
| 2d | What marks the end of a turn? | `result` event (one per turn), followed by `command_lifecycle completed` when the turn was started by a message. Intermediate or final is not in the event: read the job list (last `background_tasks_changed`) when `result` arrives. Gotchas: a spurious empty `result` (`num_turns:0`, `result:""`, `stop_reason:null`) appears around wake and `/compact`; an error turn has `subtype:"success"` with `is_error:true` | verified |
| 3a | 12 minute job | Job `sleep 720` ended after 12:00, notification arrived, session woke itself. Longer than this was not tested | verified (12 min); longer not tested |
| 3b | Documented time limit | Unattended sessions (`-p`, SDK, CI) limit background commands: default 30 min (10 min for `-p` with text prompt, not stream-json), `timeout` parameter allowed, maximum 2 h. Raisable with `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS`. Enforced: a job started with `timeout:5000` ended with `task_updated status:"killed"`, notification status `stopped`, summary "...stopped after reaching its background time limit" | documented + limit mechanism verified |
| 4a | Launcher exits, agent and job continue; new process sends and reads from a saved position | Yes. The CLI holds stdin itself (fifo opened read-write, then the starting shell exited). Another process wrote to the fifo while the job ran; the agent answered and still woke when the job ended. The output file was read from a saved byte offset | verified |
| 4b | Does killing only the CLI kill the job? | SIGTERM: yes, the CLI stops the job first (`task_notification status:"stopped"`). SIGKILL: no, the job's process keeps running as an orphan, unrecorded and nobody to wake | verified |
| 5a | `--resume` with unknown or lost session id | Process exits at once (no message needed), stderr `No conversation found with session ID: <id>`, and stdout gets one `result` with `subtype:"error_during_execution"`, `is_error:true`, `errors:["No conversation found with session ID: <id>"]` | verified |
| 5b | Changed model on `--resume` | Session and session id are kept; init and the assistant event report the new model; history intact. Resuming from a different working folder also worked | verified |
| 6 | Actual model reported? What proves "started answering"? | Yes: `system/init.model` and `assistant.message.model`; `result.modelUsage` lists every model used, including side calls (haiku shows up even on a sonnet session). Evidence of start: `command_lifecycle started` (turn picked up the message), echoed user event (text is in the context), `system/status requesting` (only seen with `--include-partial-messages`), `stream_event message_start` (partial mode only), first `assistant` event | verified |
| 7 | Prevent or detect `nohup ... &` | `--disallowedTools "Bash(nohup *)" "Bash(setsid *)" "Bash(*&)" "Bash(* & *)"` denied all three tested forms and emits `system/permission_denied`. A `PreToolUse` hook (passed with `--settings <file>`, no global change) denies by regex and denied `setsid ... &` and `(sleep 30 &) ; echo x` (run with an earlier, looser version of the regex below; the final regex was only unit-tested). Without either, the nohup job runs and is missing from the record (no `task_*` event, no wake) | verified |
| 8a | Does a job survive an API error? | Yes. Local mock endpoint returning 429 during a job: job kept running, its end was still reported, the wake turn then also hit the error. Retried with the existing process afterwards: recovered, same session | verified (mock endpoint) |
| 8b | Error text per class | `assistant.error` field is the discriminator, see table below. Billing, rate limit, overloaded, 500, auth, unknown model and prompt-too-long all distinguishable. No real billing or rate-limit error was provoked | verified (mock endpoint) |
| 9a | Interrupt a turn without killing a job | stdin line `{"type":"control_request","request_id":"..","request":{"subtype":"interrupt"}}` gives `control_response success`, stops the running foreground tool, ends the turn with `result error_during_execution` (`terminal_reason:"aborted_tools"`) and `command_lifecycle cancelled`. A background job kept running and woke the session when it ended | verified |
| 9b | Compact in this mode | Send the text `/compact` as a user message: `status compacting` then `compact_boundary`, `result` (empty), same session id. Tokens went 23709 to 7068 | verified |
| 9c | Model change without restart | `control_request` `set_model` works in the same process (`control_response` with the model; unknown model is rejected immediately with `error_code:"not_found"`, so it cannot be used to cause an API error) | verified |

## Recommended adapter capabilities

| capability | value | reason |
|---|---|---|
| `sends_while_writing` | `True` | Accepted at any time, receipt visible at once (`queued`). Semantics: inside the turn at the next tool boundary, otherwise as the next turn |
| `reconnects` | `True` | Process keeps running without Orch; stdin fifo and stdout file persist. Needs pid + output file + byte offset saved. Not possible after the CLI process died (a resumed process is a new CLI) |
| `job_record` | `True` | `background_tasks_changed` gives the live list; `task_notification` gives end status. `nohup`-style jobs are invisible, so they must be blocked |
| `shares_terminal` | `False` | This process has no terminal UI. Opening `claude --resume <id>` in a terminal would be a second CLI on an open session (REC-8). `claude attach` for background sessions exists in `--help` and was not tested |
| `compacts` | `True` | `/compact` as a user message |

## Surprises relative to docs/redesign.md

1. `command_lifecycle` events are not in the plan: they are the cleanest `Received(message_id)` signal and exist only because the input line carries a `uuid`. `--replay-user-messages` gives a second signal, later (when the text enters the conversation, not when it is accepted).
2. A message sent during a long tool call enters the same turn only at the tool boundary; sent during plain text streaming it waits for the end of the turn. MSG-1 "at once" holds for receipt, not for the model seeing it.
3. The wake turn after a job has no `TurnStarted` marker of its own other than `system/init`; the engine must derive `Trigger.job` from the preceding `task_notification`. If a job ends while a turn is running there is no wake turn at all (JOB-5 fallback must not fire then).
4. A job's exit code is only in `task_notification.summary` text (`exit code N`); `status` is `completed`/`failed`/`stopped` and `task_updated.patch.status` adds `killed`. `ended` and `lost` are not events.
5. Turn end has no intermediate/final marker (JOB-2 is the engine's job), and there are spurious empty `result` events (`num_turns:0`).
6. Error turns are `result.subtype:"success"` with `is_error:true`; check `is_error` and `terminal_reason`. The 5 minute MSG-5 check can use the `queued` line instead of "answered".
7. Killing the CLI with SIGTERM stops the job (REC-9 "stop agents" semantics are fine). With SIGKILL the job lives on with no owner; Orch's `lost` job handling (JOB-7) has to cover this.
8. `system/init` is not printed at process start; it appears only with the first turn (and again at every turn). An idle process emits nothing, so liveness must come from the pid. A bad `--resume` fails at startup, before any message.
9. Unattended background jobs have a built-in time limit (30 min default in stream-json mode, max 2 h) unless `BASH_DEFAULT_TIMEOUT_MS` is raised; "hours-long jobs" need that variable. Not tested beyond 12 min.
10. The CLI keeps writing session transcripts to the user's normal `~/.claude/projects/` (one folder per probe working folder). Using a separate `CLAUDE_CONFIG_DIR` per agent avoids touching the user's config but then `--resume` of an old session cannot find it.

## Details

### Command used to start

```
mkfifo IN.fifo
exec 3<>IN.fifo                       # holder, so the pipe never sees EOF
claude -p --input-format stream-json --output-format stream-json --verbose \
  --replay-user-messages --model haiku --effort low --allowedTools Bash \
  [--include-partial-messages] [--resume <id>] [--settings <file>] [--disallowedTools ...] \
  <&3 >>OUT.jsonl 2>>ERR.txt 3>&- &
```

The starting shell then exited and the CLI stayed up (the CLI's own fd 0 is the read-write fifo). `--permission-mode bypassPermissions` was refused in this environment by the permission classifier, so it was not tested; `--allowedTools Bash` was enough (init shows `permissionMode:"auto"`).

Input lines (one JSON object per line):

```
{"type":"user","message":{"role":"user","content":"text"},"uuid":"<our id, a UUID>"}
{"type":"control_request","request_id":"r1","request":{"subtype":"interrupt"}}
{"type":"control_request","request_id":"r2","request":{"subtype":"set_model","model":"sonnet"}}
```

All probes used UUID-shaped values for `uuid`. Without it no id can be matched (not tested what the CLI generates).

### 1. Message while writing

Sample A: message 2 sent 10 s into a foreground tool call (turn from message 1). Both lifecycles `queued` are immediate; message 2 is echoed and started only when the tool result is delivered, and the single `result` answers both. Note `completed` for message 2 comes before `completed` for message 1.

```
{"type":"command_lifecycle","state":"queued","command_uuid":"bbbb..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"bbbb..0001"}
{"type":"user","content":"Use Bash (foreground, no background) to run exactly: python3 -c 'import time; time.sleep(2...","uuid":"bbbb..0001","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"python3 -c 'import time; time.sleep(25)'\", \"description\": \"Sleep for 25 seconds\", \"run_in_backgro..."}]}
{"type":"system","subtype":"task_started","task_id":"bjso315kf","description":"Sleep for 25 seconds","is_backgrounded":"False","task_type":"local_bash"}
{"type":"command_lifecycle","state":"queued","command_uuid":"bbbb..0002"}
{"type":"system","subtype":"task_notification","task_id":"bjso315kf","status":"completed","summary":"Sleep for 25 seconds","output_file":""}
{"type":"user","content":[{"type":"tool_result","content":"(Bash completed with no output)","is_error":"False"}],"uuid":"5484..a28d"}
{"type":"user","content":"Also, in your final reply include the word BANANA.","uuid":"bbbb..0002","isReplay":true}
{"type":"command_lifecycle","state":"started","command_uuid":"bbbb..0002"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"FIRST-DONE\n\nBANANA"}]}
{"type":"command_lifecycle","state":"completed","command_uuid":"bbbb..0002"}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":2,"api_error_status":null,"result":"FIRST-DONE\n\nBANANA"}
{"type":"command_lifecycle","state":"completed","command_uuid":"bbbb..0001"}
```

Sample B: message 2 sent 4 s into a long plain-text answer (`--include-partial-messages`, assistant events omitted). `queued` is immediate, the echo and `started` come only after `result` of turn 1, and turn 2 is a new turn with its own `system/init`.

```
{"type":"command_lifecycle","state":"queued","command_uuid":"dddd..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"dddd..0001"}
{"type":"system","subtype":"status","status":"requesting","user_message_uuids":["dddddddd-0000-4000-8000-000000000001"]}
{"type":"user","content":"Write the numbers 1 to 4000, one per line, as plain text, nothing else.","uuid":"dddd..0001","isReplay":true}
{"type":"command_lifecycle","state":"queued","command_uuid":"dddd..0002"}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n21\n22\n23\n24\n25\n26\n2..."}
{"type":"command_lifecycle","state":"completed","command_uuid":"dddd..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"dddd..0002"}
{"type":"system","subtype":"status","status":"requesting","user_message_uuids":["dddddddd-0000-4000-8000-000000000002"]}
{"type":"user","content":"Now also say WORD at the very end.","uuid":"dddd..0002","isReplay":true}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n21\n22\n23\n24\n25\n26\n2..."}
{"type":"command_lifecycle","state":"completed","command_uuid":"dddd..0002"}
```

With partial messages the stream also has `system/status {"status":"requesting","user_message_uuids":[...]}` at turn start and `stream_event` lines (`message_start`, `content_block_delta` with `text_delta`, `message_stop`).

Adapter rules: `Received(message_id)` = `command_lifecycle` with state `queued` and that `command_uuid`; `TurnStarted` for a message turn = `started`; "taken into context" = `isReplay` user event with that uuid. After an Orch restart `arrived(id)` can be answered from the saved output file: `queued` line present means yes; absent means the CLI never read it (only if the CLI is still alive and the fifo still holds the line it will still arrive). Not tested: crash between write and read.

### 2. Background jobs

Start of three jobs (A exit 0 after 10 s, B exit 3 after 15 s, C exit 0 after 20 s), turn end while they run, then three separate wake turns. The wake turns start with `system/init`, have no user event and no `command_lifecycle`:

```
{"type":"command_lifecycle","state":"queued","command_uuid":"eeee..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"eeee..0001"}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"user","content":"Start three separate Bash commands with run_in_background=true: (A) sleep 10; echo A-out; ...","uuid":"eeee..0001","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 10; echo A-out\", \"description\": \"Run background job A\", \"run_in_background\": true}"}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 15; echo B-err >&2; exit 3\", \"description\": \"Run background job B\", \"run_in_background\": tr..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 20; echo C-out\", \"description\": \"Run background job C\", \"run_in_background\": true}"}]}
{"type":"system","subtype":"background_tasks_changed","tasks":["bh5fn366o"]}
{"type":"system","subtype":"task_started","task_id":"bh5fn366o","description":"Run background job A","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: bh5fn366o. Output is being writ...","is_error":"False"}],"uuid":"bbbe..6f84"}
{"type":"system","subtype":"background_tasks_changed","tasks":["bh5fn366o","brcaweiuk"]}
{"type":"system","subtype":"task_started","task_id":"brcaweiuk","description":"Run background job B","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: brcaweiuk. Output is being writ...","is_error":"False"}],"uuid":"209f..6f4b"}
{"type":"system","subtype":"background_tasks_changed","tasks":["bh5fn366o","brcaweiuk","bk131psf3"]}
{"type":"system","subtype":"task_started","task_id":"bk131psf3","description":"Run background job C","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: bk131psf3. Output is being writ...","is_error":"False"}],"uuid":"281a..517b"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"started"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":4,"api_error_status":null,"result":"started"}
{"type":"command_lifecycle","state":"completed","command_uuid":"eeee..0001"}
{"type":"system","subtype":"task_updated","task_id":"bh5fn366o","patch":{"status":"completed","end_time":1795607976794}}
{"type":"system","subtype":"task_notification","task_id":"bh5fn366o","status":"completed","summary":"Background command \"Run background job A\" completed (exit code 0)","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/bh5fn366o.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":["brcaweiuk","bk131psf3"]}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"A: exit code 0"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"A: exit code 0"}
{"type":"system","subtype":"task_updated","task_id":"brcaweiuk","patch":{"status":"failed","end_time":1795607981809}}
{"type":"system","subtype":"task_notification","task_id":"brcaweiuk","status":"failed","summary":"Background command \"Run background job B\" failed with exit code 3","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/brcaweiuk.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":["bk131psf3"]}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"B: exit code 3"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"B: exit code 3"}
{"type":"system","subtype":"task_updated","task_id":"bk131psf3","patch":{"status":"completed","end_time":1795607986814}}
{"type":"system","subtype":"task_notification","task_id":"bk131psf3","status":"completed","summary":"Background command \"Run background job C\" completed (exit code 0)","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/bk131psf3.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":[]}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"C: exit code 0"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"C: exit code 0"}
```

Job ending while a turn runs (two background jobs of 8 s plus a 16 s foreground tool): the notification events are emitted when the jobs end, the `<task-notification>...` text is injected as `isReplay` user messages at the tool boundary and the same turn answers; `turns=4`, no extra wake turn:

```
{"type":"command_lifecycle","state":"queued","command_uuid":"ffff..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"ffff..0001"}
{"type":"user","content":"Now in one go: start two background Bash jobs (run_in_background=true) 'sleep 8; echo D' a...","uuid":"ffff..0001","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 8; echo D\", \"description\": \"Run background job D\", \"run_in_background\": true}"}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 8; echo E\", \"description\": \"Run background job E\", \"run_in_background\": true}"}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"python3 -c 'import time; time.sleep(16)'\", \"description\": \"Sleep 16 seconds in foreground via pyt..."}]}
{"type":"system","subtype":"background_tasks_changed","tasks":["bp6vcdpc4"]}
{"type":"system","subtype":"task_started","task_id":"bp6vcdpc4","description":"Run background job D","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: bp6vcdpc4. Output is being writ...","is_error":"False"}],"uuid":"a30b..3e91"}
{"type":"system","subtype":"background_tasks_changed","tasks":["bp6vcdpc4","bv5yhevay"]}
{"type":"system","subtype":"task_started","task_id":"bv5yhevay","description":"Run background job E","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: bv5yhevay. Output is being writ...","is_error":"False"}],"uuid":"c5c6..4cee"}
{"type":"system","subtype":"task_started","task_id":"bd0mn0yhn","description":"Sleep 16 seconds in foreground via python","is_backgrounded":"False","task_type":"local_bash"}
{"type":"system","subtype":"task_updated","task_id":"bp6vcdpc4","patch":{"status":"completed","end_time":1795608035415}}
{"type":"system","subtype":"task_notification","task_id":"bp6vcdpc4","status":"completed","summary":"Background command \"Run background job D\" completed (exit code 0)","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/bp6vcdpc4.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":["bv5yhevay"]}
{"type":"system","subtype":"task_updated","task_id":"bv5yhevay","patch":{"status":"completed","end_time":1795608035428}}
{"type":"system","subtype":"task_notification","task_id":"bv5yhevay","status":"completed","summary":"Background command \"Run background job E\" completed (exit code 0)","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/bv5yhevay.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":[]}
{"type":"system","subtype":"task_notification","task_id":"bd0mn0yhn","status":"completed","summary":"Sleep 16 seconds in foreground via python","output_file":""}
{"type":"user","content":[{"type":"tool_result","content":"(Bash completed with no output)","is_error":"False"}],"uuid":"0822..24e3"}
{"type":"user","content":"<task-notification>\n<task-id>bp6vcdpc4</task-id>\n<tool-use-id>toolu_014DCpD6ZcDQshevwSwkvU...","uuid":"aa20..159f","isReplay":true}
{"type":"user","content":"<task-notification>\n<task-id>bv5yhevay</task-id>\n<tool-use-id>toolu_01CGAcjveQfyTYuKszQuWS...","uuid":"2ad6..24cd","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"D: exit code 0\nE: exit code 0\nfg-done"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":4,"api_error_status":null,"result":"D: exit code 0\nE: exit code 0\nfg-done"}
{"type":"command_lifecycle","state":"completed","command_uuid":"ffff..0001"}
```

Two jobs ending in the same second while idle: one wake turn for both, preceded by an empty `result` (`num_turns:0`) that must be ignored as a turn end:

```
{"type":"command_lifecycle","state":"queued","command_uuid":"9999..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"9999..0001"}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"user","content":"Start two background Bash jobs (run_in_background=true): 'sleep 8; echo F' and 'sleep 8; e...","uuid":"9999..0001","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 8; echo F\", \"description\": \"Run background job F\", \"run_in_background\": true}"}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 8; echo G\", \"description\": \"Run background job G\", \"run_in_background\": true}"}]}
{"type":"system","subtype":"background_tasks_changed","tasks":["bcsde7jj5"]}
{"type":"system","subtype":"task_started","task_id":"bcsde7jj5","description":"Run background job F","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: bcsde7jj5. Output is being writ...","is_error":"False"}],"uuid":"329c..70ae"}
{"type":"system","subtype":"background_tasks_changed","tasks":["bcsde7jj5","buiw6xwgs"]}
{"type":"system","subtype":"task_started","task_id":"buiw6xwgs","description":"Run background job G","is_backgrounded":"True","task_type":"local_bash"}
{"type":"user","content":[{"type":"tool_result","content":"Command running in background with ID: buiw6xwgs. Output is being writ...","is_error":"False"}],"uuid":"0b31..00f4"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"ok"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":3,"api_error_status":null,"result":"ok"}
{"type":"command_lifecycle","state":"completed","command_uuid":"9999..0001"}
{"type":"system","subtype":"task_updated","task_id":"bcsde7jj5","patch":{"status":"completed","end_time":1795608080782}}
{"type":"system","subtype":"task_notification","task_id":"bcsde7jj5","status":"completed","summary":"Background command \"Run background job F\" completed (exit code 0)","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/bcsde7jj5.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":["buiw6xwgs"]}
{"type":"system","subtype":"task_updated","task_id":"buiw6xwgs","patch":{"status":"completed","end_time":1795608080784}}
{"type":"system","subtype":"task_notification","task_id":"buiw6xwgs","status":"completed","summary":"Background command \"Run background job G\" completed (exit code 0)","output_file":"<tmp>/j1-proj/7be3e7e5-9f82-4e07-aea9-819df103e5a6/tasks/buiw6xwgs.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":[]}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"result","subtype":"success","is_error":false,"stop_reason":null,"num_turns":0,"result":""}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"7be3..e5a6"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"F: exit code 0\nG: exit code 0"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"F: exit code 0\nG: exit code 0"}
```

Output file path is in `task_notification.output_file` (under the CLI's tmp folder); it holds the job's stdout/stderr. Task ids are short strings (`bh5fn366o`); `run_id` also exists.

Adapter mapping: `JobStarted` = `task_started` (`is_backgrounded:true`; foreground tools also emit `task_started` with `is_backgrounded:false` and a `task_notification` at end, so filter on `is_backgrounded`). `JobEnded` = `task_notification` (`status` `completed`/`failed`/`stopped`, parse `exit code (\d+)` from `summary`; absent for stopped/killed). Running jobs at any moment = `tasks` of the last `background_tasks_changed` (it also lists foreground tool tasks while they run).

### 3. Long job and time limit

Started `sleep 720; echo long-done` in the background at 20:01:55; during the wait a message sent from a separate process was answered (4). `task_notification` came at 20:13:55 (exactly 720 s), `background_tasks_changed []`, `system/init`, then the wake reply. Longer jobs: not tested (the probe budget is short); the documented limit is above.

Docs (tools reference, "Time limit for background commands", requires 2.1.285+): sessions running unattended (`-p`, SDK, CI) limit background commands; default 30 minutes, 10 minutes when `-p` gets a text prompt instead of `--input-format stream-json`; a `timeout` passed with `run_in_background` replaces it, up to 2 hours; `BASH_DEFAULT_TIMEOUT_MS` above 1800000 raises the default, `BASH_MAX_TIMEOUT_MS` above 7200000 raises the maximum. Interactive terminal sessions have no limit. Observed enforcement:

```
{"type":"user","content":"Start one background Bash job with run_in_background=true and timeout=5000 (milliseconds):...","uuid":"ce11..85fd","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 40; echo t-done\", \"description\": \"Background sleep then echo completion marker\", \"timeout\":..."}]}
{"type":"system","subtype":"background_tasks_changed","tasks":["bm1us5s5z"]}
{"type":"system","subtype":"task_started","task_id":"bm1us5s5z","description":"Background sleep then echo completion marker","is_backgrounded":"True","task_type":"local_bash"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"Started. The background job is running `sleep 40; echo t-done`, and I'll report its status..."}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":2,"api_error_status":null,"result":"Started. The background job is running `sleep 40; echo t-done`, and I'..."}
{"type":"system","subtype":"task_updated","task_id":"bm1us5s5z","patch":{"status":"killed","end_time":1795608867008}}
{"type":"system","subtype":"task_notification","task_id":"bm1us5s5z","status":"stopped","summary":"Background command \"Background sleep then echo completion marker\" was stopped after reaching its bac...","output_file":"<tmp>/t1-proj/260e9939-2b25-4d28-88e2-c9c23595792f/tasks/bm1us5s5z.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":[]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"Status: killed. The 5000 ms timeout ran out before `sleep 40` finished, so `t-done` never ..."}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"Status: killed. The 5000 ms timeout ran out before `sleep 40` finished..."}
```

### 4. Launcher exit, reconnect, killing the CLI

The shell that started the CLI exited immediately in every probe and nothing stopped. A different short-lived process (`python3`) opened the fifo read-write, wrote one user line, and closed it. The CLI answered `PING` while the 12 minute job ran, and woke later. The output file was read again from byte offset 12621 (end of the last line read) and contained only the new events. Orch's own output reader can therefore be restarted at a saved offset; lines are complete JSON lines.

Killing: SIGTERM to the CLI pid ended it within 3 s; the job (`sleep 300`, child of a `zsh -c` child of the CLI) was gone and the output file got `task_notification status:"stopped"` right before exit. SIGKILL to the CLI: CLI gone, `sleep 200` still running (found with `ps`) until it ended by itself. Job and CLI are in different process groups.

### 5. Session errors, model change

```
$ claude -p --input-format stream-json ... --resume 00000000-0000-4000-8000-00000000dead
stderr: No conversation found with session ID: 00000000-0000-4000-8000-00000000dead
stdout: {"type":"result","subtype":"error_during_execution","is_error":true,"num_turns":0,"stop_reason":null,
         "session_id":"00000000-...-dead","errors":["No conversation found with session ID: 00000000-..."], ...}
exit: process ended at startup, no message needed
```

Resume of a real session in a different working folder worked and answered from history. `--resume <id> --model sonnet` (later flag wins over the earlier `--model haiku`): init `model:"claude-sonnet-5-5"`, assistant `model:"claude-sonnet-5-5"`, same `session_id` on every event (no fork unless `--fork-session`). `result.modelUsage` listed `claude-haiku-5-5` and `claude-sonnet-5-5`. A `/compact` also keeps the session id.

### 6. Model and start-of-answer signals

`system/init` fields used: `model`, `session_id`, `permissionMode`, `apiKeySource`, `claude_code_version`. It is repeated at the start of every turn (also wake turns). `assistant.message.model` carries the model of that answer; for API errors it is `<synthetic>` (see 8). Order for a normal turn: `command_lifecycle started`, `system/init`, (`status requesting`), echoed user event, `assistant` thinking/text/tool_use events (each content block as its own `assistant` line unless `--include-partial-messages`), `result`, `command_lifecycle completed`.

### 7. Preventing `nohup` and friends

Without protection, `nohup sleep 40 > /dev/null 2>&1 & echo started-nohup` in a normal foreground Bash call ran; output had no `task_*` and no `background_tasks_changed` event, nothing at job end:

```
{"type":"user","content":"Use Bash (foreground, NOT run_in_background) to run exactly: nohup sleep 40 > /dev/null 2>...","uuid":"c069..90d9","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"nohup sleep 40 > /dev/null 2>&1 & echo started-nohup\", \"description\": \"Launch background sleep an..."}]}
{"type":"user","content":[{"type":"tool_result","content":"started-nohup","is_error":"False"}],"uuid":"a49b..a1aa"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"launched"}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":2,"api_error_status":null,"result":"launched"}
```

Permission rules (`--disallowedTools` entries given as separate arguments or comma separated). Denied: `nohup sleep 30 > /dev/null 2>&1 & echo ...`, `setsid echo three`, `echo one & echo two`, `echo four &`. Pattern syntax: `Bash(nohup *)`, `Bash(setsid *)`, `Bash(*&)`, `Bash(* & *)`. Event:

```
{"type":"user","content":"Use Bash (foreground) to run exactly: nohup sleep 30 > /dev/null 2>&1 & echo started-nohup...","uuid":"b3bf..6acf","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"nohup sleep 30 > /dev/null 2>&1 & echo started-nohup\", \"description\": \"Run nohup sleep in backgro..."}]}
{"type":"system","subtype":"permission_denied","tool_name":"Bash","decision_reason_type":"subcommandResults","message":"Permission to use Bash with command nohup sleep 30 > /dev/null 2>&1 & echo started-nohup has been de..."}
{"type":"user","content":[{"type":"tool_result","content":"Permission to use Bash with command nohup sleep 30 > /dev/null 2>&1 & ...","is_error":"True"}],"uuid":"acfa..5fe6"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"The first command was denied, so I'm trying the fallback command once as instructed."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"sleep 30 > /dev/null 2>&1 & echo started-amp\", \"description\": \"Run background sleep and echo fall..."}]}
{"type":"user","content":[{"type":"tool_result","content":"<tool_use_error>Blocked: sleep 30 followed by: echo started-amp. To wa...","is_error":"True"}],"uuid":"6ea0..2758"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"The first command was denied: \"Permission to use Bash with command nohup sleep 30 > /dev/n..."}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":3,"api_error_status":null,"result":"The first command was denied: \"Permission to use Bash with command noh..."}
```

(The `sleep 30 ... & echo` retry was blocked by the CLI's own rule against `sleep` followed by another command, not by our patterns.) Rules match command text, so forms like `bash -c '... &'` or `(cmd &)` can slip through; the hook is stricter. Hook, passed with `--settings <file>` (so no global config is changed):

```
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"python3 -I /path/nobg_hook.py"}]}]}}
```

Hook script reads the JSON from stdin (`tool_input.command`), prints `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}` when `re.search(r"\b(nohup|setsid|disown|daemonize)\b|(?<![&>])&(?![&>])|\bscreen\s+-dm|\btmux\s+new(-session)?\s+-d", cmd)`. Unit-tested on: `2>&1`, `&&`, `&>` not matched; `x &`, `(x &)`, `x & y`, `nohup` matched; a URL with `?a=1&b=2` is a false positive. Observed result in the stream for a hook deny (no `permission_denied` event; the tool_result carries the reason):

```
{"type":"user","content":"Run each of these with Bash (foreground), one tool call each, in order, and report results...","uuid":"ec33..a185","isReplay":true}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"setsid sleep 30 > /dev/null 2>&1 < /dev/null &\", \"description\": \"Run setsid sleep in background w..."}]}
{"type":"user","content":[{"type":"tool_result","content":"PreToolUse:Bash hook error: Detached shell jobs are not allowed. Use r...","is_error":"True"}],"uuid":"48e7..da10"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"(sleep 30 &) ; echo x\", \"description\": \"Run subshell sleep and echo x\"}"}]}
{"type":"user","content":[{"type":"tool_result","content":"PreToolUse:Bash hook error: Detached shell jobs are not allowed. Use r...","is_error":"True"}],"uuid":"3279..4625"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"tool_use":"{\"command\": \"echo plain-ok\", \"description\": \"Echo plain-ok\"}"}]}
{"type":"user","content":[{"type":"tool_result","content":"plain-ok","is_error":"False"}],"uuid":"78ca..40e9"}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
```

Commands run through `run_in_background:true` contain no `&`, so the hook does not affect real background jobs. To detect instead of block: the foreground tool_use `command` text in `assistant` events can be scanned in the adapter; a surviving process is not visible in the job record.

### 8. API errors (local mock endpoint)

Setup: a small local HTTP server (127.0.0.1) mimicking `/v1/messages` (streaming), `ANTHROPIC_BASE_URL=http://127.0.0.1:PORT`, `ANTHROPIC_API_KEY=<fake>`, `CLAUDE_CONFIG_DIR=<empty temp dir>`, `CLAUDE_CODE_MAX_RETRIES=2` (the default retry count and waits were not measured). The server ignores request headers (they are never read), so no credentials were involved (`apiKeySource:"ANTHROPIC_API_KEY"`). The first reply started a background job (`sleep 40`), then the server was switched to return an error status. The job kept running through the retries and error turns; its `task_notification` came on time and the wake turn failed with the same error, then (server back to ok) the next message succeeded in the same process and session.

Sample (429 during a job; first the user message turn, then the wake turn after the job ended):

```
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"776e..a2af"}
{"type":"system","subtype":"api_retry","attempt":"1","max_retries":"2","retry_delay_ms":"510","error_status":"429","error":"rate_limit"}
{"type":"system","subtype":"api_retry","attempt":"2","max_retries":"2","retry_delay_ms":"1120","error_status":"429","error":"rate_limit"}
{"type":"user","content":"hello again","uuid":"d698..038f","isReplay":true}
{"type":"assistant","model":"<synthetic>","content":[{"text":"API Error: Request rejected (429) \u00b7 Number of request tokens has exceeded your per-minute ..."}],"error":"rate_limit","api_error_status":429,"api_error_type":"rate_limit_error","is_api_error_message":true}
{"type":"result","subtype":"success","is_error":true,"stop_reason":"stop_sequence","terminal_reason":"api_error","num_turns":1,"api_error_status":429,"result":"API Error: Request rejected (429) \u00b7 Number of request tokens has excee..."}
{"type":"system","subtype":"task_updated","task_id":"bjsv5l2qw","patch":{"status":"completed","end_time":1795608639091}}
{"type":"system","subtype":"task_notification","task_id":"bjsv5l2qw","status":"completed","summary":"Background command \"mock job\" completed (exit code 0)","output_file":"<tmp>/m1-proj/776e96e5-ce5a-4721-aa13-a1ea2608a2af/tasks/bjsv5l2qw.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":[]}
{"type":"system","subtype":"init","model":"claude-haiku-5-5","session_id":"776e..a2af"}
{"type":"system","subtype":"api_retry","attempt":"1","max_retries":"2","retry_delay_ms":"602","error_status":"429","error":"rate_limit"}
{"type":"system","subtype":"api_retry","attempt":"2","max_retries":"2","retry_delay_ms":"1007","error_status":"429","error":"rate_limit"}
{"type":"assistant","model":"<synthetic>","content":[{"text":"API Error: Request rejected (429) \u00b7 Number of request tokens has exceeded your per-minute ..."}],"error":"rate_limit","api_error_status":429,"api_error_type":"rate_limit_error","is_api_error_message":true}
{"type":"result","subtype":"success","is_error":true,"stop_reason":"stop_sequence","terminal_reason":"api_error","num_turns":1,"api_error_status":429,"result":"API Error: Request rejected (429) \u00b7 Number of request tokens has excee..."}
```

Classification table. `assistant.error` and `result.terminal_reason` are the fields to read; `result.subtype` is always `success`. The mock returned Anthropic-shaped error bodies; real services may word them differently, so match on the structured fields first:

| Mock response | `system/api_retry` events | `assistant.error` | `api_error_status` / `api_error_type` | `result.terminal_reason` | text |
|---|---|---|---|---|---|
| 429 `rate_limit_error` | yes, `error:"rate_limit"` | `rate_limit` | 429 / `rate_limit_error` | `api_error` | `API Error: Request rejected (429) · <message>` |
| 400 "credit balance is too low" | none | `billing_error` | 400 / `invalid_request_error` | `api_error` | `Credit balance is too low` |
| 529 `overloaded_error` | yes, `error:"overloaded"` | `server_error` | 529 / `overloaded_error` | `api_error` | `API Error: 529 Overloaded. This is a server-side issue, usually temporary...` |
| 500 `api_error` | yes, `error:"server_error"` | `server_error` | 500 / `api_error` | `api_error` | `API Error: 500 Internal server error...` |
| 401 `authentication_error` | yes, `error:"authentication_failed"` | `authentication_failed` | 401 / `authentication_error` | `api_error` | `Invalid API key · Fix external API key` |
| 404 `not_found_error` (model) | none | `model_not_found` | 404 / `not_found_error` | `api_error` | `There's an issue with the selected model (...). It may not exist or you may not have access to it...` |
| 400 "prompt is too long" | none | `invalid_request` | 400 / `invalid_request_error` | `prompt_too_long` | `Prompt is too long` |
| session lost (`--resume` unknown) | n/a | n/a, process exits at startup | n/a | result `subtype:"error_during_execution"` | `No conversation found with session ID: ...` |

Mapping to `ErrorClass`: `rate_limit`, `server_error` (retried by the CLI first) -> `transient`; `billing_error` -> `billing`; `No conversation found` -> `session_lost`; `authentication_failed`, `model_not_found`, `invalid_request` -> `other`. An error also arrives as ordinary `assistant`/`result` events, so the error turn must not be treated as a normal reply (`message.model == "<synthetic>"`, `is_api_error_message:true`, `result.is_error:true`). The process stays alive after an API error.

Invalid model against the real API could not be forced: `set_model` with an unknown name is refused before any request (`control_response` `subtype:"error"`, `error:"Model 'x' not found"`, `error_code:"not_found"`), the process and session continue unchanged, and a job running at that time was unaffected. A startup `--model` with an unknown name against the real API was not tried.

### 9. Interrupt and compact

Interrupt during a foreground tool call, with a background job running (`sleep 60`):

```
{"type":"user","content":[{"type":"text","text":"[Request interrupted by user for tool use]"}],"uuid":"4d28..780d"}
{"type":"result","subtype":"error_during_execution","is_error":true,"stop_reason":"tool_use","terminal_reason":"aborted_tools","num_turns":4,"errors":["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],"result":"None"}
{"type":"command_lifecycle","state":"cancelled","command_uuid":"1616..0001"}
{"type":"system","subtype":"task_updated","task_id":"bh6z3e0at","patch":{"status":"completed","end_time":1795608334662}}
{"type":"system","subtype":"task_notification","task_id":"bh6z3e0at","status":"completed","summary":"Background command \"Background sleep then echo marker\" completed (exit code 0)","output_file":"<tmp>/i1-proj/d47475d5-17ce-4d23-b353-43fbc3657c1a/tasks/bh6z3e0at.out..."}
{"type":"system","subtype":"background_tasks_changed","tasks":[]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"thinking":"..."}]}
{"type":"assistant","model":"claude-haiku-5-5","content":[{"text":"Step 1 finished: the background job ran `sleep 60; echo i-done` and exited with code 0.\n\nS..."}]}
{"type":"result","subtype":"success","is_error":false,"stop_reason":"end_turn","terminal_reason":"completed","num_turns":1,"api_error_status":null,"result":"Step 1 finished: the background job ran `sleep 60; echo i-done` and ex..."}
```

(The `control_response` line is `{"type":"control_response","response":{"subtype":"success","request_id":"int1","response":{"still_queued":[]}}}`; the foreground task also got `task_notification status:"stopped"`.) The job then completed 40 s later and the session woke itself. Interrupt while only text is streaming and interrupt with a queued message were not tested.

Compact (user text `/compact`, session had one background job finished earlier):

```
{"type":"command_lifecycle","state":"queued","command_uuid":"1717..0001"}
{"type":"command_lifecycle","state":"started","command_uuid":"1717..0001"}
{"type":"system","subtype":"status","status":"compacting"}
{"type":"system","subtype":"status","status":"None","compact_result":"success"}
{"type":"system","subtype":"compact_boundary","compact_metadata":"{\"trigger\": \"manual\", \"pre_tokens\": 23709, \"post_tokens\": 7068, \"cumulative_dropped_tokens\": 16641, \"duration_ms\": 5985, \"preserved_segment\": {\"head_uuid\": \"2f748e27-96c2-47ad-95c6-453908bed8ef\", \"anchor_uuid\": \"ab0f2276-f3eb-4aef-ad84-3aac33f92d23\", \"tail_uuid\": \"2a257571-6879-458d-8a39-bff7ca3a5ed...","logical_parent_uuid":"2a257571-6879-458d-8a39-bff7ca3a5ed7"}
{"type":"user","content":"This session is being continued from a previous conversation that ran out of context. The ...","uuid":"ab0f..2d23","isSynthetic":true}
{"type":"user","content":"<local-command-stdout>Compacted </local-command-stdout>","uuid":"e9e9..8309","isReplay":true}
{"type":"user","content":"<command-name>/compact</command-name>\n            <command-message>compact</command-messag...","uuid":"1717..0001","isReplay":true}
{"type":"result","subtype":"success","is_error":false,"stop_reason":null,"num_turns":0,"result":""}
{"type":"command_lifecycle","state":"completed","command_uuid":"1717..0001"}
{"type":"user","content":"<local-command-stdout>Set model to `sonnet (claude-sonnet-5-5)`</local-command-stdout>","uuid":"d2fa..1275","isReplay":true}
```

`set_model` mid-process (no restart, same session): `control_response` `{"subtype":"success","request_id":"sm1","response":{"model":"claude-sonnet-5-5"}}`, then a `user` event with `<local-command-stdout>Set model to ...`, then the next turn's init reports the new model.

### Not tested or not verifiable here

- Jobs longer than 12 minutes; enforcement at the 30 minute default.
- Interrupt while text is streaming; interrupt with a queued message.
- Real rate limit, billing, overload errors from the real service (deliberately not provoked); real error text may differ from the mock.
- Message text without `uuid`; crash between writing to the fifo and the CLI reading it.
- `--permission-mode bypassPermissions` (refused by the environment's permission classifier; not worked around).
- `claude attach` for background sessions; a second terminal on an open session.
- Windows or Linux behaviour.
