# Collaboration preferences

How I want the agents (above all the manager) to work with me. From the ICLR 2027 run: chat, gate notes, answers
to questions, preference files. Each line is stated by me unless marked *(inferred)*: please confirm or delete
those. Dates are chat times (`11-15 21:03`).

## Talking to me

- Short. Lead with the answer; a few lines or a small table. "You output too much text … That is a burden."
  *(11-13 15:40)*
- Self-contained: assume I've forgotten the context, and remind me in plain words what each experiment is.
  *(11-11 21:08; 11-15 19:42)*
- Complete in one message; answer the obvious follow-up too. *(11-15 19:42)*
- Plain names, no internal codes (E4, Part B, [R1]). *(11-12 13:13)*
- One sentence when I ask for one sentence. *(11-14 02:31; 11-15 12:22)* [No need to record as a preference.]
- Check the real status before answering. *(11-15 21:22)* [No need.]
- Walk through results group by group (one logical story each): purpose, result, claim, direction.
  *(11-13 14:33, 14:38)* [This is one of the important thing. The agent should report to me first in general how many experiments, and what are they doing. And it's not good to unfold the details of each one, which is look like a long report, and it's a burden to read them all. I think it can use a communication preference that firstly generally say what have been done. In a plain and natural language, and in the end, it can prompt to is the user use this language such as, would you like me to tell more about xxx? or continue to xxxx"]
- For each point: supported yes / partly / no, one table, next action with cost. *(pref 11-16)* [This is a Rebattle-specific point, and I think it can expand to the research activity. It should say the conclusion or verdict, which I have said before in the lessons.md comments. And the rubric could be How our purpose has been Achieved.]
- Long reviews go in a Markdown file I can comment in, with conclusions next to the tables. *(11-15 21:26, 21:31)* [I think it is one of the methods for communication or discussion. I think we can support it on the dashboard. When I click the button, the manager could generate a Markdown file, and I can go into it and comment. But this Markdown file should be strictly written based on the human review instruction and should be concise and self-contained And easy to understand.]
- Show progress and an ETA without being asked. *(inferred: I asked about 20 times)* [I think solved by the critic check-in.]

## Decisions and autonomy

- Decide from my preferences; ask me only what is mine: money beyond the cap, authors' time, reversing a decision.
  *(11-15 20:15)* [Resource cap should be one, and it should not focus too much on it. Author's time is not one of them. Revising a direction, I think the manager should strictly prove that the direction is wrong. But normally, when we choose a direction, there should be some solid conclusion and rigorous evidence to prove it is wrong. And the manager should be self motivated. Once see a fact that is opposite or negative, it should Try to think of the deep reason for fixing it before changing direction, And before you even reach out to me. Let's define it as a research guidance.]
- "You can decide what to check." "Both are ok, you decide." *(11-12 17:11, 17:27)* [The point is not meaningful.]
- Ask with options and a recommendation; I usually take the recommendation. *(inferred: "A", "as
  recommended", "I agree with your two Recommended options")* [This is fine. No meaning here.]
- While I'm discussing, send nothing to the other agents. I'll say "discussion over" or "suspend".
  *(11-15 21:03, 22:09; 11-16 00:11)* [This could be made sure by the discussion mode.]
- Night mode: act on my behalf, aim to surprise me, record every decision for the morning. *(11-16 02:45; 11-15
  12:45)* [Aim to surprise me is not a good expression. Use the research guidance I said before.]
- Stay in scope: no edits outside the task (paper, figures). *(11-15 21:52)* [I think we remove it.]
- Don't re-check what I say is correct (e.g. the authors' labels); keep working on the task. *(11-09 18:06; gate 11-11)*
- Work from my guidance, not your own agenda. *(11-16 02:40)* [I think it's not a good expression.Not a good instruction.]

## Money

- Budgets as caps, raised when the work needs it ($100 → $150 → $200). *(11-14 02:33; gate 11-16)*
- Expensive models for small runs; ask before a large run with them. *(11-14 02:32)*
- The local Claude Code CLI isn't counted against the budget, but use it with moderation. *(11-16 02:49)*
- Don't account to the cent; focus on the work. *(11-16 13:03)*

## Working toward the goal

- Goal first. For the rebuttal: we found flaws and fixed them. Never conclude that the method doesn't work. *(11-12 13:13)*
- A result against the claim: first verify it's correct, then find the cause in real cases, then fix data or
  mechanism; change the method last. *(11-12 13:13; 11-15 12:54)*
- Find the reason before proposing experiments; justify each new one. *(11-15 23:52)*
- Don't over-check results in our favour. *(11-16 11:15)*
- An exploration task means trying many approaches, not one quick attempt. *(11-16 02:40)*
- Comprehensive experiments over arguments; tables show the effort. *(goal)*
- Components that aren't the novelty can be swapped, even if tables must be redone. *(11-13 14:46)*
- An intuitively obvious claim needs no experiment. *(11-13 15:40)*
- Automatic LLM labels validated against a stronger model; human labels only as an option. *(gate 11-15; pref
  11-16)*
- Speed: run independent jobs in parallel; plan to the deadline. *(11-15 12:29)*

[Mostly are rebuttal specific points.]

## Gatekeeping

- The manager holds the gate. Low-level problems shouldn't reach me. *(11-15 01:09; 11-16 02:00)*
- Gate on substance (does the table support the claim, is the score good enough), not cell matching.
  *(11-16 02:02)*
- Check every number and its setting against the source before showing it. *(pref 11-15)* [I think we don't let the manager check it in a normal situation. It's mostly the critics' job.]
- Watch processes and logs, not the worker's report. *(11-15 19:58, 20:24)* [Not only the workers report. It has the rights to watch progresses and logs. I mean the manager.]
- Before saying "done", check that all my earlier directions were addressed. *(11-13 15:50, 20:19)*
- Write my preferences down and restate them in every instruction. *(11-15 01:02)*

## What frustrates me

- Too much text; replies I must work to understand. *(11-13 15:40; 11-16 00:14)*
- Being asked what the system could decide itself. *(11-15 20:15)*
- Teaching the same rule twice. *(UEGh comments)*
- An endless list of experiments. *(11-15 23:52)*
- Messages lost between agents. *(11-12 15:32, 15:48)*
- Agents talking to each other while I'm discussing. *(11-15 21:03)*
- Obvious mistakes reaching me. *(11-15 19:51; 11-16 02:00)*

## ICLR decisions (examples, not rules)

- Labels: authors → LLM labellers → authors allowed again as an option. *(gate 11-08; 11-12; 11-16)*
- Only models from the paper; Opus through the local CLI. *(goal; 11-15 13:06)*
- Budget $100 → $150 → $200; final experiment spend $122.52.
- No paper edits during the rebuttal; my paper-revision ideas kept for later. *(11-15 21:52, 22:05)*
- Key Finding 1 stays as one context sentence in §4.1. *(goal)*
