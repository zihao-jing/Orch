# Writing preferences

From the ICLR 2027 rebuttal: chat, gate notes, four rounds of comments on drafts. Full rebuttal rules:
`ICLR2027/iclr2027_rebuttal/final/WRITING_GUIDE.md` (`G#` below). Raw comments: `iclr2027_rebuttal/notes/`.

## Conclusions

- A conclusion is a verdict that holds under a stated condition, not a walk through what was measured. *(11-15 01:15)*
- An observation or a list of facts is not a conclusion. *(G14; UEGh comments)*
- Answer the concern as asked; say what the result means for it. *(G14)*
- For a big concern: how much the problem matters, what the audit shows, which repair works. *(G15)*
- Two or three sentences for important concerns. *(G16)*
- For a weakness we accept: say so plainly, then give the fix. *(G18)*
- Never leave a problem on its own ("X did not fix it"); pair it with the fix. *(pref 11-13)*
- No caveats that weaken our own result ("not a repair", "only a ceiling"); say what the experiment is for. *(11-15 00:55)*
- Show only the final, correct result: no intermediate, superseded or "clean test" data. *(11-16 01:49)*

## Numbers

- Few numbers in prose; tables carry them. *(11-15 00:55)*
- One or two key figures at most, and only when they carry the verdict (the gain, a min or max). *(G17)*
- No case counts and no intervals in prose. *(G17; 11-15 01:15)*
- Percentages, not "nine times in ten". *(G29)*
- Every share says what it is a share of (e.g. "% of failures"). *(G11)*

## Tables

- Self-explaining: every row, column and term readable from the table, its label or caption. *(G8)*
- Percentages in cells; total cases in the caption. *(11-16 02:00)*
- Caption above, as a quote: `> **Table R1: title.** definitions, totals.` Nothing after the table. *(G23)*
- Captions short: total cases, models by name, needed definitions only. No breakdown of counts. *(G12)*
- Short column names, defined in the caption or the paragraph before. CI in its own column. *(G28)*
- A Δ column when a table compares before and after. *(G36)*
- No n/a-heavy tables: split them; status goes in the text, not a column. *(11-15 00:55)*
- Don't mix rows with different meanings (frequency vs intervention): two tables. *(G45)*
- A claim like "largest" needs the competing numbers in the table. *(G39)*
- ✅ / ❌ / – in capability tables. *(G27)*
- Mark our rows; baselines by method name, never "LLM judge" or "[R1]". *(G10)*

## Terms

- Use the paper's own terms; don't invent new ones ("garbled", "copied message"). *(G9)*
- One word per concept; don't reuse a word for two things. *(G47)*
- Say what an operation acts on before naming it (what "selection" selects). *(G46)*
- Every operation or failure names its pipeline stage (extraction, relay, aggregation). *(11-13 14:46)*
- Describe experiments in plain words, not internal codes (E4, Part B). *(11-12 13:13)*

## Structure

- Each section stands alone; each response stands alone. *(G19)*
- Section = concern restated → experiment tied to it → conclusion table. *(11-16 02:00)*
- Experiment as "we did X to find out Y": what was set up and compared, no hyperparameters. *(G7, G34)*
- Explain why each setting matters to the concern. *(G35)*
- Short labelled paragraphs: `` `Experiment setup.` `` before a table, finding labels after it. *(G24, G44)*
- Vary structure; "Conclusion:" only when it really is one; never "Verdict". *(G25; 11-16 02:43)*
- No one-sentence paragraphs; merge parallel findings. *(G48)*
- Bullets of one or two short sentences. A bold lead phrase per finding, in at most about half the sections. *(G53, G55)*
- Case examples compare both methods' real outputs, or are left out. *(G49)*
- Headings name the concern: `W1: <short phrase>`. *(G4)*

## Tone

- Natural openings that vary: "We agree…", "Thank you for pointing out…", "This is a fair point…". *(G5, G43)*
- Never "The reviewer asks…", "Concern:", "What we did:". *(G5; 11-16 02:43)*
- Restate the real concern; don't soften it into an easier question. *(G6)*
- Concede gently, never in the reviewer's harsh words. *(G30)*
- Confident: "we have revised", not "we will". *(G32)*
- Opening: thank for the strengths named, say in general what we fixed; no IDs or results. *(G1–G3, G31)*

## Length

- No length limit, but no sentence longer than it needs to be. *(G20)*
- Final pass: shorten every long sentence whose meaning survives. *(G54)*
- One claim, one table per section; drop delicate sub-arguments. *(pref 11-16)*

## Before → after

- Conclusion
  - Before: "Conclusion: removing any one check moves 6% to 33% of MuSiQue traces to another route, so each check is
    needed."
  - After: "`Every stage is needed.` Each check catches failures that no other check sees. Without it, up to a
    third of MuSiQue traces are blamed on the wrong stage or missed entirely."
  - *(chSu, before vs final)*
- Narration → verdict
  - Before: "Control answered 0/92; restore 13/92 = 14.1% [7.7, 23.0], paired +13 [7, 20]…"
  - After: "When the audit flags a distortion at the relay checkpoint, restoring the source sentence recovers answers
    that otherwise stay wrong."
  - *(manager chat 11-15)*
- Caption
  - Before: "82 runs: 41 failures with a known cause (30 corrupted messages, 11 deleted facts), each with 2 models.
    Intervals are 95% CIs. The lost-fact column counts the 53 runs…"
  - After: only the total number of runs and the two model names.
  - *(chSu; G12)*
- Opening
  - Before: "We will reorganise the paper as in point 1…"
  - After: thank the reviewer for the strengths they named, then say what kinds of fixes we made, with no IDs.
  - *(UEGh comments)*

## Self-check before showing the user

- Does each conclusion answer the concern as written?
- Any number repeated from the table? Any case count in prose?
- Any undefined term, code or invented word?
- Any caveat that weakens our own result?
- Any table with n/a cells or mixed meanings?
- Would a reader who never saw the review follow it?
- The user's note: "I might have repeatedly commented the same thing … you should be aware of it." Apply every
  rule everywhere, not only where it was commented. *(UEGh comments)*
