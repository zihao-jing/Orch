# Task U: a clearer, calmer dashboard

For: one coding agent (Codex). Coordinator: the Claude session that merges branches.

## Tips

1. Work on your own branch: worktree `/Users/fancy/Documents/Orch/.worktrees/ui`, branch `rd-ui` (made from
   `redesign`). Change only the page, `orch/runtime/page/` (`index.html`, `style.css`, `app.js`, `logic.js`), and its
   tests (`tests/test_rt_k.py`, plus new `tests/test_rt_u_*.py` if useful). Touch `orch/runtime/board.py` only where
   the page really needs data it cannot get (say why in the report). No other file under `orch/`.
2. Run only the related tests while you work: `python3 tests/run.py rt_k` (and your new ones).
3. Run the full test suite (`python3 tests/run.py`) once, at the end.
4. Only after all items below are done: commit on `rd-ui` (message ends with a blank line and
   `Co-Authored-By: Codex <noreply@openai.com>`), never push, and append the report at the end of this file.

Plain HTML, CSS and JavaScript, as now: no framework, no build step, no web fonts or files from the network. Python 3.9
and the standard library for tests. Keep every text inserted as text, never as HTML (`test_no_text_is_inserted_as_html`).

## What to read (and only this)

- The four page files and `tests/test_rt_k.py`.
- `docs/redesign.md`, rows UI-1…UI-6 (`grep -n "UI-[1-6]" docs/redesign.md`): every control they name stays.
- `orch/runtime/board.py` lines 200–260: what `api/messages`, `api/message` and `api/turn` return. `api/turn?id=<turn>`
  already gives a turn's `input`, `output` and `system_prompt`; `api/messages` is paged (`limit`, `before`, `q`,
  `agent`, and it says `more`).
- For the old behaviour of the message list only (not its look): `git show 29a2e04^:orch/board.html`, the part after
  the comment `B9b: the timeline` (`BLOCKS`, `lineHTML`, `promptPanel`, `PAGE`).

## See it

A finished real run to look at, with 47 messages, 5 agents, gates, jobs, a model switch and two audits. Read it, never
change its files and never press buttons that send commands:

```
cd /Users/fancy/.claude/jobs/4b93f1df/tmp/s-live && PYTHONPATH=/Users/fancy/Documents/Orch/.worktrees/ui \
  python3 -m orch serve . --port 8793
```

Take screenshots before and after with headless Chrome. The page keeps polling, so use `--timeout`, not
`--virtual-time-budget`:

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --hide-scrollbars \
  --user-data-dir=/tmp/u-shots --window-size=1440,900 --timeout=4000 --screenshot=/tmp/u-1440.png http://127.0.0.1:8793/
