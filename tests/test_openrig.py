import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import agent_info as info
import openrig
from test_agent_info import IsolatedTestCase

NODES = json.loads((Path(__file__).parent / "fixtures/openrig-nodes.json").read_text())
CLAUDE_TOKEN = NODES[0]["resumeToken"]
RIGS = [{"rigId": "01RIG", "rigName": "boxbox"}]


def tile(session, pid=7):
    """Herdr's process info for a tile attached to an OpenRig seat."""
    argv = ["/opt/homebrew/bin/tmux", "attach", "-t", session]
    return {"process_info": {"foreground_processes": [{"pid": pid, "argv": argv}]}}


def daemon(rigs=RIGS, nodes=NODES, items=()):
    def fetch(url):
        if url.endswith("/api/ps"):
            return rigs
        if "/api/queue/list?state=" in url:
            return list(items)
        if url.endswith("/api/rigs/01RIG/nodes"):
            return nodes
        raise AssertionError(url)
    return Mock(side_effect=fetch)


class AttachTests(unittest.TestCase):
    def test_reads_the_target_of_any_attach_form(self):
        for argv, expected in [
            (["tmux", "attach", "-t", "dev-build@boxbox"], "dev-build@boxbox"),
            (["/usr/bin/tmux", "attach", "-r", "-t", "a@b"], "a@b"),
            (["tmux", "-L", "openrig", "attach-session", "-t", "=a@b"], "a@b"),
            (["tmux", "-S", "/tmp/sock", "a", "-ta@b:1"], "a@b"),
            (["tmux", "new-session", "-t", "a@b"], None),
            (["ssh", "host", "tmux", "attach", "-t", "a@b"], None),
            (["tmux", "attach"], None),
            ([], None), (None, None), ([3], None),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(openrig.attached_session(argv), expected)

    def test_only_a_daemon_on_this_machine_is_asked(self):
        self.assertEqual(openrig.daemon_url({}), openrig.DEFAULT_URL)
        self.assertEqual(openrig.daemon_url({"OPENRIG_URL": "http://localhost:7500/"}), "http://localhost:7500")
        self.assertIsNone(openrig.daemon_url({"OPENRIG_URL": "http://10.0.0.5:7433"}))
        self.assertIsNone(openrig.daemon_url({"OPENRIG_URL": "http://[bad"}))

    def test_context_needs_a_known_fresh_reading(self):
        self.assertEqual(openrig.context_reading(NODES[0]), (None, 1000000, 7))
        stale = dict(NODES[0], contextUsage=dict(NODES[0]["contextUsage"], fresh=False))
        self.assertIsNone(openrig.context_reading(stale))
        self.assertIsNone(openrig.context_reading({"contextUsage": {"availability": "unknown"}}))
        self.assertIsNone(openrig.context_reading({}))

    def test_session_only_for_a_seat_of_the_same_kind(self):
        self.assertEqual(openrig.agent_session(NODES[0], "claude")["value"], CLAUDE_TOKEN)
        self.assertIsNone(openrig.agent_session(NODES[0], "codex"))
        self.assertIsNone(openrig.agent_session(dict(NODES[0], resumeToken=None), "claude"))
        self.assertEqual(openrig.node_list({"nodes": NODES}), NODES)
        self.assertEqual(openrig.node_list("text"), [])


class BridgeTests(unittest.TestCase):
    def bridge(self, fetch, clock):
        rpc = Mock(return_value=tile("dev-build@boxbox"))
        return openrig.Bridge("sock", rpc, fetch=fetch, url=openrig.DEFAULT_URL, clock=clock), rpc

    def test_maps_a_tile_to_its_seat_and_caches_both_lookups(self):
        now = [0.0]
        fetch = daemon()
        bridge, rpc = self.bridge(fetch, lambda: now[0])
        self.assertEqual(bridge.seat("w1:p5")["resumeToken"], CLAUDE_TOKEN)
        now[0] = 5
        bridge.seat("w1:p5")
        self.assertEqual((rpc.call_count, fetch.call_count), (1, 2))
        now[0] = openrig.PANE_SECONDS + 1
        bridge.seat("w1:p5")
        self.assertEqual((rpc.call_count, fetch.call_count), (2, 4))

    def test_an_absent_daemon_is_not_asked_again_until_the_backoff_ends(self):
        now = [0.0]
        fetch = Mock(side_effect=ConnectionRefusedError())
        bridge, rpc = self.bridge(fetch, lambda: now[0])
        self.assertIsNone(bridge.seat("w1:p5"))
        now[0] = openrig.NODES_SECONDS + 1
        self.assertIsNone(bridge.seat("w1:p5"))
        self.assertEqual(fetch.call_count, 1)
        now[0] = openrig.BACKOFF_SECONDS + 1
        bridge.seat("w1:p5")
        self.assertEqual(fetch.call_count, 2)

    def test_unknown_rig_and_non_tile_panes_find_nothing(self):
        bridge, rpc = self.bridge(daemon(rigs=[]), lambda: 0)
        self.assertIsNone(bridge.seat("w1:p5"))
        rpc.return_value = {"process_info": {"foreground_processes": [{"argv": ["zsh"]}]}}
        self.assertIsNone(bridge.seat("w1:p6"))
        bridge.forget({"w1:p6"})
        self.assertEqual(set(bridge.panes), {"w1:p6"})


class CollectorBridgeTests(IsolatedTestCase):
    def test_an_openrig_claude_seat_gets_its_session_and_context(self):
        collector = self.collector()
        collector.openrig = openrig.Bridge("sock", Mock(return_value=tile("dev-build@boxbox")),
                                           fetch=daemon(), url=openrig.DEFAULT_URL)
        transcript = self.home / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "sessionId": CLAUDE_TOKEN,
                                          "timestamp": "2026-01-01T00:00:10Z",
                                          "message": {"model": "claude-opus-5-5"}}) + "\n")
        collector.discover = lambda wanted: collector.paths.update({CLAUDE_TOKEN: transcript})
        status, reports, calls = self.sync(collector, [{"pane_id": "w1:p5", "agent": "claude"}])
        self.assertEqual(reports["w1:p5"]["agent_info"], "Claude Opus 5.5 · 7%/1M")
        self.assertEqual(status["missing_session"], {})
        self.assertEqual(status["missing_context"], 0)

    def test_work_is_the_seats_task_else_its_own_title(self):
        items = [{"destinationSession": "dev-build@boxbox", "summary": "Old task", "tsUpdated": "2026-10-22T01:00:00Z"},
                 {"destinationSession": "dev-build@boxbox", "summary": " Add filters ", "tsUpdated": "2026-10-22T02:00:00Z"},
                 {"destinationSession": "dev-qa@boxbox", "summary": "Not mine", "tsUpdated": "2026-10-22T03:00:00Z"}]
        collector = self.collector()
        collector.openrig = openrig.Bridge("sock", Mock(return_value=tile("dev-build@boxbox")),
                                           fetch=daemon(items=items), url=openrig.DEFAULT_URL)
        transcript = self.home / "transcript.jsonl"
        lines = [{"type": "custom-title", "customTitle": "dev-build@boxbox", "sessionId": CLAUDE_TOKEN},
                 {"type": "assistant", "sessionId": CLAUDE_TOKEN, "message": {"model": "claude-opus-5-5"}}]
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
        collector.discover = lambda wanted: collector.paths.update({CLAUDE_TOKEN: transcript})
        agents = [{"pane_id": "w1:p5", "agent": "claude"}]
        status, reports, calls = self.sync(collector, agents)
        self.assertEqual(reports["w1:p5"]["agent_work"], "Add filters")
        # Without a task, OpenRig's own seat-name title is not shown as work.
        collector.openrig.items, collector.openrig.items_at = [], float("inf")
        status, reports, calls = self.sync(collector, agents)
        self.assertIsNone(reports["w1:p5"]["agent_work"])

    def test_a_plain_agents_work_is_its_session_title(self):
        collector = self.collector()
        transcript = self.home / "transcript.jsonl"
        lines = [{"type": "ai-title", "aiTitle": "Fix login", "sessionId": CLAUDE_TOKEN},
                 {"type": "custom-title", "customTitle": "Login bug", "sessionId": CLAUDE_TOKEN},
                 {"type": "assistant", "sessionId": CLAUDE_TOKEN, "message": {"model": "claude-opus-5-5"}}]
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
        collector.discover = lambda wanted: collector.paths.update({CLAUDE_TOKEN: transcript})
        agent = {"pane_id": "w1:p1", "agent": "claude", "agent_session": {"kind": "id", "value": CLAUDE_TOKEN}}
        status, reports, calls = self.sync(collector, [agent])
        self.assertEqual(reports["w1:p1"]["agent_work"], "Login bug")
        # A status-line capture replaces the observation but keeps the title.
        info.atomic_json(collector.directory / (info.digest(CLAUDE_TOKEN) + ".json"), {
            "pane_id": "w1:p1", "captured_at": 9e9,
            "observation": {"model": "Opus 5.5", "model_id": "claude-opus-5-5", "context": info.context(1, 4)}})
        status, reports, calls = self.sync(collector, [agent])
        self.assertEqual(reports["w1:p1"]["agent_work"], "Login bug")
        index = self.home / ".codex" / "session_index.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text('{"id":"t1","thread_name":"Old"}\n{"id":"t1","thread_name":"Keep work"}\nnot json\n')
        self.assertEqual(info.thread_names(index.read_text()), {"t1": "Keep work"})

    def test_work_can_replace_the_tab_name_in_the_first_row(self):
        import tomllib
        import sidebar_settings as prefs
        config = (Path(__file__).parent / "fixtures/config.toml").read_text()
        def first_row(settings):
            rows = tomllib.loads(prefs.render_config(config, settings))["ui"]["sidebar"]["agents"]["rows"]
            return [token["token"] for token in rows[0] if isinstance(token, dict)][-1]
        self.assertEqual(first_row(prefs.DEFAULTS), "tab")
        self.assertEqual(first_row(prefs.DEFAULTS | {"show_work": True}), "$agent_work")

    def test_a_plain_agent_without_work_keeps_its_tab_name_an_openrig_seat_does_not(self):
        from unittest.mock import patch
        import fallback
        collector = self.collector({"show_work": True})
        collector.openrig = openrig.Bridge("sock", Mock(return_value=tile("dev-build@boxbox")),
                                           fetch=daemon(), url=openrig.DEFAULT_URL)
        collector.discover = lambda wanted: None
        agents = [{"pane_id": "w1:p5", "agent": "claude", "tab_id": "w1:t1"},
                  {"pane_id": "w2:p1", "agent": "hermes", "tab_id": "w2:t1"},
                  {"pane_id": "w3:p1", "agent": "codex", "tab_id": "w3:t1",
                   "agent_session": {"kind": "id", "value": "t-none"}}]
        tabs = [{"tab_id": "w1:t1", "label": "openrig:rig:boxbox#1"}, {"tab_id": "w3:t1", "label": "Fix login"}]
        calls = []
        def rpc(endpoint, method, params=None):
            calls.append((method, params))
            return {"agents": agents} if method == "agent.list" else {"tabs": tabs} if method == "tab.list" else {}
        with patch.object(info, "rpc", rpc), patch.object(fallback, "rpc", rpc):
            collector.sync()
        reports = {p["pane_id"]: p["tokens"] for m, p in calls
                   if m == "pane.report_metadata" and p.get("source") == info.SOURCE}
        self.assertIsNone(reports["w1:p5"]["agent_work"])
        self.assertEqual(reports["w3:p1"]["agent_work"], "Fix login")

    def test_without_detail_collection_the_daemon_is_never_asked(self):
        collector = self.collector({"collect_details": False})
        collector.openrig = Mock()
        self.sync(collector, [{"pane_id": "w1:p5", "agent": "claude"}])
        collector.openrig.seat.assert_not_called()

    def test_a_failing_lookup_is_recorded_and_details_continue(self):
        collector = self.collector()
        collector.openrig = Mock(**{"seat.side_effect": info.RpcError("pane_not_found")})
        status, reports, calls = self.sync(collector, [{"pane_id": "w1:p5", "agent": "claude"}])
        self.assertEqual(status["openrig_error"], "RpcError")
        self.assertEqual(status["missing_session"], {"claude": 1})


if __name__ == "__main__":
    unittest.main()
