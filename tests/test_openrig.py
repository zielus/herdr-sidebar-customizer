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


def daemon(rigs=RIGS, nodes=NODES):
    def fetch(url):
        if url.endswith("/api/ps"):
            return rigs
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
