# Lesson material

Reference for the `usage-audit` skill. One entry per finding title in the report's "What to change" section. Each gives where to find the worst instance, the mechanism to teach, the arithmetic to show, a habit, a drill, and a check question with its answer.

The worst instance opens every lesson. Each finding's evidence list in the report ends with it, and the tables named below hold the runners-up.

Take rates from the report's own numbers or the learner's price table. The mechanisms below come from the Claude Code documentation; confirm commands against the learner's version before using them in a drill.

- Costs and context: https://code.claude.com/docs/en/costs.md
- Prompt caching: https://code.claude.com/docs/en/prompt-caching.md
- Subagents: https://code.claude.com/docs/en/sub-agents.md
- Model and effort: https://code.claude.com/docs/en/model-config.md
- Loops and scheduled tasks: https://code.claude.com/docs/en/scheduled-tasks.md

## Terms to define on first use

- **Context:** everything the model is sent on one call: instructions, tool definitions, and the whole conversation so far, including every tool result.
- **Call (request):** one round trip to the model. A single prompt that uses tools makes many calls, one after each batch of tool results.
- **Prompt cache:** the service remembers the unchanged front part of the context for a limited time and bills re-reading it at a small fraction of the normal input rate.
- **Cache lifetime:** how long the cache survives without use. It is either 5 minutes or 1 hour.
- **Subagent:** a separate Claude instance with its own context that does one delegated job and returns only its final report.
- **Compaction:** replacing the conversation so far with a summary, to shrink the context.

## Analogies

Build the analogy from the learner's own field, and always say where it breaks. Two examples for a network engineer, to show the shape:

- Context is like a header that every packet in a flow must carry, except this header grows with every exchange. Where it breaks: a cache makes the repeated part cheap, though never free.
- Cache lifetime behaves like a session-table idle timeout. Let the flow sit idle past it and the next packet pays full setup again. Where it breaks: the setup cost here scales with the size of the whole conversation.

---

## Most spend happens at very large context

**Worst instance.** "Costliest single turns" and "Costliest sessions": the prompt and the session with the highest cost and context.

**Mechanism.** Every call re-sends the entire context. Caching bills the repeated part at the cache-read rate, but it is billed on every call, and tool use multiplies calls. Cost per call scales with context size; cost per task scales with context size times number of calls. Output, the actual work, is often the smaller share.

**Arithmetic.** Context tokens × cache-read rate = cost of one call before any output. Show it for the session's median context, then for a new session's starting context. Multiply each by the session's request count.

**Habit.** When a task is finished or the subject changes, write a short handoff note and start a new session from it.

**Drill.** Run `/context` at the start of the next session and again an hour in. Name the three largest items. Then start one task today from a handoff note and compare.

**Check.** "You ask the same one-line question in a 400k session and in a 40k session. Which costs more, and by roughly how much?" Answer: the 400k one, about ten times more, because the whole context is re-read either way.

**Going further.** `/compact` with instructions on what to keep; `/rename` then `/resume` to find old sessions; moving long instructions out of always-loaded files into skills that load on demand.

## Idle sessions are resumed after their cache has expired

**Worst instance.** "Costliest cold resumes": one row per resume, with the idle time before it and what it cost.

**Mechanism.** The cache has a lifetime. After it lapses, the next call cannot read the context cheaply: the full context is processed again and written back to the cache at the cache-write rate, which is many times the read rate. The cost of one cold resume equals the size of the whole session, so large sessions are the expensive ones to leave and return to.

**Arithmetic.** Context tokens × cache-write rate = one cold resume. Compare with context tokens × cache-read rate for a warm call. Use the report's count of misses and its split by idle gap. If the report shows a large share of 5-minute writes, show that a short break is enough.

**Habit.** When stepping away from a large session for longer than the cache lifetime, close the task with a handoff note first. When returning to a cold large session, start a new one from the note.

**Drill.** Run `/usage` and read the prompt cache line: find the lifetime in effect, the number of misses, and whether the cache is warm now.

**Check.** "You return from lunch to a 300k session. Rank three options by cost: continue it, compact it, or start a new session from a handoff note." Answer: a new session is cheapest. Compacting costs one full-context request and is small afterwards. Continuing pays the full re-write and then keeps paying for 300k on every call.

**Going further.** Clearing a session costs nothing, while compacting a large one is itself a large request.

