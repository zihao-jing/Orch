# Lessons from building and running orch

General lessons for any manager–critic–worker run. Examples come from the ICLR 2027 rebuttal run
(`ICLR2027/20261108-153043`: 272 rounds, 967 turns, Nov 8–16 2026).

Status: ✅ fixed · ◐ partly · ⏳ open. Source: commit hash, turn id (`r161-t01`), chat date (`11-15 21:03`),
`ins#` = an entry from the earlier audit-insights doc (now merged here), `aud` = reported by the auditor and not
checked against the records. Auditor points (rounds 35–270) are kept only when they generalise; turn ids without
`aud` were checked. Dropped points: end of §7.

## 1. Design intent

- **Roles split by point of view, not by function.** [I think it's not strictly not by function. For example, the manager's function is to understand users' query and guide the critic based on the preference.]
  - Manager: the user's view. It holds preferences, reconciles requirements, decides as the user would, and guides
    the critic.
  - Critic: splits the job into steps and reviews each one.
  - Worker: does one step, with a small context. [So probably now we need a setting that, If the critic has passed a step, use a new session for a worker. But how to Retain useful context the project needs to think.] [And I'm thinking here: we automatically let the critic guide multiple workers if the steps are not serial, To parallel it do some works At the same time to increase the efficiency. By the action of assigning how many workers, I don't think Critic is responsible for that. This should think a way to Implement. For example, the critic may propose how many worker Could be start at the same time. And the manager or user could approve it. And the difficulties become how to manager the workers, whether it is viable, efficient and reliable to let multiple workers communicate to one specific critic, Or find a better way for one-to-more communicating. Another difficulty is how to display this kind of communication on the dashboard.]
- **Purpose: less review work for the user, and stretches with no user at all.** [Less user instead of no user, The general direction should be guided by the user, I think. It is Inevitable, and our targets are to reduce the user load for reviewing and deciding. They are nearly identical because Decision is easy to make, but understanding the output and reviewing the model's output is Quite a burden.] Every lesson below is judged by
  that.
- **Main measure: does the manager's verdict match the user's?** Baseline: 7 of 13 milestone gates were sent back
  after the manager had accepted. [It is normal. Like human collaboration, there are some meetings to align with the target and goal. It cannot be making the very right direction decision without aligning with the human. So the core thing is to discuss and make sure the manager's function is working, or we say the manager is doing its job.It reviews the current progress and checks whether it matches the user's goal And preference. And of course, one of the important things is to define the user's preference or guidance or goal..]

## 2. Role boundaries and drift

- **Each agent knows only its counterpart.** An agent told about the whole team writes about the team. *(72ec1fe)* ✅
- [This is not essentially correct. For example, the worker should only know itself, It is assuming the critic is the user. And the manager generally knows about the worker instead of only critic. This is what I found during the usage. So, it's fine to let the manager know about the worker. And even sometimes directly talk to the worker In very few situations.] 
- **The critic passes on instructions as its own**, without saying where they came from. *(72ec1fe)* ✅
[Yes, the critic need to let the workers know it is the user. And never mind mentioning the manager, the organization system, or the critic, the worker, this kind of thing. But I noticed that the critic very often says “worker” directly in the instruction.]
- **The manager relays instead of judging.** It passed reports on without reading the deliverable as the user
  would. User: "You didn't hold the gate for me, it's your job." *(11-16 02:00)* ⏳ prompt only [But the manager should see at least several files and understand the basic experiment and its result. Also, it should see the critics checklist, compare it with our general goal and the manager's own milestone list, to see whether this aligns with our goal and whether we are on the good pace and track. But it should be in a limit view, for example, only see the logs, starting scripts, reports Or markdown files. I think the manager should have the power to decide what kind of files it should see. But for intermediate results, the scripts that the worker wrote should not be reviewed by the manager. This is not a strict rule. The manager should still have some exceptions to directly read detailed files if something strange or wrong is found. This should be slightly carefully designed. I guess mainly it's it's system prompt, or we can use a method in theintermediate terms, such as other system reminder.I'm not saying we have to use this system reminder. I mean this is one of the Approaches.]
- **The manager forwards to the critic while the user is still discussing.** User: "why … talk to critic every
  time, I am discussing with you." *(11-15 21:03)* Rule: collect during a discussion, send once the user closes it.
  ⏳ preference only [Probably here we can set the discussion mode with the manager. And if the discussion mode is ended, I can, or the system can, let the Manager to organize the information and then send it to the critic in a batch. Afterwards, the critic should understand the direction change and the new instructions, then decide whether to interrupt the workers' current job.]
- **Higher levels trust the reports of lower levels.** User: monitor processes and logs, not the worker's report
  *(11-15 19:58)*. A call count of 1,667 against 1,180 planned went unexplained *(11-15 20:24)*. ⏳ [What is the function of the critic? It should find the worker's problems. So maybe trust is a problem Against our design purpose.]
- **Agents assume orch can do things it can't.** The manager promised a hand-over on resume; orch gives it no turn
  there *(r148 chat)*. Rule: each agent gets a model of orch's control flow. ⏳
[I actually don't agree with this point. I think We should use this way to make the Orch robust. The Orch Should scan the newer messages to see whether there is a label indicating communication or message passing, I think this way could fix these kind of problems. The same during the quick message, when I can talk to the critic or worker. They will reply right? And in their reply, they are allowed to send messages to other agents. And the Orch It doesn't have to wait for its turn and can directly scan the new message to see whether there is a message passing tag.]
- **Agents police trusted inputs.** They re-validated the authors' labels. User: "They are not police, they are
  workers" *(11-09 18:06; gate 11-11)*. Rule: input the user marks as trusted is not audited again.
[This point is that when doing some experiments or framing some claims, the worker is checking whether it is correct all the time instead of doing the task. For example, it is checking whether the timestamp of the file or the hash of the file is identical. And sometimes this involves checking the cost over and over again. Which is waste of time and resource. I mean this kind of thing should be in the system prompt of the worker or the critic. This is a general fix for agents not only in the system. If we fix the system prompt, I think instruction following enhancement is one of them. Also, something like: do not write like defensive sentences; should have a conclusion or verdict instead of descriptive of the fact. Probably this kind of thing is in the writing Preference.]
- **The manager bypassed the critic.** From the board chat it addressed a note straight to the worker. That turn
  ran 6,743 s and cost $33.53. *(board note 11-13 15:54 → r102-t01)* Rule: the manager writes only to the critic. ⏳ [I said in a very specific situation, the manager can directly send a message to a worker. But the system prompt should not prompt the manger to do it.]
- **The manager's chat with the user leaked into the critic's prompt**, as much as 45% of one critic input.
  *(aud r078, r103)* ⏳
[And sure, this is a crucial problem. The manager should never disclose that there is a user, and should let the critic believe that the manager is the user itself. The same communication protocol should be used between the critic and the worker.]
- **The worker drifts into scope work**: judging completeness, listing open work. *(aud r251)* ⏳
- **The critic escalates routine questions**, and the manager answers at 10–25 times the question's length.
  *(aud r216–220)* ⏳
[I think it's fine if the critic cannot decide the routine questions. The key point is to reduce the communication length by throwing out and answering a question. I saw that there are a lot of long responses, either to answer a question or to throw out a question. This communication budget should be controlled.The model can think inside its thinking, but the response should be short. Messages to the other agent for answering a question or other things should also be short. Also, I found that it's very hard to get the model to keep concise only with a system prompt. Sometimes we need to remind the model every time to make them concise and save the output length. So, whether it is boundary control or length control, I think a length reminder should be added. I don't mean there is a specific cap or limitation for the response, but at least we need to prompt the model to be concise.]
- **Clear roles depend on the reader**: what reaches the manager is not what reaches the user. Check-in acks were
  mislabelled, so the manager mixed requests for the user into them. *(93d15a5)* ✅
[Not exactly. The content in the message labels is the message sent to a known role of an agent. Let's define a 'role view' of each agent. Each agent should have a default user view, Let me explain, It should assume one interface is the user, This is one of the foundations for building the system. You need to keep this in mind. The 'role view' Is the set that the agent can see and communicate. But they are not the default user view. Let's take an example. The manager's default is me, and I am the user view of the manager. The manager's role view is a set that includes the worker and the critic. For the critic, the default view is the manager, and the role view is the worker, or workers if we have multiple ones. This is one of the most important logics of the system design. Please understand this requirement in future development.]

## 3. Communication

- **Sent ≠ received.** Check the recipient's next prompt, not the sender's output. *(ins#3)* ✅ [We can label “sent” as “received” if a message is hold by the orch; we label it as “hold”.]
- **Show delivery status where the message was written.** Fold, don't remove. *(ins#4)* ✅
- **Validate every address.** The critic sent instructions to itself 4 times. *(ins#5)* ✅ [In the communication prompt, I mean the protocol System prompt: it should let the agent know what or who it can communicate to, so it never chooses the wrong one.]
- **Tags with a start and an end, not line markers.** An untagged `## Checklist` leaked 40 lines to the worker and
  emptied the PASS gate. *(0bc9eb8, b03dd60)* ✅
- **At every junction (gate, retry, restart, phase change), deliver, merge or supersede what is queued.** The wait
  gate's message reached only the critic, and the manager then contradicted it. *(45b909f, ed8ee6b)* ✅
- **Never truncate instructions.** A length cap cut the critic's instructions to the worker to 27–65%, while the
  worker's reports passed whole. *(aud r069–r110; caps removed in 5a9225f)* ✅
- **Every hop restated the message.** A 10.9k-character manager message became 6k of critic text, then 3.8k of
  worker text, within four minutes. Bound the size; pass a pointer. *(r149-t03; aud)* ⏳[I don't think we should bound the size. We need to see why there is a 10K message for a critic. And why is nearly 4K of math workers' input from the critic. As we assume the lower layer agent is collaborating with the real user, it is not really possible to pass a very long text every time. The foundation of simulating the real user is also to reduce this kind of problem.]
- **When orch holds back part of a message, log why.** orch keeps some parts of a reply from the next agent on
  purpose (for example the critic's checklist). The log showed only how much was held back, so a deliberate hold
  looked the same as a lost message. *(aud r141)* ⏳ [The checklist is, by design, not passed to the worker. Some of the other message blocks are also designed to prevent information leakage. Or extract different parts of the message and pass them to a different role. This kind of situation is not required to be locked.]
- **A corrected instruction should replace the old one.** When the critic corrected its instruction after a manager
  answer, the worker received both versions. *(aud r165, r172)* ⏳ [I think this point means that the worker previously received instruction, and the critic follows up with instruction. If in this situation, the critic should say that the previous requirement is declined, and we now are going to do what?]
- **Urgent notes shouldn't wait in a queue.** A note waits for the recipient's next turn: once 98–103 minutes,
  including "the machine restarted, resume from step 48". Such notes should wake the agent. *(aud r267-t03)*
  ◐ an "urgent" option exists [I think the note passing should be redesigned. The system should scan the new messages to find out what is an Added message that needs to be passed to any agent. instead of waiting for its next turn. A turn is an activation for an agent, and a message is the same for that.]
- **A question must always reach the agent it is addressed to.** orch strips some parts of a reply before passing
  it on, and some questions were stripped with them. *(aud r258)* ◐ fixed for questions inside a note (81798a7)
[Those questions or his replies should be fixed. I think it's mostly about the communication protocol.]
- **The delivery log must match what the agent actually saw.** It said "0% delivered" for text that was in the
  recipient's prompt, which misled the auditor. *(aud r181–r262)* ⏳[Should be Fixed]
- **Every gate needs "send back".** The final review pause had only "Done, carry on", so notes couldn't be sent
  back. *(11-15)* ⏳[Sounds good.]

## 4. Context

- **Each agent keeps its own working detail; the user's preferences go to everyone.** Preferences passed manager →
  critic → worker lost detail on the way, and the user had to ask for them to be repeated in every instruction.
  *(pref 11-15)* ◐ per-agent preference files *(5a9225f)*
[I think the preference sometimes could be a checklist item. I mean in the direction, not actually a checklist should be implemented. I mean the manager should based on the user's preference to verify the output is matching the user's requirements or not. if preference can be seen by the whole project, it should be a project memory or preference. that they are the different things.Of course, we can implement them all. We can have a board for golden rules That all of the agents can see. And for something like the golden board, it should be in the prompt for each agent, I mean the system prompt. For example, after 10 turns, it should be reminded Through a system reminder.]
- **Write preferences to files, not only into a conversation.** When the manager's session restarted on 11-13,
  what it knew about the user but had never written down was lost. [Yes, preferences should be in the files. I know Claude Code has its own preferences managed. What we want is our own preferences, which can be calibrated by the user in a file. Just let you know they are different things.]
- **New worker session per milestone; restart from the top.** On a restart the manager briefs the critic and the
  critic briefs the worker, instead of each getting a long handover. Briefings shrank: manager 21k → 15k, critic
  26k → 4k, worker 31k → 0.2k characters. *(e61af99, 11ae9e1)* ✅ [Here, probably the critic decides whether to reset the worker After each step. But this should be carefully designed.]
- **Each piece of an agent's state has one writer and one moment it is updated.** *(4ba86c2)* ✅
- **Decisions live in one register, not in a chain of passed-on notes.** *(e61af99)* ✅ It grew to 64 entries
  (17.7k characters) by milestone 4 and filled the briefings, so old entries need pruning. *(ins#8)* ⏳
[Sure, I think we can probably merge the decisions into the preference, or into the global golden board of the preference for brevity. The manager can manage the golden board, and each agent can manage its own preference record.]
- **Don't repeat a fixed reminder every turn.** The same 555–1,010-character reminder went into every critic
  review. Send it once, or when it changes. *(ins#8; aud)* ⏳ [Yes, the system reminder should be dynamic for something that needs to be Reminded very often, we could set intervals. For example, the concise reminding can have a three-turn interval for communication.Sometimes we can implement a random choose, for example, random choose an integer from 3 to 10 to remind the protocol or the requirements like the concise communicating.]
- **Agents spent much of their output rewriting their own notes.** The critic rewrote its full plan and checklist
  every turn: 45–75% of its output. The plan grew 59% in 10 rounds. Send only the changes. *(aud r171–r189)* ⏳
[Yes, I think this is a crucial efficiency problem too. The checklist doesn't have to be updated every turn. I said it should be in some kind of reference board within the agent itself, and every time the agent only needs to edit it, that's fine. Instead of outputting the whole checklist every time, it looks dumb.And I agree that the checklist can be a separate board and not combined with the preference. I think they have different purposes.]
- **A milestone start shouldn't paste the whole history.** The opening message to the manager and critic carried
  22.9k characters of past rounds, including the worker's step text. *(aud r117)* ⏳
[Of course, optimize it. Sometimes the context can be consistent. We don't have to update the context of the manager, and the manager should guide the critic as usual to continue to do another thing. The instruction should be like human language natural instructing. It looks like the critic and the worker could be refreshed after milestone. The first input should include the system prompt, which is the same as what it is in the beginning. But inputting 20K characters of past rounds doesn't sound match this purpose.]
- **After a cut-off, say "continue"; don't resend.** A resumed session already has the last message, but orch sent
  it again in full. *(aud r113; r267)* ⏳ [Good point.]
- **Worker reports need a length guide.** An 803-character instruction got a 7,195-character report, which the
  critic read in full. A short report, with details in files. *(r123-t01)* ⏳ [Yes, and I guess this should not only be in the system prompt, but also controlled by the system reminder we design.]
- **Record why a session restarted.** A worker session restarted mid-run with no reason recorded. *(aud r132)* ⏳ [It can be recorded. For example, when we change a model or refresh for some reasons.]
- **Keep the system prompt stable.** A change makes the model pay full price to re-read the whole conversation.
  *(4ba86c2)* ✅ [Of course, basic point]
- **Similar manager turns cost very different amounts** ($2.11 vs $0.12), perhaps because orch's changing summary
  sits ahead of the cached history. *(aud r204)* ⏳ unverified [This should be verified to find out whether the system is changing the previous context, which will cause a decline in cache hits. This should be thought significantly to be save Budget. We usually regard this kind of problem as a bug instead of a weakness.]

## 5. Progress and efficiency

- **Long turns cause late notes, costly failures and no visible progress.** Worker turns ran 30–85 minutes. Fix:
  the critic hands out one step at a time. *(ins#6; bd0c73f)* ✅ [Yes, good direction. We allow the worker to run long and implement checking. For example, once an hour, the manager will check in the critic, and the critic can also check in the duration or progress of the worker. I think the critic can Have a quick check or review per hour to see the worker's progress. It doesn't have to send a message to the worker; it can check the log or running bash commands to see the current progress. And I think the manager's checking could use the previous logic. It doesn't have to follow my instruction of one check per hour. But the critic should have a general sense of the running progress. For example, a 20-minute check will be fine. But the check should save the context of the critic and should be a quick check instead of a full review. Could use some prompts like: “Please have a quick check of the current progress, Or how's the current running progress” Instead of reviewing the whole output and the result.]
- **Don't start a turn with nothing to do.** Worker r161-t01 ran the full 2-hour timeout to reply 78 characters:
  "Leftover timer". End a turn that has gone quiet instead of waiting for the timeout. ⏳ [And yes, I'm not favorable for the 2 hour limit for a turn, actually, as some of the task may be longer. and also, it's hard for the system itself to judge whether the turn is quiet. so, I think combine with the previous comment, the critic can analysis and judge whether the current progress should be paused, and it can throw a pause too.]
- **Don't start a turn from an empty message.** r066-t01 started from 17 characters, then the critic reviewed the
  result in full. ⏳[We should inspect why this happens, and if it is the starting problem, probably the starting point sometimes needs to be optimized.For example, after an interrupt, pause, or a question has been answered from the user gate, it should continue the whole pipeline. And what the correct start point is is what we need to consider.]
- **Long turns should report progress.** A 51-minute worker turn sent no signal to anyone. *(aud r124)* ⏳ 
[For example, the 20 minute critical checking could solve this problem. and Of course, the 20 minutes is a hyperparameter, which can be set from the agent's config. And checking doesn't mean having to send a message to the worker. The system can prompt the critic to see the progress. I think I have said this before.Use a short prompt to check progress, for example, the running log or progress percentage. Only if the critic finds that there is complete quiet, or the worker is not on the right direction, or even the Wacher is down, should it send the message to the worker.]
- **The agent tools have their own limits.** Claude Code stopped a background watcher after 10 minutes, inside a
  44-minute turn. *(aud r065)* [ See the last comment.]
- **Don't spend rounds asking "still running?"** For five rounds the worker replied only that a job was still
  running (r212–r216). orch's job wait should have held those rounds. ⏳
[I think we replace the system's progress pausing or waiting with critical check-in. It usually waits for an unrelated progress. To solve this problem, We can replace the waiting with the critic checking. Let the critic check whether the current progress is still running and on the right direction. On the dashboard, it can show the running progress Or probably better with its log. But don't hold the whole pipeline for that.]
- **Pause work that depends on a question to the user.** Rounds kept going while a question waited for the user,Panda
  so that work might be thrown away. *(aud r161–r165)* ⏳ [I think in night mode, the system should be running automatically. If there is a question, the manager could give a recommendation and act based on the recommendation. If not in night mode, and the manager decides to ask the user instead of deciding itself, we should pause the work here And the user, AKA I, should answer the question. For a pause without a question, the critic can propose a pause, and the manager can decide a pause.]
- **Don't wake a higher level that has nothing to decide.** Check-ins now come from the critic's plan, with no
  manager turn. *(ins#7; bd0c73f)* ✅ [Sounds good.]
- **A manager answer costs an extra critic turn.** After each manager pause or answer, the critic spends a turn
  rewriting its instruction. *(aud r256, r262)* ⏳ [I think it's partially as expected, if a crucial question has been answered, Agents in the pipeline should use the new instruction to adjust their preferences, or even provide a new instruction to the worker.]
- **Parallel by default.** The user had to ask twice to "run all possible jobs in parallel" *(11-15 12:29,
  14:42)*. The critic should plan which jobs depend on others and run the rest at once. ⏳ [If the preference is running fast, and this kind of requirement has already been written into the preference, then yes, we should use parallel running . The worker should explore parallel running by default. If the worker is not sure about the topology of the work, he should ask the critic or the manager with a broader view. Yes, another setting I want to add to the system is multiple workers. Think about how this can be designed. I have an idea. For example, the critic can propose multiple workers, and the manager can propose it too. If it's inside a budget, the manager can automatically decide and approve the multiple workers; otherwise, it should be pushed to me.]
- **Find the cause before adding work.** User: "endless experiments … find reason first instead [of] list
  experiments" *(11-15 23:52)*. [This is a preference. When we set a goal, the manager should guide the system. If the experiment result is not satisfied, firstly, go deeper and deeper to find the reason. The deeper the agent goes, the easier it is to find the fix for the experiment or further experiment direction. That's the meaning or requirement for the manager. It should not only summarize the fact, but also when the fact is against our goal or target, it should think of how to fix it and find a way to do so. For example, first suspect the experiment results. Especially, the experiments for large language models should go deeper to see the response of models and compare their logs directly. This should be done by critics and workers instead of managers. Of course, if it is still not satisfactory, managers can also do these things themselves. This is a preference of exploration or experiments and research. And I think this is also a point the manager should learn: instead of trusting all the results, they should find any caveats and the hidden reason behind a fact or result, which can guide to the right direction in the future.]
- **Don't over-check results in our favour** unless something looks badly wrong. *(11-16 11:15)* [I think it's not a surface requirement. It's the deep meaning I said in the last comment.]
- **Only the worker's own processes count as its jobs.** System programs and other people's processes in the
  project folder were taken for its jobs and held up reviews for hours. Each process the worker starts is now
  tagged. *(40d6768, 080906c, 4e58615)* ✅ [I think the redesign of waiting for progress is solving this issue.]
- **The user's own Claude hooks can slow every turn.** A terminal hook added about 4.5 min per turn. Fix: an option
  to turn hooks off for agents. *(45b909f)* ✅ [Confirm resolved.]

## 6. The human interface

- **Questions to the user stand on their own**: the decision, why it matters, options with cost, a recommendation.
  *(0ed5474)* ✅ Even so, the user once had to ask "What does the current question mean?" *(11-08 17:20)*
[I think the point is to let the manager communicate with the user using a specific expression style. It should be self-contained with the context of each experiment, instead of only containing experiment code Which is used in the critics report or the log name. Sometimes, assume the user forgets the context of the project. And also need to keep concise. So here we found another communication preference to the real user. I think all the agents should lean towards using this style to Communicate their Default view (the user they believe)]
- **"Settle it" lost the user's typed answer**, so the manager settled on its own guess. *(45b909f, Q38)* ✅
- **Night mode**: the manager decides alone and every decision is recorded for the morning. *(bd0c73f)* It settled
  22 decisions and 19 questions. Whether the user later overruled them isn't tracked. ⏳ [It is fine.]
- **A switch should show what the user last set**, even before the run applies it. Otherwise a click looks
  ignored. *(f05b176)* ✅ [Yes, I think the switch should be smooth and functional. Things like switching to night mode should not happen throughout a question.]
- **Show progress without being asked.** The user asked for status or a finish time about 20 times. ⏳[Critics checking could solve this.]
- **Make each review cheap.** The final gate asked the user to read three full files and confirm six night
  decisions. Show what changed, what was checked, and where to look. ⏳ [I think it is the communication preference. Systematic miner should Include this too.]
- **Ways to reach the manager**: board chat or `orch tell`. Opening its session with `claude --resume` also works,
  but what is agreed there doesn't reach the other agents. *(11-16)* [III think the system should scan this kind of augmented messages, and if there is a message to other agents generated by the manager inside the interactive session the user opened. I guess this could be implemented by Claude Code hook. We found that the hook may delay the process, so we removed several hooks from terminal tool. If the hook works here, we can use the hook Or other equivalent tools.]

## 7. Checking quality

- **A critic PASS doesn't mean the user is satisfied.** The user sent back 7 of 13 milestones. After the critic
  passed the final files, the manager found 26 more problems while reading them with the user. *(r148 chat)*
[So I think here we should have a milestone fallback. I didn't verify that, but I think the milestone pass may mislead the manager to think about what the current status is. But I think it's fine. The manager knows after discussing with me. We can implement a lightweight milestone fallback mainly for the dashboard display. And it should match with the checklist, And the managers context. I don't think it is needed to give the manager additional prompt when switching back to milestone. Why do we do this? Because sometimes the user passes the milestone but later finds more problems.]
- **PASS only when every checklist item is ticked.** *(b03dd60)* ✅ Checklists grew to about 60 items; now capped
  at about 25. *(note 11-08)* ✅ [So, I think a two-level checklist is needed. We don't have to name it checklist both. One is the steps that the critic splits the manager's instruction, and the steps are the batch of requests the critic sent to the worker. To be specific, at the first step, the critic will pass a small batch of work from the whole milestone to the worker. I think you will know what I mean by the step. And the critic can manage a checklist for this step and judge workers' output based on that. And I prefer the checklist to be a board (.md) Instead of inside each output. And a critic can edit it.]
- **The manager's gate checks substance**: does each table support its claim, and is the score good enough? It is
  not a cell-by-cell check against sources. *(11-16 02:02)* [Yes, I think currently there are always 5 rubrics for passing a milestone. Which is a bit general and not specific. I think managers should set this kind of rubric based on the task itself, and they can decide how many rubrics to set. For example, we can set them ranging from 3 to 8. And for example, in the rebuttal, it can set a rubric such as whether each concern is strongly supported.]
- **The user's spot check found problems in 2 of 2 tables.** *(11-16 02:00)* [This is the performance of manager problem. We need to combine the ways I said before to improve their performance and do their work. Including the system reminder. As model may forget things.]
- **An auditor reports the design as a bug unless it is told the design.** 4 of its 12 findings were false. For
  example, it called the checklist, kept from the worker on purpose, lost text. *(ins#10)* ✅ [We can add the system's design logic into the auditor's system prompt. And I think a false positive rate of 4/12 is acceptable.]
- **Check audit findings before acting on them.** The auditor said orch's own text started worker turns. In
  r202-t03 that text was the "your job has finished" message, which is by design. *(ins#11)* [Fine]
- **orch's automatic checks read their own reminders as the question.** The short-reply check took the night-mode
  reminder at the end of a message for the question, and flagged correct "nothing needs you" replies as failures.
  Remove reminders before checking. *(aud r107, r137)* ⏳[Not fully understand, but I think it's easy to fix.]
- **The auditor needs timestamps**: when each turn started, how long notes waited, when questions opened and
  closed. Without them it can't back up timing claims. *(aud r049, r267)* ⏳ [Sounds good.]
- **Models from one family may share blind spots** (critic and worker are both Claude). ⏳ untested [I will control that.]
- **Auditor points dropped:**
  - "Spend far over the phase cap": wrong, built on estimated costs. *(ins#1, ins#10)*
  - "orch's text starts worker turns": partly wrong, see above. *(r202-t03)*
  - Size figures seen only once, or already covered above.

## 8. Reliability and cost

- **A cost meter is only as good as its price list.** Claude Code priced a non-Claude model at Claude rates: $565
  of a reported $701 was this estimate. Mark each cost as reported or estimated. *(ins#1; ed8ee6b)* ✅ [Good, and I think the system is overly focused on the costs when I'm running the ICLR project. The agent spent some time on the cost calculation. But I think that's mostly the API instead of the agent itself. For the agents themselves, I will monitor the cost myself. The system doesn't have to report them accurately.]
- **Record facts on each turn, not once at the start.** The start-up record still said Opus after 70 rounds on
  another model. *(ins#2)* ✅ [Yes, they should.]
- **Treat errors by type.** Temporary (overload, rate limit): retry or switch model. Account or billing: stop and
  tell the user. *(ins#9; 5a9225f)* ◐ [Sounds good. And I didn't verify the retry behaviors. I think the API should be retried, and the agent message should be retried to ensure system Continuously. And to resolve this, we also add a backup model Setting, and I think we can have a second backup model setting too.]
- **A retry must repeat the task.** An 85-minute turn failed with no output. The retry said only "Please carry on."
  and failed again. Retry with the task, after checking what was started and written. *(r084-t01, r085-t01)* ⏳ [I think this is the agent Message level retry, not the API retry inside Claude code. I think that retrying with the same session will save the cash budget, and it's fine to say only "please carry on." The crucial problem is that if a model fails, we shouldn't directly change to the backup. We should try it several times first, and then change to the backup. Or we can just set A model list for the agent, and use another key in the config to specify which model is being used. I have a better idea: we can set a model list in the orch project general setting, And in each agent, we just need to choose the ids, And backup IDs. They don't have to repeat model settings In each agent setting.]
- **A provider error was saved as the agent's reply**, and the whole message was resent. It is now recognised and
  retried properly. *(aud r058; 5a9225f)* ✅ [Basic problem.]
- **Save the agent's final answer, not the raw event stream.** Twice orch stored Claude Code's whole event stream
  as the reply: 5.7M characters (r111-t01, failed) and 6.5M (r202-t01, cut off). Keep only the final message and
  cap its size. ⏳[I think it's a bug to fix. Generally, I don't like to set a cap size. It should issue a warning if a size is out of range, and the warning should be displayed on the dashboard.]
- **Cut-off turns lose their model and cost** in the records. *(r267-t01)* ⏳[It's fine. The cost is not an issue.]
- **Only one orch process per run.** A lock stops a second `--resume` of the same run. *(ed8ee6b)* ✅
- **Agent settings reload before each turn** (model, backend, timeouts), with no restart. *(59dd7e6)* ✅ [When changing to model IDs, need to check whether the general model list is changed too. I want the system to be able to automatically change models without pause. And if a worker has changed the model, I guess the critic should be aware of it and send a corresponding instruction. But also should be aware of the Communication length. Only important and necessary things should be passed. And the communication should be like user interacting with the model. This can be ensured by the system reminder. I think I have mentioned this feature many times; it is an important one.]
- **Backup models.** When a turn fails, try the next model, and stay on it for 5 h before trying the main one
  again. *(f05b176)* ✅ [As previously said, try the same model several times (like1 min, 2min, 4min) if it still fails, Then change the model. And this should be shown on the dashboard too. The main principle is that the dashboard should know what is going on currently. I guess for the checking of the critic I said before, it is the same thing. After checking, I think a live message should be displayed on the dashboard to indicate what the current status of this working is.]
- **A 04:00 machine restart was survived**: the run resumed from its records and sessions. *(11-16)* ✅

## 9. Prompts vs code

- **A rule that keeps breaking as a prompt should become code.**
  - Worked as prompt rules: be brief; know only your counterpart.
  - Needed code:
    - checklist tag and PASS check;
    - address check;
    - delivery tracking;
    - job detection;
    - pause as a control;
    - preference files;
    - check-ins from the plan.
  - Broke repeatedly as prompt rules, so candidates for code:
    - "don't message the critic while the user is discussing";
    - "restate preferences in every instruction";
    - "be short and self-contained with the user" (asked 5+ times).
- **Each rule has a place.** Rules every project shares (who reads what, the tags) live in a fixed protocol
  (`orch/prompts/protocol/`). Role prompts and memory are set per project. *(9bf39ac)*
[I think we should record the points that have been broken repeatedly. This is valuable knowledge for large language model development. Currently, my solution is system reminder, and a reasonable usage of the system reminder. For example, randomly awake the reminder as we have implemented. Now, set the correct intervals for the system reminder, and the system reminder should be shortened enough and made concise enough.]

## 10. Scaling

- **Board traffic.** On a long run each refresh sent 4 MB, which choked an SSH tunnel. Now only changes are sent,
  compressed, at most once a second. *(8216809)* ✅
- **Run reports get big**: report.html reached 51 MB. ⏳
- **Long sessions cost more per turn**, because each turn re-reads the history. Use fresh sessions, a cheaper
  worker model, and low effort for the critic.
- **Shared lists only grow** (decisions, checklist, plan). Prune them, cap them, send only changes. [I guess the manager is responsible for managing it. This is the new logic of golden board for the project.]
- **One critic limits parallel work.** Several workers per critic not tried yet. ⏳ [Yes, the scaling is the direction we want to explore in the Current development. I think if we use the one-to-many communication protocol, a critic is managing multiple workers. It may not be good for a critic to send many messages directly to multiple workers. I guess it's a hard task for one large language model to remember all the workers and what they are doing. It may overwhelm its context. So probably there should be a board recording who is doing what. And the critic can read that board to know the index of the work and check the work. This may Let the critic Do too much things. The foundation of this system is that each agent should get a reasonable burden of work. So, how to scale is something we need to discuss and decide]
- **Two round limits.** `max_rounds` (whole run) must stay well above `milestone_rounds` (rounds without
  progress), or the run stops mid-milestone. Defaults 400 / 50. *(f1663ce)* [Yes, currently, the default relatively larger limits are good. We are exploring the system and trying to develop it.]

## 11. Baseline and open problems

| ICLR run | value |
|---|---|
| Rounds / turns | 272 / 967 (20 interrupted, 19 stopped, 4 errors) |
| Agent spend (orch meter) | $1,596, of which $552 estimated |
| Experiment spend | $122.52 |
| Decisions | 182: user 108, manager 52, night mode 22 |
| Questions | 170: critic → manager 66, worker → critic 65, manager → user 39 (19 settled in night mode) |
| Milestone gates sent back after the manager accepted | 7 / 13 |
| Manager reviews | 10 ACCEPT, 9 REVISE |
| User chat messages | 241 |

Open, by impact:
1. The manager matching the user: preferences written down, reviews from the user's side, a score for predicting
   the user's verdict at each gate. [The score for predicting feels too heavy. I think semantic decisions are fine For a strong model.]
2. Turns with nothing to do, and work done while a question waits for the user. [Then stop, or the manager can decide to do anything else.]
3. Text that repeats or keeps growing: fixed reminders, rewritten notes, resends after a cut-off.
4. Notes that wait for a turn; old instructions not replaced by their corrections. [Commented before]
5. Gates: send back everywhere, cheaper reviews, progress and finish time.
6. Saving only the final answer; retries that repeat the task; cost of cut-off turns.
7. Roles enforced by orch: the manager writes to the critic, and the user's chat stays out of the lower agents'
   prompts.

[I add something I can remind. For the Conversations between the user and each agent from the dashboard. The agent should assume this is from the user. And it is actually from the user. What I mean is, when I talk to the critic, the critic is assuming it is talking to the manager. And the critic itself doesn't know the messages from different people. I think you'll know my explaining. Different sources, but the critic should regard them as the same source.]

