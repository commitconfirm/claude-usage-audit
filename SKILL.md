---
name: usage-audit
description: Audit where Claude tokens and dollars went, and produce a report on which working habits to change.
disable-model-invocation: true
---

# Usage audit

`usage_audit.py` in this folder does all the counting offline and writes the report. Your job is to run it, add the written analysis, and hand over the finished page. The report is the only input you need: it is a few hundred lines, while the raw logs are millions of tokens.

## Steps

1. Ask the user which remote hosts they also run Claude on (IPs or SSH names), unless they already said. Done when you have a list or an explicit "none".

2. Run the audit. Default to the last 7 days; use the period the user names if they give one.

   ```bash
   python3 <this folder>/usage_audit.py --since 7d --md /tmp/claude-usage-audit.md --hosts <host,host>
   ```

   With no remote hosts, replace `--hosts ...` with `--no-remote`. Add `--redact` when the user says the result will be shared. Add `--prices <file>` when the user has a contracted-rate price table. Done when the command exits 0 and the file exists.

3. Read `/tmp/claude-usage-audit.md` in full. Done when you have read the "Checks and caveats" section at the end.

4. Write the analysis to `/tmp/claude-usage-notes.md`. The report's "What to change" section already lists each pattern with its evidence. Your notes turn those into **habits**: what this user should do differently on a normal working day, in the order to try them.
   - Tie each habit to rows in the report: the session, model, host, or tool by name, with its dollar figure.
   - Merge patterns that are one habit seen twice (long sessions and cold resumes usually are).
   - Where the arithmetic is simple, show the per-call or per-thread cost before and after.
   - Close with what the data rules out, and what it cannot see.

   Done when every finding marked high or medium impact is covered by a habit or named as overlapping one.

5. Build the final report, repeating the options from step 2:

   ```bash
   python3 <this folder>/usage_audit.py --since 7d --hosts <host,host> \
     --notes /tmp/claude-usage-notes.md --html ./claude-usage-audit-$(date +%Y%m%d).html
   ```

   Done when the HTML file exists.

6. Deliver it. Publish the HTML file as an artifact when an artifact tool is available; otherwise give its path. In the reply, state the estimated total, the period, and the top three habits in one line each.

7. State what the audit cannot see: the caveats section, any remote host listed as failed, and any price-check mismatch (a mismatch means the configured rates differ from what Claude Code used, so dollar figures are off while token figures hold).

## Boundaries

Work from the report and from numbers. Session titles, project names, file names, and tool names are the labels to use; prompt and document text stay in the logs.
