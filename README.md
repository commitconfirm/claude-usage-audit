# claude-usage-audit

Find out where your Claude tokens went, from the session logs already on your machine.

One Python file, standard library only. It makes no network calls, only reads the logs, and costs zero tokens to run. A thin skill (`SKILL.md`) lets Claude interpret the report afterwards.

## What it reads

| Surface | Default location |
|---|---|
| Claude Code (terminal, IDE, desktop Code tab) | `~/.claude/projects` |
| Claude desktop app sessions (macOS) | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Claude desktop app sessions (Linux, Windows) | `~/.config/Claude/...`, `%APPDATA%\Claude\...` |

Sessions that ran on other hosts (over SSH, or the desktop app's SSH sessions) are logged on those hosts. See "Remote hosts" below. Web-hosted chat leaves no logs anywhere you can read; for a view across every surface, an organization admin has to export the spend report.

## Run it

```bash
python3 usage_audit.py --since 7d --html report.html
```

The HTML report is one self-contained page with no scripts and no network requests. Use `--md report.md` for Markdown, or neither to print Markdown to the terminal.

Run in a terminal, it first asks which remote hosts to include. Press Enter for none.

Useful options:

| Option | Purpose |
|---|---|
| `--since 7d` / `--since 2026-11-01` / `--until 2026-11-15` | Limit the period (UTC) |
| `--notes notes.md` | Include your own written analysis under "Analyst notes" |
| `--redact` | Replace session, project, file, and MCP names with hashes |
| `--prices my-prices.json` | Use contracted rates instead of list prices |
| `--dump-prices > my-prices.json` | Write the built-in price table to edit |
| `--json out.json` | Full results for further processing |
| `--context-threshold 150000` | What counts as a large context |
| `folder ...` | Read specific folders instead of the defaults |
| `--hosts 10.0.0.5,build-box` | Include these remote hosts without being asked |
| `--no-remote` | Skip the remote-host question |
| `--export FILE` / `--merge FILE ...` | Move numbers by hand from hosts you cannot reach over SSH |

## Remote hosts

For each host you name, the script runs `ssh <host> python3 -` and pipes itself over stdin. The remote copy reads that host's logs and sends back a numeric export. The local run merges it and adds a "By host" table.

- **Nothing is installed** on the remote host. It needs `python3` and your normal SSH access.
- **Transcripts stay on the host.** The export holds token counts, timestamps, and the same short labels as the report. With `--redact`, labels are hashed before they leave the host.
- **Nothing is counted twice.** A session mirrored on your laptop and present on the host is matched by message ID.
- Host names and addresses come from your `~/.ssh/config` as usual, so jump hosts and non-default users work. Password and host-key prompts appear in your terminal.
- A host that fails is listed in the report with the reason, and the run continues.

For a host your laptop cannot reach, run `python3 usage_audit.py --export host-a.json` there, carry the file over, and add `--merge host-a.json` locally.

## What the report shows

- **What to change:** each costly pattern the numbers point at, ranked by the share of cost it touches, with its evidence, its single worst instance (session, time, cost), and the habit that addresses it. Patterns that were checked and found small are listed too.
- **Where the money went:** cache reads, cache writes, output, input.
- **Cost drivers**, as overlapping shares: large-context requests, subagents, long sessions, parallel sessions, turns you did not start, cache misses, repeated tool arguments.
- **Breakdowns** by model (main thread versus subagent), context size, effort level, day, project, and surface.
- **Costliest sessions and turns**, with median and peak context.
- **Who started the work:** your prompts versus notifications, loops, and other sessions. Prompts that fire three or more times in a session are listed by hash, which is how loops surface.
- **Cache misses**, grouped by how long the session sat idle first.
- **Subagents** by type and model, and how many launches inherited the session's model.
- **Tool traffic by size**, per tool and per MCP server: tokens the model wrote as arguments (such as a document body) and tokens that came back as results.
- **Repeated long tool arguments:** the same body emitted more than once, flagged when it crosses two tools (the double-write pattern).
- **Files rewritten** and **targets re-read** three or more times in a session.

## What it never shows

Prompt text, response text, and document contents stay in the logs. The report carries numbers plus short labels: session titles, project names, file names, tool names. `--redact` hashes those too.

## How the numbers are derived

- Each API response is logged once per content block and copied into forked or resumed session files. The script counts each response once, by message ID.
- Dollar figures are token counts multiplied by the price table. The built-in table is Anthropic list pricing as of 2026-11-15. On an organization with contracted rates, edit a copy and pass `--prices`.
- The report ends with a price check: it recomputes the cost records Claude Code itself logged and reports how many match.
- Figures marked "(est.)" use characters divided by four, because the logs carry no token count for individual tool arguments or results.

## Known limits

- The log format is undocumented and changes between Claude Code releases. Built against 2.1.2xx to 2.1.321.
- Loop and scheduled-task detection relies on repeated prompts and on the turn-origin field. It has been tested on background-task notifications, and not yet on `/loop` output.
- Remote-host support is covered by an automated end-to-end test with a stand-in `ssh`. Check the first real run's "By host" table against what you expect.
- Small-model side calls made by tools, such as web fetch summaries, are not logged per request.

## Use as a Claude Code skill

```bash
git clone https://github.com/commitconfirm/claude-usage-audit ~/.claude/skills/usage-audit
```

Then run `/usage-audit` in Claude Code. The skill is a blunt tutor. It runs the script, names your most expensive mistakes with their dates and dollar figures, and teaches three lessons, one at a time: what went wrong, why it cost that, what to do instead, a drill, and a check question. It builds the final HTML report with the lessons included and keeps a progress log, so the next run starts with what changed.

`LESSONS.md` holds the teaching material. The skill is user-invoked only, so it adds nothing to your context until you call it.

## Tests

```bash
python3 -m unittest discover tests
```

## Related

Several tools read the same logs. Anthropic's `session-report` plugin gives an HTML report of tokens by project, skill, and subagent type. Claude Code's built-in `/insights` gives a qualitative report on friction and habits, and `/usage` lists recommendations for the current machine. This tool adds dollar costs, per-model and context-size splits, cache-miss gaps and lifetimes, turn origins, repeated tool arguments, desktop-app sessions, remote hosts over SSH, and a report that carries no transcript text.

## License

MIT
