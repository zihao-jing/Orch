# Probe P5: Antigravity CLI (`agy`)

Run on 2026-11-25 in temporary folders under `/Users/fancy/.claude/jobs/4b93f1df/tmp/probes/p5/` (not in any user project). Cheapest models used: `gemini-3.8-flash-low` and, for the switch test, `gemini-3.8-flash-medium`. Nothing signed in, no settings changed. Probe driver: `scripts/drive.py` with `FMT=agy` (opens a FIFO read-write as stdin).

## Version

- `agy --version`: **1.5.8** (`/Users/fancy/.local/bin/agy`).
- Signed in: yes. Plain `agy -p` calls worked with no login step.
- Side effect to know about: `--mode plan` writes a plan artifact file under `~/.gemini/antigravity-cli/brain/<conversation>/`. Use the default mode in tests.
- `--dangerously-skip-permissions` was **denied** by the auto-mode classifier, so it was not used (not worked around). The job test ran in default mode, where `run_command` was allowed without prompts in the probe folder.

## Results

| Question | Answer | Verified? |
|---|---|---|
| Input mode that stays open | `--input-format stream-json --output-format stream-json`. One NDJSON message per stdin line, shape `{"event":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}`. A FIFO held open: 4 messages, 4 results, one conversation. A message sent during a turn was queued and answered after it. Without the `event` key or `message` object the process exits with an error (`stream input "user" message is missing the "message" field`). | verified |
| Message sent while writing | Queued by the CLI; each becomes its own turn, in order. **While an async job runs the turn stays open, so a message sent then is not answered until the job ends** (timed test: sent at 25 s, job ended at 54 s, answered at 59 s). | verified |
| Background jobs with a record | `run_command` takes `WaitMsBeforeAsync`. With `0` the command goes async and returns an id `<conversation>/task-N`. Without it, the command ran in the foreground (4 s block). Record: `manage_task` with `Action: list` shows status, created and completed times, and "No background tasks are currently running" when empty. Output also in `brain/<conv>/.system_generated/tasks/task-N.log` (the failing one held `bad`). | verified |
| Self-wake when a job ends | **Not as a new turn.** The turn stays open until its async jobs end: in the timed test (45 s job, "reply STARTED and end your turn") `STARTED` itself appeared only when the job ended (54 s), followed by a `system_message`, the report of the job and one `result` covering everything (57 s). The earlier runs also show one `result` for reply and wakeups. | verified (coordinator, with timestamps) |
| Resume a session | `--conversation <id>` (the `conversation_id` comes in the first `init` event in stream mode and in `result` in `-p` json). A new stream process with `--conversation` recalled a code word given in the old process. | verified |
| Error for an unknown session id | **Not an error.** `warning: conversation "<id>" not found` on stderr, exit 0, and a **new** conversation with a new `conversation_id` answers. Orch must compare the returned id with the requested one. | verified |
| Does a model switch keep the session | Yes. Conversation started on `gemini-3.8-flash-low`, continued on `gemini-3.8-flash-medium`: recall worked; `init.model` reports `gemini-3.8-flash-medium`. | verified |
| Effort levels listed | `--effort low\|medium\|high\|xhigh\|max` (help text). `/effort` (as a `-p` prompt) returns `{"command":{"name":"effort","data":{"current":"low","available":["low","medium","high"]}}}`. Model ids also encode effort: `gemini-3.8-flash-{low,medium,high}`, `gemini-4-*-{low,medium,high,xhigh,max}` (from `agy models`). `--model gemini-3.8-flash-low --effort high` fails: `--model gemini-3.8-flash-low conflicts with --effort=high`. Orch must pick one of the two ways, not both. | verified |
| Actual model reported | Stream mode: `init.model`. The `-p --output-format json` result has no model field. | verified (stream mode only) |
| Error for a bad model | `--model no-such-model`: exit 1; JSON `{"status":"ERROR","error":"invalid model selection (--model \"no-such-model\" --effort \"\"): model no-such-model is not recognized as a known model or custom model in settings\nAvailable models:\n  Gemini 4 Neon (High)..."}` (list of display names follows). Bad effort: `invalid --effort "banana" (valid: low, medium, high, xhigh, max)`. | verified |
| Model switch inside one stream session | Not tested. | not verified |
| Compaction | `/compact` as a `-p` prompt: exit 0, `status: SUCCESS`, empty response, 30.6 s, stderr `warning: run ended with no output and no recorded error`. Whether the history was actually compacted was not checked. | command accepted; effect not verified |
| Two processes on one conversation | A second `agy -p --conversation <id>` started 4 s after a stream turn of 71 s on the same id. It answered after the first turn (`num_turns` 6, `duration_seconds` 74), with no error. The ordering is inferred from the timings and turn count. | observed (ordering inferred) |
| Terminal joining | `agy` interactive and `remote-control` exist in help; not run. | not verified |