```

Check it at 1440×900, at 1440 wide with the full height, at 400 wide (phone), and in dark mode (`--force-dark-mode`).
Stop your server when you are done.

## What is wrong now (seen on that run)

- The Team grid fills the first screen; progress and the messages start below it. The manager card comes last.
- "Needs you" is shown with nothing to do ("You answered 3 question(s)"). Two yellow warning banners carry good news
  (all milestones done; a model switched).
- The messages are a wall of about 60 rows that all look the same: Orch's housekeeping ("Nothing is going on…",
  "My answers: …") weighs as much as the manager's report to the user, and almost every row says "Answered".
- Agent cards: "Critic Critic"; a model line such as `zenmux/deepseek/deepseek-v4-pro (chosen: deepseek-v4-pro)
  (backup: deepseek-v4-pro)`; up to six buttons with a solid red Delete always showing; a terminal line on each card;
  uneven heights.
- Boxes in boxes in boxes, thin borders everywhere, nearly one text size (12–13 px) and tiny uppercase headings.
- Six identical header buttons: "Stop agents" looks as harmless as "Light/dark".
- At 400 px wide the header buttons and cards run off the right edge.

## What to build

### 1. Messages (the user's own requests: do these first)

- **Thin rows**, one line each, about 28 px high, like the old board: time · sender → recipient · status only when it
  is not the normal end (waiting, held, failed, dropped) · the model's short name for a message an agent wrote · a
  one-line preview · input → output size in characters for a message a turn wrote (e.g. `12k → 1.4k`; mark an input
  above 30k). No box around each row; a hover tint and a thin divider are enough. Messages to the user stand out
  (accent bar at the left), and so do messages still waiting for an answer.
- **Inputs.** A row opens in place to show:
  - the full text, with markdown;
  - "Input to the turn that wrote this" (folded; load `api/turn?id=<m.turn>` only when it is opened, then cache it),
    with that turn's system instructions folded inside it;
  - "What <recipient> received" (folded) for a message an agent received: the exact text its program got, that is,
    this message with what Orch added around it (who it is from, reminders, other messages sent along). It is
    `rendered` from `api/message`. Under the title, one line says so: "The message as delivered, with Orch's additions."
  
  Keep "Send to critic" where it is offered now. What a user has opened stays open across refreshes.
- **Every label says what it shows.** No label that needs this document to be understood. Name the agent and the
  thing: "Input to the critic's turn that wrote this", "What the worker received", "The manager's whole reply"
  (today's "Show the whole reply": everything that turn wrote, not only this message). Where a short label cannot say
  it all, add one plain line of explanation under it or a `title` tooltip. The same goes for the level filters, the
  Orch notes switch, statuses ("Answered" means the recipient replied to it; "Delivered" means it reached the
  recipient), and the buttons in the "⋯" menus.
- **Three levels of interaction as filters** (the function of the old board's levels, in the new list; still one list
  in time order, no step blocks, per UI-2):
  1. *You ↔ Manager*: any message from or to the user, and Orch ↔ manager;
  2. *Manager ↔ Critic*: manager ↔ critic, and Orch → critic;
  3. *Critic ↔ Workers*: critic ↔ any worker, and Orch → a worker.
  
  Messages to or from the monitor form a small fourth group, *Monitor*. Use agents' roles, not their ids. A separate
  switch, *Orch notes*, hides Orch's own housekeeping messages (silence checks, reminders, notices); it is on by
  default. Each level gets a small colour mark on its rows. The choices are kept in `localStorage`, and the search and
  agent filter keep working with them.
- **At most a page at a time.** Show the newest 30 rows that match, with "Show 30 more" and a count of what is left,
  and "Show all" when more than 30 are left. Older rows come from `api/messages?before=…` as now. The newest are at the
  bottom; opening the page or a new message scrolls nothing the user is reading.

### 2. Every other long list is capped too

Recent activity of an agent, the checklist, earlier runs, audit records and the talk panel's history: show the newest
10–20, then "Show more". Nothing on the page grows without bound.

### 3. Layout

- A slim header: run name, a status dot with the run's state, then *Talk* as the main button, a theme icon, and a
  "⋯" menu with Night mode, Earlier runs, More and Stop agents (Stop agents asks before it acts, as now).
- On wide screens two columns: left (wide) the milestone strip and then the messages; right (narrow, sticky) the team
  as compact rows, manager first, then critic, workers, monitor. One column on a phone, with nothing running off the
  edge at 360 px.
- A team row: a status dot (writing pulses, waiting for a job is amber, failed is red, closed is grey), the name,
  the role only when it differs from the name, the short model name (`deepseek-v4-pro`, not the full path; the
  backups only in the model window), last activity. Talk and Model as small buttons; Close, Delete and Stop in that agent's "⋯"
  menu. Delete is red only inside the menu.
- The terminal command takes no line of its own: one small underlined link-style button, "Open in terminal", that
  copies `orch terminal <project> <agent>` and says so in a toast ("Copied: orch terminal s-live manager. Paste it
  in a terminal."). Its tooltip shows the command. `test_UI_3_terminal_command_is_shown_to_copy` keeps passing, so
  change it only as far as the command now sits in the tooltip and the copied text.
- The same rule for every small extra: a line of its own only when it carries something to read, otherwise a link-style
  button.
- *Needs you* appears only when a gate is open or a question waits for the user: one card at the top, its options as
  buttons. Notices (milestones done, a model switched) become a toast plus a marked row in the messages, not yellow
  banners. Keep the connection-lost banner.

### 4. Style

- Fewer borders: tinted surfaces and spacing instead of boxes in boxes.
- A type scale of about 22 / 15 / 13 px; section titles in normal case, not tiny capitals.
- One accent colour plus the three level colours, each checked for contrast in light and dark (WCAG AA for text).
- A softer dark mode in which the warning colours do not glare.
- Keep `prefers-reduced-motion`, keyboard focus rings, 44 px touch targets on touch screens, and 16 px inputs on
  phones.

### 5. What the page shows wrong (check, then fix in the page)

- The worker's backup is shown as the same model as its current one.
- The manager still shows "discussing" after its terminal closed.
- The worker's "Task" is old text from an earlier message.
- "Needs you" counts questions that were already answered.

If one of these is wrong in the data the runtime sends, not in the page, do not fix the runtime: list it in the report
with what you saw.

## Done when

- The four screenshots (wide, full height, phone, dark) show the new layout. Save them under `/tmp/u-*.png` and name
  them in the report.
- Each item above works on the s-live run: the level filters and the Orch notes switch, paging, the input of a turn,
  what the recipient received, the whole reply, and the "⋯" menus. Every label is understandable without this file.
- `tests/test_rt_k.py` still passes and covers the new pieces of `logic.js`: the level of a message, the cap and
  paging, and the short model name.
- The full suite passes.

## Report

(append here: what changed, the screenshots, anything left over, and any data bug found in item 5)

### Dashboard update

- The timeline opens on the newest 30 matching messages in chronological order. Interaction levels and Orch notes are saved locally and work with text search and the agent filter. Older pages use the sequence cursor and show an exact remaining count. Rows distinguish messages to the user, messages waiting for an answer, and Orch notices.
- Message details show Markdown and load the writing turn, its system instructions, and the exact delivered text on demand. The writing model and input/output character counts come from the recorded turn. Open rows and panels survive state refreshes.
- A slim header, compact team rows, and action menus keep milestones and work visible. The terminal link copies the command. Closed gates are omitted from Needs you, and informational notices appear in the timeline and as toasts when fresh.
- Checklist items, talk history, activity, earlier runs and audit records start at 15 entries. Older audit replies and activity pages load on demand. Earlier runs open read-only.
- `board.py` supplies exact filtered message counts and per-turn summary metadata, the newest and previous pages of activity, and an audit history index. The existing responses remain available without the new query options.

### Finished-run checks

The headless Chrome check used the supplied `s-live` run with write requests blocked. It verified the 30-of-47 initial page, older paging, level and notes filters, search with the agent filter, lazy input/system/whole-reply panels, exact delivered text, action menus, and both audit replies. The page fit a 360 px viewport with 16 px inputs and 44 px touch targets. The interaction-label colours measured at least 6.16:1 on the light filter background and 6.89:1 on the dark filter background. The local server was stopped after verification.

- Before, 1440 × 900 — `/tmp/u-before.png`
- Desktop, 1440 × 900 — `/tmp/u-1440.png`
- Full-height desktop, 1440 × 1430 — `/tmp/u-full.png`
- Phone, 400 × 1120 — `/tmp/u-phone.png`
- Dark, 1440 × 900 — `/tmp/u-dark.png`

### Saved-state findings

- The worker’s active choice really is the named `deepseek-v4-pro` backup. The team shows the short reported model once. The model window identifies the selected choice by backend, provider, model, effort and name, and marks it in use.
- The manager has a saved open discussion with `by_terminal: true` and no open terminal. The page labels it “Terminal discussion pending”. The discussion remains available to apply or end.
- The worker’s saved assignment still points to its earlier test-run instruction, even after the final report and finish. The page labels it “Last assignment” when a final report is outstanding or the run is finished.
- All six saved gates are closed. Needs you previously treated answered gates without a recorded `sent_in` as actionable. It now appears only for open gates.

### Tests

- `python3 tests/run.py rt_k rt_j rt_u` — 91 passed.
- `python3 tests/run.py` — 781 tests, 775 passed, 6 skipped, no failures or errors.
