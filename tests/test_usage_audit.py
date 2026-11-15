import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import usage_audit as ua  # noqa: E402

BODY = "\n".join("Design decision number %d for the leaf-spine fabric." % i for i in range(40))


def usage(inp=10, cr=0, cw1=0, out=100):
    return {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw1,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": cw1},
            "output_tokens": out, "speed": "standard", "inference_geo": "global"}


def assistant(mid, ts, sid="s1", content=None, agent=None, model="claude-opus-5-5", **u):
    rec = {"type": "assistant", "sessionId": sid, "timestamp": ts, "uuid": mid + "-u", "cwd": "/work/fabric",
           "entrypoint": "cli", "effort": "high",
           "message": {"id": mid, "model": model, "usage": usage(**u), "content": content or []}}
    if agent:
        rec.update(agentId=agent, isSidechain=True)
    return rec


def prompt(uuid, ts, text, sid="s1", kind="human"):
    return {"type": "user", "sessionId": sid, "timestamp": ts, "uuid": uuid, "origin": {"kind": kind},
            "message": {"role": "user", "content": text}}


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


class AuditTest(unittest.TestCase):
    def write_logs(self, files, d=None):
        d = d or tempfile.mkdtemp()
        for name, recs in files.items():
            path = os.path.join(d, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                for r in recs:
                    fh.write(json.dumps(r) + "\n")
        return d

    def load(self, files):
        st = ua.Store()
        for p in ua.iter_log_files([self.write_logs(files)]):
            ua.ingest_file(p, st)
        return st

    def run_audit(self, files):
        return ua.analyze(self.load(files), ua.DEFAULT_PRICES, None, None, 150000, ua.Labeler(False))

    def test_counts_each_response_once(self):
        a1 = assistant("m1", "2026-11-01T10:00:01Z", cr=200000, cw1=1000, out=500)
        a = self.run_audit({
            "proj/s1.jsonl": [prompt("p1", "2026-11-01T10:00:00Z", "plan the fabric"), a1, a1],
            "proj/s2.jsonl": [a1],  # copy carried into a forked session file
        })
        self.assertEqual(a["requests"], 1)
        expected = (10 * 4 + 200000 * 0.20 + 1000 * 8 + 500 * 20) / 1e6
        self.assertAlmostEqual(a["total"]["cost"], expected, places=9)
        self.assertEqual(a["by_context"]["150k-250k"]["requests"], 1)

    def test_subagent_and_origin_attribution(self):
        a = self.run_audit({
            "proj/s1.jsonl": [
                prompt("p1", "2026-11-01T10:00:00Z", "plan the fabric"),
                assistant("m1", "2026-11-01T10:00:01Z", out=100),
                prompt("p2", "2026-11-01T10:10:00Z", "<task-notification>done</task-notification>", kind="task-notification"),
                assistant("m2", "2026-11-01T10:10:01Z", out=100),
            ],
            "proj/s1/subagents/agent-a1.jsonl": [
                assistant("m3", "2026-11-01T10:05:00Z", agent="a1", model="claude-sonnet-5-5", out=100),
            ],
        })
        self.assertEqual(a["requests"], 3)
        self.assertEqual(a["by_origin"]["human"]["requests"], 2)  # main request plus its subagent
        self.assertEqual(a["by_origin"]["task-notification"]["requests"], 1)
        self.assertEqual(a["subagents"][0]["model"], "claude-sonnet-5-5")
        self.assertIn("claude-sonnet-5-5 (subagent)", a["by_model"])

    def test_repeated_prompt_and_double_write(self):
        recs = []
        for i in range(3):
            recs.append(prompt("p%d" % i, "2026-11-01T1%d:00:00Z" % i, "check the build status"))
            recs.append(assistant("m%d" % i, "2026-11-01T1%d:00:05Z" % i, out=50))
        recs.append(prompt("p9", "2026-11-01T14:00:00Z", "publish the design"))
        recs.append(assistant("m9", "2026-11-01T14:00:05Z", out=4000, content=[
            tool_use("t1", "Write", file_path="/work/fabric/DESIGN.md", content=BODY),
            tool_use("t2", "mcp__docs__publish_page", title="Design", body=BODY + "\nFooter line added."),
        ]))
        a = self.run_audit({"proj/s1.jsonl": recs})
        self.assertEqual(a["repeated_prompts"][0]["firings"], 3)
        self.assertAlmostEqual(a["repeated_prompts"][0]["median_gap"], 3600)
        dup = a["duplicate_payloads"]
        self.assertEqual(dup["clusters"], 1)
        self.assertTrue(dup["top"][0]["crosses_tools"])
        self.assertEqual(dup["top"][0]["file"], "DESIGN.md")

    def test_report_has_no_prompt_text(self):
        secret = "plan the fabric for the unannounced site"
        a = self.run_audit({"proj/s1.jsonl": [prompt("p1", "2026-11-01T10:00:00Z", secret),
                                               assistant("m1", "2026-11-01T10:00:01Z")]})
        self.assertNotIn("unannounced", ua.render(a, 150000))
        self.assertNotIn("unannounced", ua.render_html(a, 150000))

    def test_findings_and_html_report(self):
        recs = [prompt("p1", "2026-11-01T10:00:00Z", "plan the fabric")]
        recs += [assistant("m%d" % i, "2026-11-01T10:%02d:00Z" % (i + 1), cr=300000, out=200) for i in range(5)]
        recs.append(assistant("m9", "2026-11-01T13:00:00Z", cr=0, cw1=300000, out=200))  # resumed cold after 2h
        a = self.run_audit({"proj/s1.jsonl": recs})
        titles = [f["title"] for f in a["findings"]]
        self.assertIn("Most spend happens at very large context", titles)
        self.assertIn("Idle sessions are resumed after their cache has expired", titles)
        self.assertEqual(a["cache_misses"]["by_gap"]["over 1h"][0], 1)
        page = ua.render_html(a, 150000, notes="Start with **handoff notes**.\n\n- one\n- two", notes_title="Lessons")
        self.assertIn("<h2>Lessons</h2>", page)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("<strong>handoff notes</strong>", page)
        self.assertIn("<li>one</li>", page)
        self.assertIn('class="fill" style="width:', page)
        self.assertNotIn("<script", page)
        self.assertNotIn("http", page)                      # nothing loads from the network
        self.assertIn("## What to change", ua.render(a, 150000))
        self.assertNotIn("unannounced", json.dumps(a, default=str))

    def test_export_merge_counts_shared_requests_once(self):
        shared = assistant("m1", "2026-11-01T10:00:01Z", out=100)
        remote_files = {"proj/s1.jsonl": [
            prompt("p1", "2026-11-01T10:00:00Z", "design the unannounced site"), shared,
            assistant("m2", "2026-11-01T10:01:00Z", out=100, content=[
                tool_use("t1", "Write", file_path="/srv/DESIGN.md", content=BODY)])]}
        export = json.loads(json.dumps(ua.export_store(self.load(remote_files), None, None, False)))
        self.assertNotIn("unannounced", json.dumps(export))       # no prompt text leaves the host
        self.assertNotIn("leaf-spine", json.dumps(export))        # no document text either
        local = self.load({"proj/ssh-s1/s1.jsonl": [shared]})    # local mirror of the same session
        total, new = ua.merge_store(local, export, "10.0.0.5")
        self.assertEqual((total, new), (2, 1))
        a = ua.analyze(local, ua.DEFAULT_PRICES, None, None, 150000, ua.Labeler(False))
        self.assertEqual(a["requests"], 2)
        self.assertEqual(a["by_host"]["10.0.0.5"]["requests"], 1)
        self.assertEqual(a["by_host"]["local"]["requests"], 1)

    def test_rejects_hosts_that_look_like_ssh_options(self):
        for bad in ("-oProxyCommand=touch /tmp/x", "host;rm -rf", "a b", ""):
            with self.assertRaises(ValueError):
                ua.fetch_remote(bad, None, None, False, 5)

    @unittest.skipIf(os.name == "nt", "needs a POSIX shell")
    def test_end_to_end_over_a_stand_in_ssh(self):
        # A fake "ssh" that ignores the host and runs the remote command locally,
        # with HOME pointing at a folder that plays the remote machine.
        remote_home = tempfile.mkdtemp()
        self.write_logs({".claude/projects/proj/s9.jsonl": [
            prompt("p1", "2026-11-01T10:00:00Z", "remote work", sid="s9"),
            assistant("m9", "2026-11-01T10:00:01Z", sid="s9", cr=200000, out=500)]}, remote_home)
        bindir = tempfile.mkdtemp()
        shim = os.path.join(bindir, "ssh")
        with open(shim, "w") as fh:
            fh.write('#!/bin/sh\necho "motd noise"\nshift 3\nHOME="%s" exec sh -c "$1"\n' % remote_home)
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
        empty_local = tempfile.mkdtemp()
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "usage_audit.py")
        out = subprocess.run([sys.executable, script, empty_local, "--hosts", "100.64.0.9,bad;host"],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        report = out.stdout.decode()
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        self.assertIn("API requests: 1.", report)
        self.assertIn("| 100.64.0.9 | ok: 1 requests, 1 not already counted |", report)
        self.assertIn("| bad;host | failed: not a valid host name or address |", report)


if __name__ == "__main__":
    unittest.main()