## Sessions stay open for many hours

Teach this with the two lessons above; it is the same habit seen from the calendar. A session is the unit of one task. The report's span and active-time columns show the gap: long span with little activity means the session was left open and resumed cold.

**Check.** "A session spans 90 hours with 6 hours of activity. Which two costs does that pattern produce?" Answer: large context on every call, and cold resumes after each idle stretch.

## Subagents run on expensive models and grow large

**Worst instance.** "Costliest subagent threads": the thread with the highest cost, its model, and how large its context grew.

**Mechanism.** A subagent keeps noisy work out of the main context, which is good. It also makes its own calls on its own context, and those are billed like any others. It starts with tens of thousands of tokens before doing anything, grows as it works, and uses the session's model unless the launch names another. A subagent on a cheaper model can still be costly if its task is broad enough that its context grows large.

**Arithmetic.** From the subagent table: cost per thread by model, side by side. Then the same number of threads at the cheapest model's average. Point at the maximum context column for threads that grew.

**Habit.** When delegating, name the model and give one narrow task with a stated stopping point and the form the answer should take.

**Drill.** Launch the next delegation with an explicit model and a one-paragraph brief. Afterwards, find that thread's row in the next report.

**Check.** "Why can a subagent on a cheaper model still cost more than doing the work in the main session?" Answer: it pays its own startup context and makes many calls; if its context grows large, the cheaper rate is multiplied by a lot of tokens.

**Going further.** A default model for all subagents; custom subagent definitions with a fixed model.

## Background work keeps waking a large session

**Worst instance.** "Costliest single turns", rows whose origin is not `human`.

**Mechanism.** When a background task finishes, its result arrives as a new turn in the session that launched it. A new turn sends that session's full context. Ten tasks finishing separately means ten wake-ups, each at the main session's size.

**Arithmetic.** From "Who started the work": turns not started by the learner, their mean context, and their cost. Mean context × cache-read rate × calls per wake-up shows what one wake-up costs.

**Habit.** When fanning out background work, do it from a small coordinator session and ask for one combined report.

**Drill.** Launch the next batch of background tasks from a new session and compare that row in the next report.

**Check.** "Five background tasks report back one at a time to a 400k session. What are you paying for five times?" Answer: re-reading 400k of context on each wake-up, plus whatever the model does in response.

## A prompt repeats on a timer inside a working session

**Mechanism.** A loop started inside a session fires on its interval and sends that session's full context each time, even when nobody is working. Its cost is firings × context size.

**Habit.** When a loop is needed, start it in a dedicated small session, set the longest interval that still serves, and stop it when the need ends.

**Drill.** Open `/usage` and find the loops rows: how often each fires and its tokens per run.

**Check.** "A loop fires every 10 minutes for 8 hours in a 300k session. How many full-context sends is that?" Answer: 48.

## Several sessions run at the same time

**Mechanism.** Each session has its own context and its own cache. Running several multiplies spend without being waste by itself. The waste is in sessions still open that are no longer doing needed work, especially ones with loops or background tasks.

**Habit.** When opening another session, close or clear one that has finished its task.

**Check.** "Does running four sessions cost more per unit of work than one?" Answer: no, unless some are idle but still firing, or each carries a large context it does not need.

## The same long content is written out more than once

**Mechanism.** Output tokens carry the highest rate. A document body written as a tool argument is output, and it then stays in context and is re-read on later calls. Writing the same body twice, to a file and then to a second destination, pays for it twice.

**Habit.** When content must reach two places, write it to a file once and have the second step send the file. When revising, edit the changed parts in place.

**Drill.** In the next report, read "Tool traffic by size": find the tool with the most argument tokens and ask what it was writing.

**Check.** "A 20k-token design doc is rewritten in full three times. Roughly how many output tokens did the revisions cost compared with editing three paragraphs?" Answer: about 60k against a few hundred.

## Nearly everything runs at high effort

**Mechanism.** The effort level sets how much the model thinks before answering. Thinking is billed as output. High effort earns its cost on design and debugging and adds little on routine edits and lookups.

**Habit.** When the next turn is routine, lower the effort; raise it again for design decisions and hard bugs.

**Drill.** Change the effort level once in each direction during the next session and note where the answer quality changed.

**Check.** "Which costs more at high effort: a hard design question or a file rename?" Answer: both pay for extra thinking, but only the design question benefits from it.