## Commands used

```
# smoke test
agy -p "Reply with the single word OK." --output-format json --model gemini-3.8-flash-low

# open input (FIFO held by driver)
agy --input-format stream-json --output-format stream-json --model gemini-3.8-flash-low
# stdin line per message:
{"event":"user","message":{"role":"user","content":[{"type":"text","text":"Reply with the word ONE only."}]}}

# async job (default mode)
# prompt asked for run_command with WaitMsBeforeAsync 0 for "sleep 12; echo JOBOK > ok.txt" and "sleep 6; echo bad 1>&2; exit 3"

# sessions
agy -p "Remember this code word: LIME-9. Reply only OK." --output-format json --model gemini-3.8-flash-low
agy -p "What code word did I give you?" --conversation <id> --output-format json --model gemini-3.8-flash-medium
agy -p "Reply OK." --conversation 00000000-0000-4000-8000-000000000000 --output-format json   # new conversation, warning on stderr

# errors
agy -p "Reply OK." --model no-such-model --output-format json     # exit 1, status ERROR
agy -p "Reply OK." --model gemini-3.8-flash-low --effort banana   # exit 1
agy -p "/effort" --conversation <id> --output-format json          # lists current and available
agy models                                                          # model list
```

## Event samples (shortened)

```
{"event":"init","conversation_id":"<uuid>","init":{"model":"gemini-3.8-flash-low","cwd":"<probe dir>","tools":["run_command","manage_task",...]}}
{"event":"step_update","step_update":{"conversation_id":"<uuid>","step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"ONE"}}
{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"sleep 12; echo JOBOK > ok.txt"}}}}
{"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"tool","tool_info":{"output":"Command exited with code 0.\n..."}}}
{"event":"step_update","step_update":{"step_index":5,"state":"DONE","step_type":"system_message"}}          # wake-up, no input sent
{"event":"step_update","step_update":{"step_index":6,"state":"DONE","step_type":"agent_response","text_delta":"Task `<id>/task-3` has completed (exited with code 3)."}}
{"event":"result","result":{"conversation_id":"<uuid>","status":"SUCCESS","response":"ONE\n","duration_seconds":3.2,"num_turns":1,"usage":{...}}}
{"event":"result","result":{"status":"ERROR","response":"MARKER-C\n"}}   # last line after our kill: "stream input cancelled: context canceled"
```

## Surprises

- An unknown `--conversation` id silently starts a new conversation (exit 0).
- `--model <id-with-effort-suffix> --effort <x>` is rejected, so effort is either part of the model id or the `--effort` flag, never both.
- Two processes on the same conversation do not error; the second waits for the running turn.
- Agy keeps its turn open while an async job runs and reports the job inside that turn: there is no intermediate ending (JOB-2, JOB-3), and no reply to anyone until the job ends. The tool step stays `ACTIVE` meanwhile.
- The last stream `result` after a kill has `status: ERROR` (our kill). Orch must not count it as a turn error.

## Recommended capability values

| Capability | Value | Basis |
|---|---|---|
| sends_while_writing | `queued` (each message is its own later turn, in order) | verified |
| reconnects | `true` (new process with `--conversation <id>` and stream input) | verified |
| job_record | `true` (`manage_task` list; `brain/<conv>/.system_generated/tasks/task-N.log`) | verified |
| shares_terminal | `not verified` | not verified |
| compacts | `not verified` (`/compact` accepted, effect not checked) | not verified |
| self_wake (JOB-4) | not applicable: the turn does not end while a job runs | verified |

## Conclusion

Agy can run as an open CLI: one process with stream-json input stays open across messages, queues messages sent during a turn, and keeps the turn open until its async jobs end. Orch must check that the returned conversation id equals the requested one, and must not send both `--model` with an effort suffix and `--effort`.

## Timed job test (coordinator, 2026-11-25)

Seconds since start; `gemini-3.8-flash-low`, default mode, open stdin pipe.

