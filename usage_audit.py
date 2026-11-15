#!/usr/bin/env python3
"""claude-usage-audit: offline cost-driver report from local Claude session logs.

Reads the JSONL transcripts that Claude Code and the Claude desktop app already
write to disk, and reports where tokens and estimated dollars went. Standard
library only, no network access, read-only on the logs.

The report contains numbers plus short labels (session titles, project and file
names, tool names). It never contains prompt, response, or document text.
Use --redact to replace the labels with stable hashes.
"""
import argparse
import bisect
import collections
import datetime as dt
import hashlib
import html
import json
import os
import re
import shlex
import socket
import statistics
import subprocess
import sys
import zlib

VERSION = "0.5.0"

# USD per million tokens, list price. Longest matching model-id prefix wins.
# Source: https://platform.claude.com/docs/en/about-claude/pricing (2026-11-15).
# Edit a copy (see --dump-prices) to enter contracted rates.
def _p(i, w5, w1, r, o):
    return {"input": i, "cache_write_5m": w5, "cache_write_1h": w1, "cache_read": r, "output": o}

DEFAULT_PRICES = {
    "fast_multiplier": 2.0,
    "us_geo_multiplier": 1.1,
    "web_search_per_request": 0.01,
    "cache_write_ttl_when_unknown": "1h",
    "models": {
        "claude-fable-5-2": _p(8, 10, 16, 0.20, 40),
        "claude-mythos-5-2": _p(8, 10, 16, 0.20, 40),
        "claude-opus-5-5": _p(4, 5, 8, 0.20, 20),
        "claude-sonnet-5-5": _p(2, 2.5, 4, 0.10, 10),
        "claude-haiku-5-5": dict(_p(0.10, 0.125, 0.20, 0.01, 0.50),
                                 over=dict(_p(0.50, 0.625, 1.0, 0.05, 2.50), threshold=100000)),
        "claude-fable-5-1": _p(10, 12.5, 20, 0.25, 50),
        "claude-mythos-5-1": _p(10, 12.5, 20, 0.25, 50),
        "claude-fable-5": _p(10, 12.5, 20, 1.00, 50),
        "claude-mythos-5": _p(10, 12.5, 20, 1.00, 50),
        "claude-opus-5": _p(5, 6.25, 10, 0.50, 25),
        "claude-opus-4-8": _p(5, 6.25, 10, 0.50, 25),
        "claude-opus-4-7": _p(5, 6.25, 10, 0.50, 25),
        "claude-opus-4-6": _p(5, 6.25, 10, 0.50, 25),
        "claude-opus-4-5": _p(5, 6.25, 10, 0.50, 25),
        "claude-opus-4-1": _p(15, 18.75, 30, 1.50, 75),
        "claude-opus-4": _p(15, 18.75, 30, 1.50, 75),
        "claude-sonnet-5": _p(2, 2.5, 4, 0.20, 10),
        "claude-sonnet-4-6": _p(3, 3.75, 6, 0.30, 15),
        "claude-sonnet-4-5": _p(3, 3.75, 6, 0.30, 15),
        "claude-sonnet-4": _p(3, 3.75, 6, 0.30, 15),
        "claude-haiku-4-5": _p(1, 1.25, 2, 0.10, 5),
    },
}

PAYLOAD_MIN_CHARS = 800      # smallest tool argument tracked as a "document body"
MISS_MIN_CONTEXT = 20000     # ignore cache behaviour on tiny requests
ACTIVE_GAP_S = 300           # requests closer than this belong to one active stretch
CHARS_PER_TOKEN = 4.0        # rough estimate, used only where logs give no token count
EXPORT_FORMAT = "claude-usage-audit-export/1"
HOST_RE = re.compile(r"^[A-Za-z0-9_\[][A-Za-z0-9_.@:\-\[\]%]*$")   # never starts with "-"
CMD_RE = re.compile(r"<command-name>\s*(/?[\w:.\-]+)\s*</command-name>")
WRITE_TOOLS = {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}


# ---------------------------------------------------------------- helpers

def parse_ts(s):
    if isinstance(s, (int, float)):
        return s / 1000.0 if s > 1e11 else float(s)
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sha(s, n=8):
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:n]


