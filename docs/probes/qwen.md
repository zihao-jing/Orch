# Probe P5: Qwen Code CLI (`qwen`)

Run on 2026-11-25 in temporary folders under `/Users/fancy/.claude/jobs/4b93f1df/tmp/probes/p5/` (not in any user project). Cheap models only (`qwen3.8-flash` where a model was chosen; otherwise the CLI default `qwen4-max`). Nothing signed in, nothing configured. Probe driver: `scripts/drive.py` (opens a FIFO read-write as stdin, so the input never reaches EOF).

## Version

- `qwen --version`: **0.28.4** (`/opt/homebrew/bin/qwen`, npm package `@qwen-code/qwen-code`).
- Signed in: yes. Plain `qwen -p` calls worked with no login step.

## Results

| Question | Answer | Verified? |
|---|---|---|
| Input mode that stays open (stream-json input) | `--input-format stream-json --output-format stream-json --include-partial-messages`. A FIFO held read-write stayed open. Two messages sent one after another, then a third sent while the second was still writing: three `result` events, one per message, processed in order. No interleaving. | verified |
| Message sent while the model is writing | Accepted and queued. It starts as its own turn after the current one ends (test: 1..60 generation, then two more messages sent at once). | verified |
| Background jobs with a record | Yes. `run_shell_command` with `is_background: true` returns `Background shell bg_<id> started (pid N).` A record is kept: `/tasks` lists it, and an on-disk `shell-<id>.status` JSON file (`status`, `exitCode` or `error: "exited with code 3"`, `startTime`, `endTime`) plus an `.output` file under `~/.qwen/tmp/<hash>/background-shells/<session>/`. | verified |
| Self-wake when a job ends | **No.** A success job (ended after 15 s) and a failing job (exit 3) both ended; the stream stayed open for 50 s with no new turn. The next user message showed the model had no notification of either job ("UNKNOWN" for their status). | verified (negative) |
| Resume a session | `--resume <session-id>` (or `--session-id <uuid>` on the first run). A new process with `--resume` and stream-json input recalled a code word given in the old process. | verified |
| Error for an unknown session id | `No saved session found with ID <id>. Run \`qwen --resume\` without an ID to choose from existing sessions.` exit code 1 | verified |
| Does a model switch keep the session | Yes. Session created on `qwen4-max`, resumed with `-m qwen3.8-flash`: recall worked; `stats.models` lists both models. | verified |
| Effort levels listed | `/effort` (in `-p` mode) prints `Reasoning effort: not set (using the model/provider default). Available: low, high, xhigh`. `/effort banana` error: `Unknown reasoning effort "banana". Choose one of: low, high, xhigh.` There is no `--effort` CLI flag. Setting an effort was not tested (it may write settings). | listing verified; setting not verified |
| Actual model reported | Yes: `system/init.model`, each `assistant.message.model`, and `result.stats.models` (keys are the models that answered). | verified |
| Error for a bad model, `-m <unknown>` | **Silent fallback.** Exit 0, the default model answers, and `init.model` shows `qwen4-max`. `qwen3.5-plus` was ignored this way. Orch must compare the reported model with the requested one. | verified |
| Error for a bad model, `/model <unknown>` | Error turn: `Model 'qwen3.5-plus' is not available for auth type 'anthropic'. Available models for 'anthropic': qwen4-max, qwen3.8-max, qwen3.8-flash, deepseek-v4.1-flash-1017, deepseek-v4-pro-0813, glm-5.3.` `result.is_error: true`, subtype `error_during_execution`. | verified |
| Model switch inside one stream session | Not tested (no in-session switch tool was run; `/model <id>` only errors or persists). | not verified |
| Compaction | `/compress` in a stream session: `Context compressed (16370 -> ~14627).` The session still recalled its earlier facts afterwards. | verified |
| Terminal joining (`qwen attach`, `qwen serve`) | `qwen serve` (HTTP daemon, experimental) and `qwen attach --session <id>` exist in `--help`. Not run. `qwen sessions ps` reported no sessions. | not verified |
| Self-wake via `loop_wakeup`/cron tools | Tools exist in the tool list; not tested. | not verified |

## Commands used

```
# smoke test (plan mode, json output)
qwen -p "Reply with the single word OK." --output-format json --approval-mode plan

# open input, FIFO held read-write by the driver
qwen --input-format stream-json --output-format stream-json --include-partial-messages --approval-mode plan
# stdin line per message:
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Reply with the word ONE only."}]}}

# background job (yolo was needed for shell in headless mode; probe folder only)
qwen --model qwen3.8-flash --input-format stream-json --output-format stream-json --include-partial-messages --approval-mode yolo

# sessions
qwen -p "Remember this code word: PINEAPPLE-7. Reply only OK." --session-id <uuid> --output-format json --approval-mode plan
qwen -p "What code word did I give you?" --resume <uuid> --output-format json --approval-mode plan
qwen -p "..." --resume <uuid> -m qwen3.8-flash --output-format json --approval-mode plan

# errors
qwen -p "x" --resume 00000000-0000-4000-8000-000000000000 --output-format json   # rc 1
qwen -p "/model qwen3.5-plus" --output-format json                                # error turn
qwen -p "/effort" --output-format json                                            # lists low, high, xhigh
```

## Event samples (shortened)

```
{"type":"system","subtype":"init","session_id":"<uuid>","model":"qwen4-max","permission_mode":"plan","qwen_code_version":"0.28.4",...}
{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta",...}}}
{"type":"assistant","message":{"model":"qwen4-max","content":[{"type":"text","text":"ONE"}]}}
{"type":"result","subtype":"success","session_id":"<uuid>","is_error":false,"num_turns":1,"result":"ONE","usage":{...}}

# background job
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"run_shell_command","input":{"command":"sleep 3; exit 3","is_background":true}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","content":"Background shell bg_0a9f9750 started (pid 26490)."}]}}
# /tasks (sent as a user message) answer:
Background tasks (1 total)
[bg_0a9f9750] failed: exited with code 3  3s pid=26490  sleep 3; exit 3

# status file (on disk)
{"id":"bg_0a9f9750","status":"failed","pid":26490,"error":"exited with code 3",...}
{"id":"bg_71a23c47","status":"completed","exitCode":0,...}
```

## Surprises

- `-m <unknown>` is ignored without any error; only `/model <unknown>` reports the error.
- Plan mode blocked writing to Qwen memory during one test. The reply said so instead of failing.
- Killing the process prints `Operation cancelled.` on stderr; that is the probe's own kill, not a turn error.
- Each stream-json `system/init` is repeated at the start of each turn in the same process.

## Recommended capability values

| Capability | Value | Basis |
|---|---|---|
| sends_while_writing | `queued` (each message is its own later turn, in order) | verified |
| reconnects | `true` (new process with `--resume <id>` and stream input) | verified |
| job_record | `true` (`/tasks`, `background-shells/*.status`) | verified |
| shares_terminal | `not verified` (`qwen attach` / `qwen serve` exist) | not verified |
| compacts | `true` (`/compress`) | verified |
| self_wake (JOB-4) | `false` (no wake observed; no notification delivered on the next message) | verified (negative) |

## Conclusion

Qwen can run as an open CLI: one process with stream-json input stays open across several messages, and a message sent during a turn is queued and answered in order. Orch still needs a fallback for JOB-4 (self-wake) and must check the reported model against the requested one.