```
  0.0 >>> sent: run_command with WaitMsBeforeAsync 0 "sleep 45; echo done", reply STARTED, end the turn
  8.8 step 2 ACTIVE tool run_command
 25.0 >>> sent: Reply with the single word PING.
 53.9 step 2 DONE  tool run_command
 53.9 step 3 DONE  agent_response "STARTED"
 53.9 step 4 DONE  system_message
 56.6 step 5 DONE  agent_response "The command has finished executing with output: `done`."
 56.6 result SUCCESS num_turns 1
 59.3 step 7 DONE  agent_response "PING"
 59.3 result SUCCESS num_turns 2
```

One run, one model, 45 s. Whether a much longer job is also waited for is not verified.

# Probe P6: the missing pieces (terminal, transcript, compact, one job, model list)

Run on 2026-11-28 in a scratch folder (not in any user project), model `gemini-3.8-flash-low`, default mode, no settings changed. agy was **1.5.17** at the start and had updated itself to **1.5.19** by the second step; everything below is 1.5.19 unless marked. Drivers: a stream-json driver (pipe stdin, one message after each `result`), a pty driver for interactive mode, and a `ps` watcher for the job test.

| Question | Answer | Verified? |
|---|---|---|
| Model list | `agy models` prints `Fetching available models...` on its first line, then one `<id>\t<display name>` per line. Ids carry the effort (`gemini-4-neon-xhigh`, `gemini-3.8-flash-low`); some have none (`gemini-pro-orchestrator`). Also lists Claude and GPT-OSS ids. | verified |
| Terminal take-over | `agy --conversation <id> --model <m>` in a terminal opens the conversation with its history on screen; a message typed there is answered in the same conversation. A stream process started afterwards with `--conversation <id>` saw that reply (quoted it). | verified (pty) |
| Terminal: first run in a folder | Interactive mode asks "Do you trust the contents of this project?" (default Yes) before anything else. Stream mode never asks. | verified |
| Transcript | `~/.gemini/antigravity-cli/brain/<conv>/.system_generated/logs/transcript.jsonl`, one step per line: `step_index`, `source` (`USER_EXPLICIT`, `MODEL`, `SYSTEM`), `type` (`USER_INPUT`, `PLANNER_RESPONSE`, `SYSTEM_MESSAGE`, `CHECKPOINT`, `GENERIC`), `status`, `created_at` (UTC, seconds), `content`; model steps add `model` and token counts. User text is wrapped in `<USER_REQUEST>…</USER_REQUEST><ADDITIONAL_METADATA>…`. Turns from stream mode and from the terminal are both there. `transcript_full.jsonl` adds `tool_calls` on model steps and the tool results (`GENERIC`). | verified |
| Resume notice | Each resume adds a `SYSTEM_MESSAGE` step: "All your subagents and background tasks have been stopped due to server restart." It appears in the stream as `system_message` DONE right after the first `user_input`. Background jobs do not survive the process. | verified |
| Compact | `/compact` sent as a stream-json message: `result` SUCCESS with an empty response; the transcript gets a `CHECKPOINT` step ("# Resuming from a compaction …"). Context of the next turn: 16.1k tokens before, 15.5k after (small history; the rest is the fixed prompt). The code word given before was still recalled. | verified |
| `result.usage` | Totals for the whole process, not the turn. Per-step tokens are in the transcript. | verified |
| Background job, how it shows (changed since 1.5.8) | The model passed `WaitMsBeforeAsync: 500` (asked for 0) and the command went to the background as `<conv>/task-2`: the model replied STARTED while it ran. **The stream has no task id**: the tool step stays `ACTIVE` until the command ends, and its `DONE` carries the command's final output. The task id is only in the transcript (`GENERIC`: "Tool is running as a background task with task id: <conv>/task-2"), and the end notice there names it (`SYSTEM_MESSAGE … sender=<conv>/task-2`). | verified |
| Stop one job | The job runs as `/bin/zsh -c <CommandLine>`, a child of agy, leader of its own process group, with the command as its children. SIGTERM to the command ended the job; agy reported `Command exited with code 143` to the model, the turn ended normally, and the process stayed up. | verified |

## Surprises (P6)

- agy updates itself while in use (1.5.17 to 1.5.19 within minutes). Output formats can change between runs.
- With 1.5.19 the adapter's job detection (task id in the tool output) no longer fires: a background job looks like one long tool call. The transcript has what is needed.
- The model does not always pass the `WaitMsBeforeAsync` value it is told; 500 still went to the background.