def fmt_tok(n):
    n = float(n or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return ("%.1f" % (n / div)).replace(".0", "") + suf
    return "%d" % n


def fmt_usd(x):
    return "n/a" if x is None else "$%.2f" % x


def pct(a, b):
    return "0%" if not b else "%d%%" % round(100.0 * a / b)


def fmt_when(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_dur(s):
    s = float(s or 0)
    if s >= 3600:
        return "%.1fh" % (s / 3600)
    if s >= 60:
        return "%dm" % round(s / 60)
    return "%ds" % round(s)


def text_len(content):
    """Characters of text in a tool_result content field (images counted as 0)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(text_len(b.get("text") if isinstance(b, dict) and "text" in b
                            else (b.get("content") if isinstance(b, dict) else b))
                   for b in content)
    return 0


def big_strings(obj, path=""):
    """Yield (path, string) for every long string inside a tool input."""
    if isinstance(obj, str):
        if len(obj) >= PAYLOAD_MIN_CHARS:
            yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from big_strings(v, path + "." + str(k) if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from big_strings(v, path + "[]")


def default_roots():
    home = os.path.expanduser("~")
    cands = [os.path.join(home, ".claude", "projects"),
             os.path.join(home, ".config", "claude", "projects")]
    app_dirs = [os.path.join(home, "Library", "Application Support", "Claude"),
                os.path.join(home, ".config", "Claude")]
    if os.environ.get("APPDATA"):
        app_dirs.append(os.path.join(os.environ["APPDATA"], "Claude"))
    for a in app_dirs:
        for sub in ("local-agent-mode-sessions", "claude-code-sessions"):
            cands.append(os.path.join(a, sub))
    return [c for c in cands if os.path.isdir(c)]


class Labeler:
    def __init__(self, redact):
        self.redact = redact

    def __call__(self, kind, value):
        if value is None or value == "":
            return "-"
        value = str(value)
        if self.redact:
            return "%s-%s" % (kind, sha(value, 6))
        return value.replace("|", "/").replace("\n", " ")[:60]


# ---------------------------------------------------------------- pricing

def price_for(model, prices):
    best = None
    for k in prices["models"]:
        if model and model.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    return prices["models"].get(best) if best else None


def cost_parts(model, inp, cw5, cw1, cr, out, prices, speed=None, geo=None, web=0, tiered=True):
    """Return dict of cost components in USD, or None if the model is unpriced.

    tiered=False skips long-prompt tiers; use it for totals that span many requests.
    """
    p = price_for(model, prices)
    if p is None:
        return None
    over = p.get("over")
    if tiered and over and (inp + cw5 + cw1 + cr) > over.get("threshold", 0):
        p = over
    mult = 1.0
    if speed == "fast":
        mult *= prices.get("fast_multiplier", 1.0)
    if geo == "us":
        mult *= prices.get("us_geo_multiplier", 1.0)
    c = {"input": inp * p["input"], "write": cw5 * p["cache_write_5m"] + cw1 * p["cache_write_1h"],
         "read": cr * p["cache_read"], "output": out * p["output"]}
    c = {k: v * mult / 1e6 for k, v in c.items()}
    c["web"] = web * prices.get("web_search_per_request", 0.0)
    c["total"] = sum(c.values())
    c["write_5m"] = cw5 * p["cache_write_5m"] * mult / 1e6
    c["write_1h"] = cw1 * p["cache_write_1h"] * mult / 1e6
    return c


# ---------------------------------------------------------------- parsing

class Store:
    def __init__(self):
        self.reqs = {}                 # message id -> request dict
        self.turns = {}                # turn key -> turn dict
        self.tool_owner = {}           # tool_use id -> (tool name, message id)
        self.tool_result_chars = {}    # tool_use id -> chars returned
        self.tool_arg_chars = {}       # tool_use id -> chars the model wrote as arguments
        self.titles = {}               # session id -> title
        self.payloads = []             # long tool arguments
        self.payload_keys = set()      # (tool_use id, argument path) already recorded
        self.tool_meta = {}            # tool_use id -> [op, target, chars] for writes, edits, reads
        self.agent_launch = {}         # tool_use id -> [subagent type, model argument]
        self.agent_meta = {}           # agent id -> agent type
        self.host = "local"            # label given to requests ingested from this machine
        self.unknown_ttl = "1h"
        self.remote_status = []        # (host, outcome) for each remote host or export merged
        self.cost_states = {}
        self.files = 0
        self.lines = 0
        self.bad_lines = 0


def iter_log_files(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for d, _, fs in os.walk(root):
            for f in fs:
                if f.endswith(".jsonl") or (f.startswith("agent-") and f.endswith(".meta.json")):
                    yield os.path.join(d, f)


def project_of(cwd, path):
    if cwd:
        if "local-agent-mode-sessions" in cwd or "claude-code-sessions" in cwd:
            return "desktop-app-session"
        return os.path.basename(cwd.rstrip("/\\")) or cwd
    parts = path.replace("\\", "/").split("/")
    if "projects" in parts:
        i = len(parts) - 1 - parts[::-1].index("projects")
        if i + 1 < len(parts):
            return parts[i + 1].rsplit("-", 1)[-1] or parts[i + 1]
    return "unknown"


def ingest_file(path, st):
    if path.endswith(".meta.json"):
        try:
            meta = json.load(open(path, errors="replace"))
            aid = os.path.basename(path)[len("agent-"):-len(".meta.json")]
            st.agent_meta[aid] = meta.get("agentType")
        except (OSError, ValueError):
            pass
        return
    st.files += 1
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            st.lines += 1
            if '"message"' not in line and "itle" not in line and "cost-state" not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                st.bad_lines += 1
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            sid = o.get("sessionId") or o.get("session_id")
            if t == "assistant":
                ingest_assistant(o, sid, path, st)
            elif t == "user":
                ingest_user(o, sid, st)
            elif t in ("custom-title", "ai-title") and sid:
                title = o.get("customTitle") or o.get("aiTitle")
                if title and (t == "custom-title" or sid not in st.titles):
                    st.titles[sid] = title
            elif t == "cost-state":
                st.cost_states[json.dumps(o.get("modelUsage"), sort_keys=True)] = o.get("modelUsage") or {}


def ingest_assistant(o, sid, path, st):
    m = o.get("message")
    if not isinstance(m, dict):
        return
    u = m.get("usage")
    model = m.get("model")
    if not isinstance(u, dict) or not model or model.startswith("<"):
        return
    mid = m.get("id") or o.get("requestId") or o.get("uuid")
    if not mid:
        return
    r = st.reqs.get(mid)
    if r is None:
        agent = o.get("agentId") or o.get("parent_tool_use_id")
        r = st.reqs[mid] = {
            "id": mid, "sid": sid, "agent": agent, "sub": bool(agent or o.get("isSidechain")),
            "ts": parse_ts(o.get("timestamp") or o.get("_audit_timestamp")),
            "model": model, "effort": o.get("effort"),
            "attr_agent": o.get("attributionAgent"), "attr_skill": o.get("attributionSkill"),
            "attr_plugin": o.get("attributionPlugin"), "attr_mcp": o.get("attributionMcpServer"),
            "entry": o.get("entrypoint") or ("desktop-audit" if "session_id" in o else "unknown"),
            "project": project_of(o.get("cwd"), path), "host": st.host,
            "speed": u.get("speed"), "geo": u.get("inference_geo"),
            "inp": 0, "cr": 0, "cw5": 0, "cw1": 0, "out": 0, "think": 0, "web": 0,
        }
    # The same response is logged once per content block, and copied into forked or
    # resumed session files. Token fields repeat, so keep the maximum, never the sum.
    cc = u.get("cache_creation") or {}
    cw_total = u.get("cache_creation_input_tokens") or 0
    cw5 = cc.get("ephemeral_5m_input_tokens")
    cw1 = cc.get("ephemeral_1h_input_tokens")
    if cw5 is None and cw1 is None:
        cw5, cw1 = (cw_total, 0) if st.unknown_ttl == "5m" else (0, cw_total)
    r["inp"] = max(r["inp"], u.get("input_tokens") or 0)
    r["cr"] = max(r["cr"], u.get("cache_read_input_tokens") or 0)
    r["cw5"] = max(r["cw5"], cw5 or 0)
    r["cw1"] = max(r["cw1"], cw1 or 0)
    r["out"] = max(r["out"], u.get("output_tokens") or 0)
    r["web"] = max(r["web"], (u.get("server_tool_use") or {}).get("web_search_requests") or 0)
    r["think"] = max(r["think"], (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    for b in m.get("content") or []:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        tid = b.get("id")
        if not tid or tid in st.tool_owner:
            continue
        name = b.get("name") or "?"
        st.tool_owner[tid] = (name, mid)
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        st.tool_arg_chars[tid] = len(json.dumps(inp, ensure_ascii=False))
        fp = inp.get("file_path") or inp.get("notebook_path")
        if name in WRITE_TOOLS and fp:
            body = inp.get(WRITE_TOOLS[name])
            st.tool_meta[tid] = ["write" if name == "Write" else "edit", fp,
                                 len(body) if isinstance(body, str) else 0]
        elif name == "Read" and fp:
            st.tool_meta[tid] = ["read", fp, 0]
        elif name == "WebFetch" and isinstance(inp.get("url"), str):
            url = inp["url"]
            st.tool_meta[tid] = ["read", "web:" + (url.split("/")[2] if "//" in url else url[:40]), 0]
        elif name in ("Agent", "Task"):
            st.agent_launch[tid] = [inp.get("subagent_type") or "?", inp.get("model") or "inherit"]
        for key, s in big_strings(inp):
            norm = " ".join(s.split())
            # crc32 rather than hash(): stable across processes, so exports from other hosts compare
            lines = frozenset(zlib.crc32(x.strip().encode("utf-8", "replace"))
                              for x in s.splitlines() if len(x.strip()) >= 12)
            st.payload_keys.add((tid, key))
            st.payloads.append({"sid": sid, "mid": mid, "tid": tid, "tool": name, "key": key, "chars": len(s),
                                "hash": sha(norm, 16), "lines": lines,
                                "label": os.path.basename(fp) if fp else None})


def ingest_user(o, sid, st):
    m = o.get("message")
    if not isinstance(m, dict):
        return
    content = m.get("content")
    has_result = False
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                has_result = True
                tid = b.get("tool_use_id")
                if tid:
                    st.tool_result_chars[tid] = max(st.tool_result_chars.get(tid, 0), text_len(b.get("content")))
            elif b.get("type") == "text" and isinstance(b.get("text"), str):
                texts.append(b["text"])
    if has_result or not texts or o.get("isMeta") or o.get("isSidechain") or o.get("agentId") \
            or o.get("parent_tool_use_id"):
        return
    ts = parse_ts(o.get("timestamp") or o.get("_audit_timestamp"))
    if ts is None or not sid:
        return
    text = "\n".join(texts)
    origin = o.get("origin") if isinstance(o.get("origin"), dict) else {}
    kind = origin.get("kind") or o.get("turnOrigin") or o.get("promptSource") or "unlabelled"
    kind = str(kind).replace("_", "-")
    key = o.get("uuid") or "%s:%s" % (sid, ts)
    if key in st.turns:
        return
    cmd = CMD_RE.search(text[:2000])
    st.turns[key] = {"sid": sid, "ts": ts, "origin": kind, "chars": len(text),
                     "hash": sha(" ".join(text.split()), 10), "cmd": cmd.group(1) if cmd else None}


# ---------------------------------------------------------------- analysis

def new_bucket():
    return {"requests": 0, "inp": 0, "cr": 0, "cw": 0, "cw5": 0, "cw1": 0, "out": 0, "cost": 0.0,
            "c_write5": 0.0, "c_write1": 0.0,
            "c_read": 0.0, "c_write": 0.0, "c_output": 0.0, "c_input": 0.0, "c_web": 0.0, "unpriced": 0}


def add(b, r):
    b["requests"] += 1
    b["inp"] += r["inp"]
    b["cr"] += r["cr"]
    b["cw"] += r["cw5"] + r["cw1"]
    b["cw5"] += r["cw5"]
    b["cw1"] += r["cw1"]
    b["out"] += r["out"]
    c = r["cost"]
    if c is None:
        b["unpriced"] += 1
    else:
        b["cost"] += c["total"]
        b["c_read"] += c["read"]
        b["c_write"] += c["write"]
        b["c_output"] += c["output"]
        b["c_input"] += c["input"]
        b["c_web"] += c["web"]
        b["c_write5"] += c["write_5m"]
        b["c_write1"] += c["write_1h"]


def analyze(st, prices, since, until, threshold, label):
    reqs = [r for r in st.reqs.values() if r["ts"] is not None
            and (since is None or r["ts"] >= since) and (until is None or r["ts"] < until)]
    reqs.sort(key=lambda r: r["ts"])
    for r in reqs:
        r["ctx"] = r["inp"] + r["cr"] + r["cw5"] + r["cw1"]
        r["cost"] = cost_parts(r["model"], r["inp"], r["cw5"], r["cw1"], r["cr"], r["out"],
                               prices, r["speed"], r["geo"], r["web"])
        r["usd"] = r["cost"]["total"] if r["cost"] else 0.0
    total = new_bucket()
    for r in reqs:
        add(total, r)
    T = total["cost"] or 1e-12
    out = {"version": VERSION, "requests": len(reqs), "total": total,
           "files": st.files, "lines": st.lines, "bad_lines": st.bad_lines,
           "remote": [(label("host", h), outcome) for h, outcome in st.remote_status]}
    if not reqs:
        return out
    out["first"], out["last"] = reqs[0]["ts"], reqs[-1]["ts"]
    live = {r["id"]: r for r in reqs}

    def group(keyfn):
        g = collections.defaultdict(new_bucket)
        for r in reqs:
            add(g[keyfn(r)], r)
        return dict(sorted(g.items(), key=lambda kv: -kv[1]["cost"]))

    out["by_model"] = group(lambda r: "%s (%s)" % (r["model"], "subagent" if r["sub"] else "main"))
    out["by_day"] = dict(sorted(group(lambda r: dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc).strftime("%Y-%m-%d")).items()))
    out["by_project"] = group(lambda r: label("proj", r["project"]))
    out["by_surface"] = group(lambda r: r["entry"])
    out["by_host"] = group(lambda r: label("host", r.get("host") or "local"))
    out["by_effort"] = group(lambda r: r["effort"] or "unrecorded")
    out["by_skill"] = group(lambda r: r["attr_skill"] or "(none)")
    out["by_mcp_attr"] = group(lambda r: r["attr_mcp"] or "(none)")

    def ctx_bucket(r):
        for hi, name in ((50e3, "under 50k"), (100e3, "50k-100k"), (150e3, "100k-150k"), (250e3, "150k-250k")):
            if r["ctx"] < hi:
                return name
        return "250k and over"
    out["by_context"] = group(ctx_bucket)

    # ---- sessions, activity intervals, concurrency
    by_sid = collections.defaultdict(list)
    for r in reqs:
        by_sid[r["sid"]].append(r)
    intervals = []
    sessions = {}
    for sid, rs in by_sid.items():
        b = new_bucket()
        active, start, prev = 0.0, rs[0]["ts"], rs[0]["ts"]
        for r in rs:
            add(b, r)
            if r["ts"] - prev > ACTIVE_GAP_S:
                intervals.append((start, prev))
                active += prev - start
                start = r["ts"]
            prev = r["ts"]
        intervals.append((start, prev))
        active += prev - start
        main = [r for r in rs if not r["sub"]]
        ctxs = [r["ctx"] for r in main] or [r["ctx"] for r in rs]
        b.update(sid=sid, title=label("sess", st.titles.get(sid) or sid[:8]),
                 project=label("proj", rs[0]["project"]), first=rs[0]["ts"], last=rs[-1]["ts"],
                 span=rs[-1]["ts"] - rs[0]["ts"], active=active,
                 max_ctx=max(ctxs), median_ctx=int(statistics.median(ctxs)),
                 sub_cost=sum(r["usd"] for r in rs if r["sub"]),
                 sub_threads=len({r["agent"] for r in rs if r["sub"]}),
                 models=sorted({r["model"] for r in rs}))
        sessions[sid] = b
    starts = sorted(i[0] for i in intervals)
    ends = sorted(i[1] for i in intervals)
    conc = collections.defaultdict(float)
    for r in reqs:
        k = bisect.bisect_right(starts, r["ts"]) - bisect.bisect_left(ends, r["ts"])
        r["conc"] = max(1, k)
        conc[min(r["conc"], 4)] += r["usd"]
    out["concurrency"] = {("%d" % k if k < 4 else "4+"): v for k, v in sorted(conc.items())}

    # ---- turns: who started the work, and what did each start cost
    turns_by_sid = collections.defaultdict(list)
    for t in st.turns.values():
        turns_by_sid[t["sid"]].append(t)
    for ts_ in turns_by_sid.values():
        ts_.sort(key=lambda t: t["ts"])
        for t in ts_:
            t.update(cost=0.0, requests=0, max_ctx=0)
    orphan = 0.0
    turn_starts = {sid: [t["ts"] for t in ts_] for sid, ts_ in turns_by_sid.items()}
    for r in reqs:
        ts_ = turns_by_sid.get(r["sid"])
        i = bisect.bisect_right(turn_starts[r["sid"]], r["ts"]) - 1 if ts_ else -1
        if i < 0:
            orphan += r["usd"]
            continue
        t = ts_[i]
        t["cost"] += r["usd"]
        t["requests"] += 1
        t["max_ctx"] = max(t["max_ctx"], r["ctx"])
        r["origin"] = t["origin"]
    turns = [t for ts_ in turns_by_sid.values() for t in ts_ if t["requests"]]
    by_origin = collections.defaultdict(lambda: {"turns": 0, "cost": 0.0, "requests": 0})
    by_cmd = collections.defaultdict(lambda: {"turns": 0, "cost": 0.0})
    rep = collections.defaultdict(list)
    for t in turns:
        o = by_origin[t["origin"]]
        o["turns"] += 1
        o["cost"] += t["cost"]
        o["requests"] += t["requests"]
        if t["cmd"]:
            by_cmd[t["cmd"]]["turns"] += 1
            by_cmd[t["cmd"]]["cost"] += t["cost"]
        rep[(t["sid"], t["hash"])].append(t)
    out["by_origin"] = dict(sorted(by_origin.items(), key=lambda kv: -kv[1]["cost"]))
    out["by_command"] = dict(sorted(by_cmd.items(), key=lambda kv: -kv[1]["cost"]))
    out["unattributed_turn_cost"] = orphan
    repeated = []
    for (sid, h), ts_ in rep.items():
        if len(ts_) < 3:
            continue
        gaps = [b["ts"] - a["ts"] for a, b in zip(ts_, ts_[1:])]
        repeated.append({"session": sessions[sid]["title"] if sid in sessions else sid[:8],
                         "prompt_id": ts_[0]["cmd"] or h, "origin": ts_[0]["origin"], "firings": len(ts_),
                         "median_gap": statistics.median(gaps), "cost": sum(t["cost"] for t in ts_),
                         "mean_ctx": int(statistics.mean(t["max_ctx"] for t in ts_))})
    out["repeated_prompts"] = sorted(repeated, key=lambda x: -x["cost"])
    out["top_turns"] = [{"session": sessions[t["sid"]]["title"], "when": fmt_when(t["ts"]), "origin": t["origin"],
                         "command": t["cmd"] or "-", "prompt_chars": t["chars"], "requests": t["requests"],
                         "max_ctx": t["max_ctx"], "cost": t["cost"]}
                        for t in sorted(turns, key=lambda t: -t["cost"])[:15]]

    # ---- cache behaviour per thread (main conversation or one subagent)
    threads = collections.defaultdict(list)
    for r in reqs:
        threads[(r["sid"], r["agent"])].append(r)
    miss = {"count": 0, "tokens": 0, "cost": 0.0, "by_gap": collections.defaultdict(lambda: [0, 0.0])}
    rebuild = {"count": 0, "tokens": 0, "cost": 0.0}
    miss_events = []
    starts_sub = []
    for (sid, agent), rs in threads.items():
        prev = None
        for r in rs:
            if prev is None:
                if agent:
                    starts_sub.append(r["ctx"])
            elif r["ctx"] >= MISS_MIN_CONTEXT and r["cr"] < 0.5 * r["ctx"]:
                wcost = r["cost"]["write"] if r["cost"] else 0.0
                wtok = r["cw5"] + r["cw1"]
                if r["ctx"] < 0.6 * prev["ctx"]:
                    rebuild["count"] += 1
                    rebuild["tokens"] += wtok
                    rebuild["cost"] += wcost
                else:
                    gap = r["ts"] - prev["ts"]
                    g = "under 5m" if gap < 300 else ("5m-1h" if gap < 3600 else "over 1h")
                    miss["count"] += 1
                    miss["tokens"] += wtok
                    miss["cost"] += wcost
                    miss["by_gap"][g][0] += 1
                    miss["by_gap"][g][1] += wcost
                    miss_events.append({"session": sessions[sid]["title"], "when": fmt_when(r["ts"]), "idle": gap,
                                        "thread": "subagent" if agent else "main", "model": r["model"],
                                        "tokens": wtok, "cost": wcost})
            prev = r
    miss["by_gap"] = dict(miss["by_gap"])
    out["cache_misses"], out["cache_rebuilds"] = miss, rebuild
    out["cold_resumes"] = sorted(miss_events, key=lambda e: -e["cost"])[:12]

    # ---- subagents
    sub = collections.defaultdict(lambda: {"threads": 0, "requests": 0, "cost": 0.0, "max_ctx": 0, "start_ctx": []})
    thread_events = []
    for (sid, agent), rs in threads.items():
        if not agent:
            continue
        typ = rs[0]["attr_agent"] or st.agent_meta.get(agent) or "unknown"
        thread_events.append({"session": sessions[sid]["title"], "when": fmt_when(rs[0]["ts"]), "type": typ,
                              "model": rs[0]["model"], "requests": len(rs), "max_ctx": max(r["ctx"] for r in rs),
                              "cost": sum(r["usd"] for r in rs)})
        s = sub[(typ, rs[0]["model"])]
        s["threads"] += 1
        s["requests"] += len(rs)
        s["cost"] += sum(r["usd"] for r in rs)
        s["max_ctx"] = max(s["max_ctx"], max(r["ctx"] for r in rs))
        s["start_ctx"].append(rs[0]["ctx"])
    out["subagents"] = [{"type": k[0], "model": k[1], "threads": v["threads"], "requests": v["requests"],
                         "cost": v["cost"], "cost_per_thread": v["cost"] / v["threads"],
                         "median_start_ctx": int(statistics.median(v["start_ctx"])), "max_ctx": v["max_ctx"]}
                        for k, v in sorted(sub.items(), key=lambda kv: -kv[1]["cost"])]
    out["subagent_threads"] = sorted(thread_events, key=lambda e: -e["cost"])[:12]
    agent_calls = collections.Counter(tuple(v) for tid, v in st.agent_launch.items()
                                      if st.tool_owner.get(tid, (None, None))[1] in live)
    out["agent_calls"] = [{"type": k[0], "model_arg": k[1], "calls": v} for k, v in agent_calls.most_common()]

    # ---- tools: what the model emitted as arguments and what came back into context
    tools = collections.defaultdict(lambda: {"calls": 0, "result_chars": 0, "arg_chars": 0})
    for tid, (name, mid) in st.tool_owner.items():
        if mid not in live:
            continue
        srv = name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else None
        for key in filter(None, (name, "mcp server: " + srv if srv else None)):
            tools[key]["calls"] += 1
            tools[key]["result_chars"] += st.tool_result_chars.get(tid, 0)
            tools[key]["arg_chars"] += st.tool_arg_chars.get(tid, 0)
    out["tools"] = [{"tool": label("tool", k) if k.startswith("mcp") else k, "calls": v["calls"],
                     "arg_tokens_est": int(v["arg_chars"] / CHARS_PER_TOKEN),
                     "result_tokens_est": int(v["result_chars"] / CHARS_PER_TOKEN)}
                    for k, v in sorted(tools.items(), key=lambda kv: -(kv[1]["result_chars"] + kv[1]["arg_chars"]))[:25]]

    # ---- the same long body emitted more than once (double writes, full re-emits)
    pls = [p for p in st.payloads if p["mid"] in live]
    by_sess = collections.defaultdict(list)
    for p in pls:
        by_sess[p["sid"]].append(p)
    clusters = []
    for sid, ps in by_sess.items():
        parent = list(range(len(ps)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        first = {}
        for i, p in enumerate(ps):
            j = first.setdefault(p["hash"], i)
            if j != i:
                parent[find(i)] = find(j)
        if len(ps) <= 600:
            for i in range(len(ps)):
                for j in range(i + 1, len(ps)):
                    a, b = ps[i]["lines"], ps[j]["lines"]
                    if len(a) >= 5 and len(b) >= 5 and find(i) != find(j):
                        inter = len(a & b)
                        if inter and inter / float(len(a | b)) >= 0.8:
                            parent[find(i)] = find(j)
        groups = collections.defaultdict(list)
        for i, p in enumerate(ps):
            groups[find(i)].append(p)
        for g in groups.values():
            if len(g) < 2:
                continue
            extra = sum(p["chars"] for p in g) - max(p["chars"] for p in g)
            rate = statistics.mean((price_for(live[p["mid"]]["model"], prices) or {"output": 0})["output"] for p in g)
            clusters.append({"session": sessions[sid]["title"], "emissions": len(g),
                             "tools": sorted({label("tool", p["tool"]) if p["tool"].startswith("mcp") else p["tool"] for p in g}),
                             "file": label("file", next((p["label"] for p in g if p["label"]), None)),
                             "largest_chars": max(p["chars"] for p in g),
                             "repeat_tokens_est": int(extra / CHARS_PER_TOKEN),
                             "repeat_cost_est": extra / CHARS_PER_TOKEN * rate / 1e6,
                             "crosses_tools": len({p["tool"] for p in g}) > 1})
    clusters.sort(key=lambda c: -c["repeat_cost_est"])
    out["duplicate_payloads"] = {"clusters": len(clusters),
                                 "repeat_tokens_est": sum(c["repeat_tokens_est"] for c in clusters),
                                 "repeat_cost_est": sum(c["repeat_cost_est"] for c in clusters),
                                 "cross_tool_cost_est": sum(c["repeat_cost_est"] for c in clusters if c["crosses_tools"]),
                                 "top": clusters[:15]}

    # ---- file revision churn and repeated reads
    file_ops = collections.defaultdict(collections.Counter)
    reads = collections.defaultdict(list)
    for tid, (op, target, chars) in st.tool_meta.items():
        mid = st.tool_owner.get(tid, (None, None))[1]
        if mid not in live:
            continue
        sid = live[mid]["sid"]
        if op == "read":
            reads[(sid, target)].append(tid)
        else:
            file_ops[(sid, target)][op] += 1
            file_ops[(sid, target)][op + "_chars"] += chars
    churn = []
    for (sid, fp), ops in file_ops.items():
        if ops["write"] + ops["edit"] >= 3:
            churn.append({"session": sessions[sid]["title"], "file": label("file", os.path.basename(fp)),
                          "writes": ops["write"], "edits": ops["edit"],
                          "emitted_tokens_est": int((ops["write_chars"] + ops["edit_chars"]) / CHARS_PER_TOKEN)})
    out["file_churn"] = sorted(churn, key=lambda c: -c["emitted_tokens_est"])[:15]
    rereads = []
    for (sid, target), tids in reads.items():
        if len(tids) >= 3:
            tgt = target if target.startswith("web:") else os.path.basename(target)
            rereads.append({"session": sessions[sid]["title"], "target": label("file", tgt), "reads": len(tids),
                            "result_tokens_est": int(sum(st.tool_result_chars.get(t, 0) for t in tids) / CHARS_PER_TOKEN)})
    out["repeated_reads"] = sorted(rereads, key=lambda c: -c["result_tokens_est"])[:15]

    # ---- headline drivers (overlapping, each a share of estimated cost)
    long_sids = {s for s, b in sessions.items() if b["span"] >= 8 * 3600}
    human = (None, "human", "unlabelled", "sdk")
    s_ctx = sum(r["usd"] for r in reqs if r["ctx"] >= threshold) / T
    s_sub = sum(r["usd"] for r in reqs if r["sub"]) / T
    s_long = sum(r["usd"] for r in reqs if r["sid"] in long_sids) / T
    s_conc2 = sum(r["usd"] for r in reqs if r["conc"] >= 2) / T
    s_conc4 = sum(r["usd"] for r in reqs if r["conc"] >= 4) / T
    s_auto = sum(r["usd"] for r in reqs if r.get("origin") not in human) / T
    s_miss = miss["cost"] / T
    s_dup = out["duplicate_payloads"]["repeat_cost_est"] / T
    out["drivers"] = [
        ("Re-reading cached context (cache-read cost)", total["c_read"] / T),
        ("Requests at or above %s context" % fmt_tok(threshold), s_ctx),
        ("Subagent requests", s_sub),
        ("Sessions spanning 8+ hours", s_long),
        ("Ran while 2+ sessions were active", s_conc2),
        ("Ran while 4+ sessions were active", s_conc4),
        ("Turns started by something other than your prompt", s_auto),
        ("Cache misses (re-writing context already paid for)", s_miss),
        ("Output tokens (the work product)", total["c_output"] / T),
        ("Repeated long tool arguments (estimate)", s_dup),
    ]
    out["sessions"] = sorted(sessions.values(), key=lambda b: -b["cost"])

    # ---- findings: which working habits the numbers point at
    F, minor = [], []

    def finding(share, title, evidence, change, floor, kind="pattern"):
        if share >= floor:
            F.append({"title": title, "share": share, "kind": kind,
                      "impact": "check" if kind == "check" else ("high" if share >= 0.3 else ("medium" if share >= 0.1 else "low")),
                      "evidence": [e for e in evidence if e], "change": [c for c in change if c]})
        else:
            minor.append((title, share))

    P = lambda v: "%d%%" % round(100 * v)
    top = out["sessions"][0]
    big = out["by_context"].get("250k and over", {"cost": 0.0})["cost"] / T
    finding(s_ctx, "Most spend happens at very large context", [
        "%s of cost came from requests carrying %s or more tokens of context, and %s from 250k or more."
        % (P(s_ctx), fmt_tok(threshold), P(big)),
        "Re-reading context cost %s (%s). The output itself cost %s (%s)."
        % (fmt_usd(total["c_read"]), P(total["c_read"] / T), fmt_usd(total["c_output"]), P(total["c_output"] / T)),
        "The costliest session (%s) ran at a median context of %s across %d requests."
        % (top["title"], fmt_tok(top["median_ctx"]), top["requests"]),
        "Costliest single prompt: %s at %s, %d requests at up to %s of context: %s."
        % (out["top_turns"][0]["session"], out["top_turns"][0]["when"], out["top_turns"][0]["requests"],
           fmt_tok(out["top_turns"][0]["max_ctx"]), fmt_usd(out["top_turns"][0]["cost"])) if out["top_turns"] else None,
    ], [
        "Start a new session for each task and carry a short handoff note across.",
        "Compact at task boundaries, before the context passes %s." % fmt_tok(threshold),
        "Keep large reference material in files the session reads when needed, so it is not carried on every request.",
    ], 0.4)

    gaps = miss["by_gap"]
    five = total["cw5"] / float(total["cw"] or 1)
    finding(s_miss, "Idle sessions are resumed after their cache has expired", [
        "%d requests re-wrote context that had dropped out of the cache: %s tokens, %s."
        % (miss["count"], fmt_tok(miss["tokens"]), fmt_usd(miss["cost"])),
        "%d followed an idle gap of over 1 hour (%s)." % (gaps["over 1h"][0], fmt_usd(gaps["over 1h"][1])) if "over 1h" in gaps else None,
        "%d followed a gap of 5 minutes to 1 hour (%s)." % (gaps["5m-1h"][0], fmt_usd(gaps["5m-1h"][1])) if "5m-1h" in gaps else None,
        "%s of cache writes used the 5-minute lifetime, so a six-minute pause re-bills the whole context." % P(five) if five >= 0.2 else None,
        "Worst single case: %s at %s, resumed after %s idle, %s re-written: %s."
        % (out["cold_resumes"][0]["session"], out["cold_resumes"][0]["when"], fmt_dur(out["cold_resumes"][0]["idle"]),
           fmt_tok(out["cold_resumes"][0]["tokens"]), fmt_usd(out["cold_resumes"][0]["cost"])) if out["cold_resumes"] else None,
    ], [
        "Before stepping away from a large session, compact it or close it with a handoff note.",
        "Pick old work back up in a new session from the handoff note.",
        "Check which cache lifetime your sessions get. The 1-hour lifetime costs more per write and survives short breaks." if five >= 0.2 else None,
    ], 0.05)

    if long_sids:
        longest = max((sessions[x] for x in long_sids), key=lambda b: b["span"])
        finding(s_long, "Sessions stay open for many hours", [
            "%s of cost came from %d sessions that stayed open 8 hours or longer." % (P(s_long), len(long_sids)),
            "The longest (%s) spanned %s with %s of activity." % (longest["title"], fmt_dur(longest["span"]), fmt_dur(longest["active"])),
        ], ["Close a session when its task ends. A session left open keeps its full context and is resumed cold."], 0.3)

    subs = out["subagents"]
    by_m = collections.defaultdict(lambda: [0, 0.0, 0])
    for x in subs:
        by_m[x["model"]][0] += x["threads"]
        by_m[x["model"]][1] += x["cost"]
        by_m[x["model"]][2] = max(by_m[x["model"]][2], x["max_ctx"])
    ranked = sorted(by_m.items(), key=lambda kv: -kv[1][1])
    per = {m: v[1] / v[0] for m, v in by_m.items() if v[0] >= 3 and v[1] > 0}
    common = max(by_m, key=lambda m: by_m[m][0]) if by_m else None
    launches = sum(c["calls"] for c in out["agent_calls"])
    pricey = sum(c["calls"] for c in out["agent_calls"] if not any(k in c["model_arg"] for k in ("sonnet", "haiku")))
    finding(s_sub, "Subagents run on expensive models and grow large", [
        "%s of cost came from subagent requests across %d threads." % (P(s_sub), sum(v[0] for v in by_m.values())),
    ] + ["%s: %d threads, %s, %s each." % (m, v[0], fmt_usd(v[1]), fmt_usd(v[1] / v[0])) for m, v in ranked[:3]] + [
        "A thread on %s cost %.0f times one on %s, the model used for the most threads."
        % (max(per, key=per.get), max(per.values()) / per[common], common)
        if common in per and max(per.values()) / per[common] >= 2 else None,
        "%d of %d launches asked for a larger model or inherited the session's model." % (pricey, launches) if launches and pricey else None,
        "The largest thread reached %s of context." % fmt_tok(max(v[2] for v in by_m.values())) if by_m else None,
        "Costliest single thread: %s on %s in %s at %s, %d requests, context up to %s: %s."
        % (out["subagent_threads"][0]["type"], out["subagent_threads"][0]["model"], out["subagent_threads"][0]["session"],
           out["subagent_threads"][0]["when"], out["subagent_threads"][0]["requests"],
           fmt_tok(out["subagent_threads"][0]["max_ctx"]), fmt_usd(out["subagent_threads"][0]["cost"])) if out["subagent_threads"] else None,
    ], [
        "Name a cheaper model on each subagent launch unless the task needs the larger one.",
        "Give each subagent one narrow task, so its context stays small.",
    ], 0.15)

    auto = [t for t in turns if t["origin"] not in human]
    finding(s_auto, "Background work keeps waking a large session", [
        "%s of cost came from %d turns you did not start (%s)."
        % (P(s_auto), len(auto), ", ".join(sorted({t["origin"] for t in auto}))) if auto else None,
        "Those turns ran at a mean context of %s." % fmt_tok(statistics.mean(t["max_ctx"] for t in auto)) if auto else None,
        "Costliest single wake-up: %s at %s, %d requests at up to %s of context: %s."
        % ((lambda w: (sessions[w["sid"]]["title"], fmt_when(w["ts"]), w["requests"], fmt_tok(w["max_ctx"]), fmt_usd(w["cost"])))
           (max(auto, key=lambda t: t["cost"]))) if auto else None,
    ], [
        "Have background tasks report once when all are finished, not one wake-up per task.",
        "Run loops and long background jobs from a dedicated small session.",
    ], 0.15)

    rp = out["repeated_prompts"]
    finding(sum(x["cost"] for x in rp) / T, "A prompt repeats on a timer inside a working session", [
        "The top one fired %d times in %s, about every %s, at %s of context: %s."
        % (rp[0]["firings"], rp[0]["session"], fmt_dur(rp[0]["median_gap"]), fmt_tok(rp[0]["mean_ctx"]), fmt_usd(rp[0]["cost"])) if rp else None,
    ], ["Move the loop to a dedicated small session.", "Lengthen its interval."], 0.02)

    finding(s_conc2, "Several sessions run at the same time", [
        "%s of cost ran while 2 or more sessions were active, and %s while 4 or more." % (P(s_conc2), P(s_conc4)),
    ], ["Parallel sessions multiply spend. Check that each one is still doing needed work."], 0.2)

    d = out["duplicate_payloads"]
    finding(s_dup, "The same long content is written out more than once", [
        "%d cases, about %s repeated output tokens, %s." % (d["clusters"], fmt_tok(d["repeat_tokens_est"]), fmt_usd(d["repeat_cost_est"])),
        "%s of that crossed two different tools (written once, then sent again elsewhere)." % fmt_usd(d["cross_tool_cost_est"]),
    ], ["Write the content to a file once and pass the file path to the second destination."], 0.02)

    hi_effort = sum(b["cost"] for k, b in out["by_effort"].items() if k in ("high", "xhigh", "max")) / T
    finding(hi_effort, "Nearly everything runs at high effort", [
        "%s of cost ran at high effort or above." % P(hi_effort),
    ], ["Lower the effort level for routine turns and keep high effort for design and debugging."], 0.6, kind="check")

    F.sort(key=lambda f: (f["kind"] != "pattern", -f["share"]))
    out["findings"] = F
    out["minor"] = sorted(minor, key=lambda m: -m[1])

    # ---- self-check against the cost Claude Code itself logged
    # Older records do not say whether cache writes had the 5-minute or 1-hour lifetime, and
    # no record says how many requests crossed a long-prompt price tier. Where the record
    # leaves that open, the logged cost only has to fall inside the possible range.
    ok = bad = 0
    for mu in st.cost_states.values():
        for model, u in mu.items():
            p = price_for(model, prices)
            if "costUSD" not in u or u["costUSD"] <= 0 or p is None:
                continue
            cw5, cw1 = u.get("cacheCreationEphemeral5mInputTokens"), u.get("cacheCreationEphemeral1hInputTokens")
            if cw5 is None and cw1 is None:
                cw = u.get("cacheCreationInputTokens") or 0
                splits = [(cw, 0), (0, cw)]
            else:
                splits = [(cw5 or 0, cw1 or 0)]
            tables = [prices]
            if p.get("over"):
                tables.append(dict(prices, models={model: {k: v for k, v in p["over"].items() if k != "threshold"}}))
            costs = [cost_parts(model, u.get("inputTokens") or 0, a, b, u.get("cacheReadInputTokens") or 0,
                                u.get("outputTokens") or 0, t, web=u.get("webSearchRequests") or 0,
                                tiered=False)["total"] for a, b in splits for t in tables]
            if min(costs) * 0.98 - 0.01 <= u["costUSD"] <= max(costs) * 1.02 + 0.01:
                ok += 1
            else:
                bad += 1
    out["price_check"] = {"matched": ok, "mismatched": bad}
    return out


# ---------------------------------------------------------------- report

def table(headers, rows):
    if not rows:
        return "_none found_\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def bucket_rows(d, total_cost, limit=12):
    rows = []
    for k, b in list(d.items())[:limit]:
        rows.append((k, b["requests"], fmt_tok(b["cr"]), fmt_tok(b["cw"]), fmt_tok(b["out"]),
                     fmt_usd(b["cost"]), pct(b["cost"], total_cost)))
    return rows


def findings_md(a, notes, notes_title="Analyst notes"):
    L = ["## What to change", "",
         "Ranked by the share of estimated cost each pattern touches. Shares overlap, and a share is the cost involved, not the saving.", ""]
    if not a["findings"]:
        L += ["No pattern crossed its threshold.", ""]
    for i, f in enumerate(a["findings"], 1):
        L += ["### %d. %s (%d%% of cost, %s)" % (i, f["title"], round(100 * f["share"]),
                                                 "worth a check" if f["impact"] == "check" else f["impact"] + " impact"), "",
              "Evidence:", ""] + ["- " + e for e in f["evidence"]] + ["", "Change:", ""] + ["- " + c for c in f["change"]] + [""]
    if a["minor"]:
        L += ["Checked and found small: " + "; ".join("%s (%d%%)" % (t[0].lower() + t[1:], round(100 * v)) for t, v in a["minor"]) + ".", ""]
    if notes:
        L += ["## " + notes_title, "", notes.strip(), ""]
    return L


def render(a, threshold, notes=None, with_findings=True, notes_title="Analyst notes"):
    if not a["requests"]:
        return ("# Claude usage audit\n\nNo priced requests found in the selected logs and date range.\n\n"
                + (table(("Remote source", "Result"), a["remote"]) if a.get("remote") else ""))
    T = a["total"]["cost"]
    day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
    hdr = ("Requests", "Cache read", "Cache write", "Output", "Est. cost", "Share")
    L = ["# Claude usage audit", "",
         "Period (UTC): %s to %s. Files read: %d. API requests: %d. Estimated cost at configured rates: **%s**."
         % (day(a["first"]), day(a["last"]), a["files"], a["requests"], fmt_usd(T)), "",
         "Costs are estimates from logged token counts. Covered: this machine%s."
         % ("".join(", " + h for h, res in a["remote"] if not res.startswith("failed")) if a["remote"] else " only"), ""]
    if with_findings:
        L += findings_md(a, notes, notes_title)
    L += ["## Where the money went", "",
          table(("Component", "Tokens", "Est. cost", "Share"), [
              ("Cache read (re-reading context)", fmt_tok(a["total"]["cr"]), fmt_usd(a["total"]["c_read"]), pct(a["total"]["c_read"], T)),
              ("Cache write, 1-hour lifetime", fmt_tok(a["total"]["cw1"]), fmt_usd(a["total"]["c_write1"]), pct(a["total"]["c_write1"], T)),
              ("Cache write, 5-minute lifetime", fmt_tok(a["total"]["cw5"]), fmt_usd(a["total"]["c_write5"]), pct(a["total"]["c_write5"], T)),
              ("Output", fmt_tok(a["total"]["out"]), fmt_usd(a["total"]["c_output"]), pct(a["total"]["c_output"], T)),
              ("Uncached input", fmt_tok(a["total"]["inp"]), fmt_usd(a["total"]["c_input"]), pct(a["total"]["c_input"], T)),
              ("Web search requests", "-", fmt_usd(a["total"]["c_web"]), pct(a["total"]["c_web"], T))])]
    L += ["## Cost drivers", "", "These overlap, so the shares add up to more than 100%.", "",
          table(("Driver", "Share of cost"), [(n, "%d%%" % round(100 * v)) for n, v in a["drivers"]])]
    L += ["## By model", "", table(("Model",) + hdr, bucket_rows(a["by_model"], T))]
    L += ["## By context size at request time", "", table(("Context",) + hdr, bucket_rows(a["by_context"], T))]
    L += ["## By effort level", "", table(("Effort",) + hdr, bucket_rows(a["by_effort"], T))]
    L += ["## By day (UTC)", "", table(("Day",) + hdr, bucket_rows(a["by_day"], T, 62))]
    L += ["## By project", "", table(("Project",) + hdr, bucket_rows(a["by_project"], T))]
    L += ["## By surface", "", table(("Entry point",) + hdr, bucket_rows(a["by_surface"], T))]
    if len(a["by_host"]) > 1 or a["remote"]:
        L += ["## By host", "", table(("Host",) + hdr, bucket_rows(a["by_host"], T, 40))]
        if a["remote"]:
            L += [table(("Remote source", "Result"), a["remote"])]
    L += ["## Costliest sessions", "",
          table(("Session", "Project", "Day", "Span", "Active", "Requests", "Median ctx", "Max ctx", "Subagents", "Est. cost", "Share"),
                [(s["title"], s["project"], day(s["first"]), fmt_dur(s["span"]), fmt_dur(s["active"]), s["requests"],
                  fmt_tok(s["median_ctx"]), fmt_tok(s["max_ctx"]),
                  "%d (%s)" % (s["sub_threads"], fmt_usd(s["sub_cost"])), fmt_usd(s["cost"]), pct(s["cost"], T))
                 for s in a["sessions"][:15]])]
    L += ["## Parallel sessions", "", "Cost by how many sessions were active at the moment of each request.", "",
          table(("Active sessions", "Est. cost", "Share"), [(k, fmt_usd(v), pct(v, T)) for k, v in a["concurrency"].items()])]
    L += ["## Who started the work", "", "A turn is everything that runs after one prompt, including its subagents.", "",
          table(("Turn origin", "Turns", "Requests", "Est. cost", "Share"),
                [(k, v["turns"], v["requests"], fmt_usd(v["cost"]), pct(v["cost"], T)) for k, v in a["by_origin"].items()])]
    L += ["### Prompts that fired 3+ times in one session", "", "Loops and scheduled tasks show up here. Prompts are shown as a hash or slash-command name.", "",
          table(("Session", "Prompt", "Origin", "Firings", "Median gap", "Mean context", "Est. cost"),
                [(r["session"], r["prompt_id"], r["origin"], r["firings"], fmt_dur(r["median_gap"]), fmt_tok(r["mean_ctx"]), fmt_usd(r["cost"]))
                 for r in a["repeated_prompts"][:15]])]
    L += ["### By slash command", "",
          table(("Command", "Turns", "Est. cost", "Share"),
                [(k, v["turns"], fmt_usd(v["cost"]), pct(v["cost"], T)) for k, v in list(a["by_command"].items())[:15]])]
    L += ["### Costliest single turns", "",
          table(("Session", "When (UTC)", "Origin", "Command", "Prompt chars", "Requests", "Max ctx", "Est. cost"),
                [(t["session"], t["when"], t["origin"], t["command"], t["prompt_chars"], t["requests"], fmt_tok(t["max_ctx"]), fmt_usd(t["cost"]))
                 for t in a["top_turns"]])]
    m, rb = a["cache_misses"], a["cache_rebuilds"]
    L += ["## Cache misses", "",
          "%d requests re-wrote context that should have been cached: %s tokens, %s. A further %d rebuilds followed a context drop (compaction), costing %s."
          % (m["count"], fmt_tok(m["tokens"]), fmt_usd(m["cost"]), rb["count"], fmt_usd(rb["cost"])), "",
          table(("Idle gap before the miss", "Misses", "Est. cost"), [(k, v[0], fmt_usd(v[1])) for k, v in sorted(m["by_gap"].items())]),
          "### Costliest cold resumes", "", "Each row is one request that paid to rebuild a context left idle.", "",
          table(("Session", "When (UTC)", "Thread", "Model", "Idle before", "Tokens re-written", "Est. cost"),
                [(e["session"], e["when"], e["thread"], e["model"], fmt_dur(e["idle"]), fmt_tok(e["tokens"]), fmt_usd(e["cost"]))
                 for e in a["cold_resumes"]])]
    L += ["## Subagents", "",
          table(("Type", "Model", "Threads", "Requests", "Est. cost", "Per thread", "Median start ctx", "Max ctx"),
                [(s["type"], s["model"], s["threads"], s["requests"], fmt_usd(s["cost"]), fmt_usd(s["cost_per_thread"]),
                  fmt_tok(s["median_start_ctx"]), fmt_tok(s["max_ctx"])) for s in a["subagents"][:15]]),
          "### Costliest subagent threads", "",
          table(("Session", "Started (UTC)", "Type", "Model", "Requests", "Max ctx", "Est. cost"),
                [(e["session"], e["when"], e["type"], e["model"], e["requests"], fmt_tok(e["max_ctx"]), fmt_usd(e["cost"]))
                 for e in a["subagent_threads"]]),
          "Model argument on each subagent launch (`inherit` means the session's own model was used):", "",
          table(("Type", "Model argument", "Launches"), [(c["type"], c["model_arg"], c["calls"]) for c in a["agent_calls"][:12]])]
    L += ["## Skills and MCP servers", "", "As attributed by Claude Code on each request.", "",
          table(("Skill",) + hdr, bucket_rows(a["by_skill"], T, 10)),
          table(("MCP server",) + hdr, bucket_rows(a["by_mcp_attr"], T, 10))]
    L += ["## Tool traffic by size", "",
          "Arguments are output tokens the model wrote to call the tool, such as a document body. Results are input pulled into context. Both are then re-read on every later request in that session. Estimated at %d characters per token." % CHARS_PER_TOKEN, "",
          table(("Tool", "Calls", "Argument tokens (est.)", "Result tokens (est.)"),
                [(t["tool"], t["calls"], fmt_tok(t["arg_tokens_est"]), fmt_tok(t["result_tokens_est"])) for t in a["tools"]])]
    d = a["duplicate_payloads"]
    L += ["## Repeated long tool arguments", "",
          "The same body (80%%+ identical lines) emitted more than once in a session: %d cases, about %s repeated output tokens, %s. Of that, %s crossed two different tools, which is the double-write pattern."
          % (d["clusters"], fmt_tok(d["repeat_tokens_est"]), fmt_usd(d["repeat_cost_est"]), fmt_usd(d["cross_tool_cost_est"])), "",
          table(("Session", "File", "Tools", "Emissions", "Largest (chars)", "Repeat tokens (est.)", "Est. cost"),
                [(c["session"], c["file"], ", ".join(c["tools"]), c["emissions"], c["largest_chars"], fmt_tok(c["repeat_tokens_est"]), fmt_usd(c["repeat_cost_est"]))
                 for c in d["top"]])]
    L += ["## Files revised 3+ times in a session", "",
          table(("Session", "File", "Full writes", "Edits", "Emitted tokens (est.)"),
                [(c["session"], c["file"], c["writes"], c["edits"], fmt_tok(c["emitted_tokens_est"])) for c in a["file_churn"]])]
    L += ["## Targets read 3+ times in a session", "",
          table(("Session", "Target", "Reads", "Result tokens (est.)"),
                [(c["session"], c["target"], c["reads"], fmt_tok(c["result_tokens_est"])) for c in a["repeated_reads"]])]
    pc = a["price_check"]
    L += ["## Checks and caveats", "",
          "- Price table check: %d of %d cost records that Claude Code logged are consistent with the configured rates."
          % (pc["matched"], pc["matched"] + pc["mismatched"]),
          "- Requests with no price for their model: %d. Unreadable log lines: %d." % (a["total"]["unpriced"], a["bad_lines"]),
          "- Cost from work whose starting prompt is no longer in the logs: %s." % fmt_usd(a["unattributed_turn_cost"]),
          "- Token estimates marked (est.) use characters divided by %d. Everything else is logged token counts." % CHARS_PER_TOKEN,
          "- Side calls that tools make on a small model (for example web fetch summaries) are not logged per request and are not included.",
          "- Web-hosted chat and sessions on other machines leave no local logs and are not included.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- html report

CSS = """
:root{ /* layout: one readable column; summary tiles, then findings cards, then reference tables */
  --bg:#f5f6f8; --surface:#ffffff; --ink:#171c26; --muted:#5b6474; --line:#dde2ea; --accent:#2a5bd7;
  --high:#b3261e; --medium:#8a5d00; --low:#5b6474;
  --display:"Avenir Next Condensed","Roboto Condensed","Arial Narrow",system-ui,sans-serif;
  --body:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace; }
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){ color-scheme:dark;
  --bg:#10141b; --surface:#171d27; --ink:#e7ebf2; --muted:#9aa5b8; --line:#2a3342; --accent:#8fb1ff;
  --high:#ff8a80; --medium:#f2c14e; --low:#9aa5b8; } }
:root[data-theme="dark"]{ color-scheme:dark;
  --bg:#10141b; --surface:#171d27; --ink:#e7ebf2; --muted:#9aa5b8; --line:#2a3342; --accent:#8fb1ff;
  --high:#ff8a80; --medium:#f2c14e; --low:#9aa5b8; }
*{box-sizing:border-box} html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--body)}
main{max-width:1040px;margin:0 auto;padding:32px 20px 64px;display:flex;flex-direction:column;gap:20px}
h1,h2,h3{font-family:var(--display);text-wrap:balance;margin:0;line-height:1.2}
h1{font-size:34px;letter-spacing:.01em} h2{font-size:24px;margin-top:20px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:18px} h4{font:600 11px/1.2 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}
p{margin:0;max-width:72ch} .muted{color:var(--muted)} code{font-family:var(--mono);font-size:.92em}
ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.tile b{display:block;font:600 24px/1.2 var(--mono);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tile span{font:600 11px/1.2 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.finding{background:var(--surface);border:1px solid var(--line);border-left:5px solid var(--low);border-radius:8px;padding:16px 18px;display:flex;flex-direction:column;gap:12px}
.finding.high{border-left-color:var(--high)} .finding.medium{border-left-color:var(--medium)}
.finding header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 12px}
.pill{font:600 12px/1 var(--mono);padding:5px 8px;border-radius:999px;border:1px solid currentColor;color:var(--low);white-space:nowrap}
.high .pill{color:var(--high)} .medium .pill{color:var(--medium)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:7px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font:600 11px/1.3 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
td:first-child{min-width:170px} tr:last-child td{border-bottom:0} td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.share{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end}
.track{width:88px;height:8px;background:var(--line);border-radius:0 4px 4px 0;overflow:hidden;display:inline-block}
.fill{display:block;height:100%;background:var(--accent);border-radius:0 4px 4px 0}
"""
NUM_RE = re.compile(r"^(-|n/a|\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?|\$?[\d.,]+[kMBhms%]?( \(\$[\d.,]+\))?)$")


def inline_md(t):
    t = html.escape(t, quote=True)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return re.sub(r"(?<![\w])_([^_]+)_(?![\w])", r"<em>\1</em>", t)


def md_to_html(md):
    """Convert the small Markdown subset this report uses: headings, paragraphs, lists, tables."""
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], rows[2:]
            numeric = [j > 0 and bool(body) and all(NUM_RE.match(r[j]) for r in body if j < len(r))
                       for j in range(len(head))]   # the first column is always a label
            t = ["<div class=\"tablewrap\"><table><thead><tr>"]
            t += ["<th%s>%s</th>" % (" class=\"num\"" if numeric[j] else "", inline_md(h)) for j, h in enumerate(head)]
            t.append("</tr></thead><tbody>")
            for r in body:
                t.append("<tr>")
                for j, c in enumerate(r):
                    m = re.match(r"^(\d+)%$", c)
                    if m and j < len(head) and head[j].startswith("Share"):
                        w = min(100, int(m.group(1)))
                        t.append("<td class=\"num\"><span class=\"share\" title=\"%s: %s\">%s<span class=\"track\">"
                                 "<span class=\"fill\" style=\"width:%d%%\"></span></span></span></td>"
                                 % (html.escape(r[0], quote=True), c, c, w))
                    else:
                        t.append("<td%s>%s</td>" % (" class=\"num\"" if j < len(numeric) and numeric[j] else "", inline_md(c)))
                t.append("</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue
        if ln.startswith("#"):
            n = len(ln) - len(ln.lstrip("#"))
            out.append("<h%d>%s</h%d>" % (n, inline_md(ln[n:].strip()), n))
        elif ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append("<li>%s</li>" % inline_md(lines[i][2:]))
                i += 1
            out.append("<ul>%s</ul>" % "".join(items))
            continue
        elif ln.strip():
            out.append("<p>%s</p>" % inline_md(ln))
        i += 1
    return "\n".join(out)


def render_html(a, threshold, notes=None, notes_title="Analyst notes"):
    """One self-contained page: no scripts, no network requests, light and dark themes."""
    esc = lambda t: html.escape(str(t), quote=True)
    md = render(a, threshold, with_findings=False)
    parts = ["<h1>Claude usage audit</h1>"]
    title = "Claude usage audit"
    if a["requests"]:
        day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
        title = "Claude usage audit %s to %s" % (day(a["first"]), day(a["last"]))
        remote_ok = [h for h, res in a["remote"] if not res.startswith("failed")]
        tiles = [("Estimated cost", fmt_usd(a["total"]["cost"])), ("API requests", "{:,}".format(a["requests"])),
                 ("Period (UTC)", "%s to %s" % (day(a["first"]), day(a["last"]))),
                 ("Sources", "this machine" + (" + %d remote" % len(remote_ok) if remote_ok else ""))]
        parts.append("<div class=\"tiles\">%s</div>" % "".join(
            "<div class=\"tile\"><span>%s</span><b>%s</b></div>" % (esc(k), esc(v)) for k, v in tiles))
        parts.append("<p class=\"muted\">Costs are estimates from logged token counts at the configured rates. "
                     "This page holds numbers and short labels only. It contains no prompt or document text.</p>")
        parts.append("<h2>What to change</h2><p class=\"muted\">Ranked by the share of estimated cost each pattern "
                     "touches. Shares overlap, and a share is the cost involved, not the saving.</p>")
        if not a["findings"]:
            parts.append("<p>No pattern crossed its threshold.</p>")
        for i, f in enumerate(a["findings"], 1):
            parts.append(
                "<section class=\"finding %s\"><header><span class=\"pill\">%s · %d%% of cost</span>"
                "<h3>%d. %s</h3></header><div class=\"cols\"><div><h4>Evidence</h4><ul>%s</ul></div>"
                "<div><h4>Change</h4><ul>%s</ul></div></div></section>"
                % (f["impact"], "Worth a check" if f["impact"] == "check" else f["impact"].capitalize() + " impact",
                   round(100 * f["share"]), i, esc(f["title"]),
                   "".join("<li>%s</li>" % esc(e) for e in f["evidence"]),
                   "".join("<li>%s</li>" % esc(c) for c in f["change"])))
        if a["minor"]:
            parts.append("<p class=\"muted\">Checked and found small: %s.</p>" % esc("; ".join(
                "%s (%d%%)" % (t[0].lower() + t[1:], round(100 * v)) for t, v in a["minor"])))
        if notes:
            parts.append("<h2>%s</h2>" % esc(notes_title) + md_to_html(notes.strip()))
        cut = md.find("## Where the money went")
        parts.append(md_to_html(md[cut:] if cut >= 0 else ""))
    else:
        parts.append(md_to_html(md.split("\n", 1)[1] if "\n" in md else ""))
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s</title><style>%s</style></head>\n<body><main>\n%s\n</main></body></html>\n"
            % (esc(title), CSS, "\n".join(parts)))


# ---------------------------------------------------------------- export, merge, remote hosts

def export_store(st, since, until, redact):
    """Numbers and labels for every request, with no prompt, response, or document text."""
    reqs = [r for r in st.reqs.values() if r["ts"] is not None
            and (since is None or r["ts"] >= since) and (until is None or r["ts"] < until)]
    live = {r["id"] for r in reqs}
    tools = {t: v for t, v in st.tool_owner.items() if v[1] in live}
    host = socket.gethostname() or "remote"
    data = {"format": EXPORT_FORMAT, "tool_version": VERSION,
            "host_label": "host-" + sha(host, 6) if redact else host,
            "reqs": reqs, "turns": st.turns, "tool_owner": tools,
            "tool_meta": {t: list(v) for t, v in st.tool_meta.items() if t in tools},
            "agent_launch": {t: v for t, v in st.agent_launch.items() if t in tools},
            "tool_result_chars": {t: v for t, v in st.tool_result_chars.items() if t in tools},
            "tool_arg_chars": {t: v for t, v in st.tool_arg_chars.items() if t in tools},
            "titles": dict(st.titles), "agent_meta": st.agent_meta,
            "payloads": [dict(p, lines=sorted(p["lines"])) for p in st.payloads if p["mid"] in live],
            "cost_states": list(st.cost_states.values()),
            "files": st.files, "lines": st.lines, "bad_lines": st.bad_lines}
    if redact:
        data["titles"] = {k: "sess-" + sha(v, 6) for k, v in st.titles.items()}
        for r in reqs:
            r["project"] = "proj-" + sha(r["project"], 6)
        for v in data["tool_meta"].values():
            v[1] = "file-" + sha(v[1], 6)
        for p in data["payloads"]:
            if p["label"]:
                p["label"] = "file-" + sha(p["label"], 6)
    return data


def merge_store(st, data, host):
    """Fold an export into the store. Returns (requests in export, requests not seen before)."""
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
        raise ValueError("not a claude-usage-audit export")
    new = 0
    for r in data["reqs"]:
        if r["id"] not in st.reqs:
            r["host"] = host
            st.reqs[r["id"]] = r
            new += 1
    for k, t in data["turns"].items():
        st.turns.setdefault(k, t)
    for t, v in data["tool_owner"].items():
        st.tool_owner.setdefault(t, tuple(v))
    for t, v in data["tool_meta"].items():
        st.tool_meta.setdefault(t, v)
    for t, v in data["agent_launch"].items():
        st.agent_launch.setdefault(t, v)
    for t, v in data["tool_result_chars"].items():
        st.tool_result_chars[t] = max(st.tool_result_chars.get(t, 0), v)
    for t, v in data.get("tool_arg_chars", {}).items():
        st.tool_arg_chars.setdefault(t, v)
    for k, v in data["titles"].items():
        st.titles.setdefault(k, v)
    for k, v in data["agent_meta"].items():
        st.agent_meta.setdefault(k, v)
    for p in data["payloads"]:
        key = (p.get("tid"), p["key"])
        if key not in st.payload_keys:
            st.payload_keys.add(key)
            p["lines"] = frozenset(p["lines"])
            st.payloads.append(p)
    for mu in data["cost_states"]:
        st.cost_states[json.dumps(mu, sort_keys=True)] = mu
    st.files += data.get("files", 0)
    st.lines += data.get("lines", 0)
    st.bad_lines += data.get("bad_lines", 0)
    return len(data["reqs"]), new


def fetch_remote(host, since, until, redact, timeout):
    """Run this same script on a remote host over SSH and return its export.

    The script is piped to the remote python3 over stdin, so nothing is installed
    there, and only the numeric export travels back. Transcripts stay on the host.
    """
    if not HOST_RE.match(host):
        raise ValueError("not a valid host name or address")
    remote = ["python3", "-", "--export", "-", "--no-remote"]
    if since:
        remote += ["--since", since]
    if until:
        remote += ["--until", until]
    if redact:
        remote.append("--redact")
    cmd = ["ssh", "-o", "ConnectTimeout=10", host, " ".join(shlex.quote(x) for x in remote)]
    with open(os.path.abspath(__file__), "rb") as src:
        p = subprocess.run(cmd, stdin=src, stdout=subprocess.PIPE, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("ssh or the remote script exited with status %d" % p.returncode)
    text = p.stdout.decode("utf-8", "replace")
    start = text.find('{"format"')          # skip anything a login script printed first
    if start < 0:
        raise RuntimeError("no export in the remote output")
    return json.loads(text[start:])


def split_hosts(text):
    return [h for h in re.split(r"[\s,]+", text or "") if h]


def ask_hosts():
    sys.stderr.write("Remote hosts where you also run Claude (IPs or SSH names, separated by spaces;\n"
                     "press Enter for none): ")
    sys.stderr.flush()
    return split_hosts(sys.stdin.readline())


# ---------------------------------------------------------------- cli

def parse_when(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dh])", s)
    if m:
        delta = dt.timedelta(days=int(m.group(1))) if m.group(2) == "d" else dt.timedelta(hours=int(m.group(1)))
        return (dt.datetime.now(dt.timezone.utc) - delta).timestamp()
    return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="log folders or .jsonl files (default: this machine's Claude Code and desktop-app logs)")
    ap.add_argument("--since", help="start: 7d, 36h, or YYYY-MM-DD (UTC)")
    ap.add_argument("--until", help="end, exclusive: YYYY-MM-DD (UTC)")
    ap.add_argument("--prices", help="JSON price table to use instead of built-in list prices")
    ap.add_argument("--dump-prices", action="store_true", help="print the built-in price table as JSON and exit")
    ap.add_argument("--md", help="write the Markdown report to this file (default: stdout)")
    ap.add_argument("--html", help="write a self-contained HTML report to this file")
    ap.add_argument("--notes", help="Markdown file with written analysis to include in the report")
    ap.add_argument("--notes-title", default="Analyst notes", help="heading for the --notes section (default: Analyst notes)")
    ap.add_argument("--json", help="also write the full results as JSON to this file")
    ap.add_argument("--redact", action="store_true", help="replace session, project, file and MCP names with hashes")
    ap.add_argument("--context-threshold", type=int, default=150000, help="context size treated as large (default 150000)")
    ap.add_argument("--hosts", help="remote hosts to include over SSH, separated by commas or spaces")
    ap.add_argument("--no-remote", action="store_true", help="do not ask about remote hosts")
    ap.add_argument("--ssh-timeout", type=int, default=600, help="seconds to wait for each remote host (default 600)")
    ap.add_argument("--export", metavar="FILE", help="write a numeric export of this machine's logs (- for stdout) and exit")
    ap.add_argument("--merge", metavar="FILE", nargs="+", default=[], help="include export files made with --export on other machines")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    if args.dump_prices:
        json.dump(DEFAULT_PRICES, sys.stdout, indent=2)
        print()
        return 0
    prices = DEFAULT_PRICES
    if args.prices:
        with open(args.prices) as fh:
            prices = json.load(fh)
    since, until = parse_when(args.since), parse_when(args.until)
    roots = args.roots or default_roots()
    st = Store()
    st.unknown_ttl = prices.get("cache_write_ttl_when_unknown", "1h")
    if args.export:
        for path in iter_log_files(roots):
            ingest_file(path, st)
        data = export_store(st, since, until, args.redact)
        if args.export == "-":
            json.dump(data, sys.stdout)
        else:
            with open(args.export, "w") as fh:
                json.dump(data, fh)
        return 0
    hosts = split_hosts(args.hosts)
    if not hosts and not args.no_remote and sys.stdin.isatty():
        hosts = ask_hosts()
    if not roots and not hosts and not args.merge:
        sys.exit("No log folders found. Pass one or more folders, --hosts, or --merge.")
    for path in iter_log_files(roots):
        ingest_file(path, st)
    for path in args.merge:
        try:
            with open(path) as fh:
                data = json.load(fh)
            total, new = merge_store(st, data, data.get("host_label") or os.path.basename(path))
            st.remote_status.append((data.get("host_label") or os.path.basename(path),
                                     "merged from file: %d requests, %d not already counted" % (total, new)))
        except (OSError, ValueError, KeyError) as e:
            st.remote_status.append((os.path.basename(path), "failed: %s" % e))
    for host in hosts:
        sys.stderr.write("Reading %s over SSH...\n" % host)
        try:
            total, new = merge_store(st, fetch_remote(host, args.since, args.until, args.redact, args.ssh_timeout), host)
            outcome = "ok: %d requests, %d not already counted" % (total, new)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as e:
            outcome = "failed: %s" % e
        sys.stderr.write("  %s: %s\n" % (host, outcome))
        st.remote_status.append((host, outcome))
    a = analyze(st, prices, since, until, args.context_threshold, Labeler(args.redact))
    notes = None
    if args.notes:
        with open(args.notes) as fh:
            notes = fh.read()
    if args.html:
        with open(args.html, "w") as fh:
            fh.write(render_html(a, args.context_threshold, notes, args.notes_title))
        print("HTML report written to %s" % args.html, file=sys.stderr)
    report = render(a, args.context_threshold, notes, notes_title=args.notes_title)
    if args.md:
        with open(args.md, "w") as fh:
            fh.write(report)
        print("Report written to %s" % args.md, file=sys.stderr)
    elif not args.html:
        sys.stdout.write(report)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(a, fh, indent=1, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o))
    return 0


if __name__ == "__main__":
    sys.exit(main())
