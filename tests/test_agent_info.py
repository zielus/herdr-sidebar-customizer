import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import agent_info as info
import fallback
import portable
import setup_claude
import sidebar_settings as prefs

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_SID = "11111111-2222-3333-4444-555555555555"
posix_hook = unittest.skipIf(portable.WINDOWS, "the Claude status-line hook is POSIX-only and refused on Windows")


class IsolatedTestCase(unittest.TestCase):
    """Points the home directory and every config or state override at a temporary directory."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        overrides = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith("HERDR_") and k not in overrides}
        # Path.home() reads USERPROFILE on Windows and HOME elsewhere.
        home = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        patcher = patch.dict(os.environ, environment | home, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collector(self, settings=None, hook=False):
        collector = info.Collector("/test.sock", self.home / "state")
        values = prefs.DEFAULTS | {"collect_details": True} | (settings or {})
        collector.settings.load = lambda: values
        collector.hook.installed = lambda: hook
        collector.integrations.installed = lambda: {}
        collector.sync_spaces = lambda agents, settings: {}
        return collector

    def sync(self, collector, agents):
        """One sync against a fake Herdr; returns (status, {pane: tokens}, rpc calls)."""
        calls = []
        def rpc(endpoint, method, params=None):
            calls.append((method, params))
            return {"agents": agents} if method == "agent.list" else {}
        with patch.object(info, "rpc", rpc), patch.object(fallback, "rpc", rpc):
            status = collector.sync()
        reports = {p["pane_id"]: p["tokens"] for m, p in calls
                   if m == "pane.report_metadata" and p.get("source") == info.SOURCE}
        return status, reports, calls


def session(kind, sid, pane="w1:p1"):
    return {"pane_id": pane, "agent": kind, "agent_session": {"kind": "id", "value": sid}}


class RpcTests(unittest.TestCase):
    def call(self, response):
        with patch.object(portable, "request", return_value=json.dumps(response).encode()):
            return info.rpc("endpoint", "agent.list")

    def test_returns_the_result(self):
        self.assertEqual(self.call({"id": "agent-info", "result": {"agents": []}}), {"agents": []})

    def test_error_carries_its_code(self):
        with self.assertRaises(info.RpcError) as caught:
            self.call({"error": {"code": "pane_not_found"}})
        self.assertEqual(caught.exception.code, "pane_not_found")

    def test_malformed_responses_raise_rpc_error(self):
        for response in [[], "text", None, {"id": "agent-info"}, {"error": "denied"}]:
            with self.subTest(response=response), self.assertRaises(info.RpcError) as caught:
                self.call(response)
            self.assertIsNone(caught.exception.code)


class MetadataTests(unittest.TestCase):
    def test_names_keep_versions_and_drop_dates(self):
        for raw, expected in [("claude-fable-5-1", "Fable 5.1"),
                              ("claude-sonnet-4-20250929", "Sonnet 4"),
                              ("claude-opus-5[1m]", "Opus 5"),
                              ("gpt-6-astra", "GPT-6 Astra"),
                              ("gpt-5.6-sol", "GPT-5.6 Sol")]:
            self.assertEqual(info.model_name(raw), expected)
        self.assertEqual(info.model_name("claude-fable-5-1", "Fable"), "Fable 5.1")
        self.assertEqual(info.model_name("custom", "Custom 12"), "Custom 12")
        self.assertEqual(info.model_name("claude-opus-5", "Opus 5 (1M context)"), "Opus 5")

    def test_claude_reports_live_effort_and_input_context(self):
        observation = info.claude_observation({
            "model": {"id": "claude-fable-5-1", "display_name": "Fable"},
            "effort": {"level": "high"},
            "context_window": {"context_window_size": 200000, "used_percentage": 25,
                               "current_usage": {"input_tokens": 1000,
                                                 "cache_read_input_tokens": 48000,
                                                 "cache_creation_input_tokens": 1000,
                                                 "output_tokens": 9000}}})
        values = info.tokens(observation)
        self.assertEqual(values["model_effort"], "Fable 5.1 · high")
        self.assertEqual(values["context_used"], "50k")
        self.assertEqual(values["context_left"], "150k")
        self.assertEqual(values["context_left_pct"], "75%")
        self.assertEqual(len(values), 16)

    def test_combined_agent_label_uses_used_percent_and_reported_capacity(self):
        observation = {"model": "Opus 5 (1M context)", "model_id": "claude-opus-5",
                       "effort": "medium", "context": info.context(250000, 1000000)}
        self.assertEqual(info.tokens(observation, "claude")["agent_info"],
                         "Claude Opus 5 · medium · 25%/1M")
        values = info.tokens(observation, "claude")
        self.assertEqual(values["claude_icon"], "\uec82")
        self.assertIsNone(values["codex_icon"])
        self.assertEqual(values["model_context"], "Opus 5 · medium · 25%/1M")
        values = info.tokens({}, "codex")
        self.assertEqual(values["codex_icon"], "\uec81")
        self.assertIsNone(values["claude_icon"])
        self.assertIsNone(values["model_context"])
        observation["context"] = info.context(90440, 258400)
        self.assertEqual(info.tokens(observation, "codex")["context_compact"], "35%/258k")
        self.assertEqual(info.tokens(observation, "codex")["context_window"], "258.4k")
        observation["context"] = info.context(percent=25)
        self.assertEqual(info.tokens(observation, "claude")["context_compact"], "25%")
        observation.pop("context")
        observation.pop("effort")
        self.assertEqual(info.tokens(observation, "claude")["agent_info"], "Claude Opus 5")
        self.assertEqual(info.tokens({}, "codex")["agent_info"], "Codex")

    def test_unknown_values_clear_tokens_instead_of_guessing(self):
        values = info.tokens(info.claude_observation({"model": {"id": "custom"}}))
        self.assertEqual(values["model_effort"], "custom")
        self.assertIsNone(values["effort"])
        self.assertIsNone(values["context_left"])
        self.assertIsNone(values["context"])
        self.assertEqual(info.tokens({}), dict.fromkeys(info.TOKEN_NAMES))

    def test_bad_numbers_and_zero_capacity_do_not_become_fake_context(self):
        for bad in [True, -1, float("nan"), float("inf"), "100"]:
            self.assertIsNone(info.number(bad))
        self.assertEqual(info.context(0, 0), {"used": 0})
        self.assertEqual(info.tokens({"context": info.context(0, 100)})["context_left_pct"], "100%")

    def test_codex_switches_effort_and_uses_latest_not_total_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "rollout.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": SID}},
                {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "low"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 19000, "total_tokens": 20000},
                    "total_token_usage": {"total_tokens": 99999999}, "model_context_window": 100000}}}]
            p.write_text("".join(json.dumps(r) + "\n" for r in records))
            reader = info.SessionReader(p, "codex", SID)
            self.assertEqual(info.tokens(reader.update())["context_used_pct"], "20%")
            with p.open("a") as f:
                f.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "high"}}))
            self.assertEqual(reader.update()["effort"], "low")  # incomplete line
            with p.open("a") as f:
                f.write("\n")
            values = info.tokens(reader.update())
            self.assertEqual(values["model_effort"], "GPT-6 Astra · high")
            self.assertIsNone(values["context_used_pct"])
            with p.open("a") as f:
                f.write(json.dumps(records[-1]) + "\n")
                f.write('{"type":"compacted"}\n')
            self.assertIsNone(info.tokens(reader.update())["context"])
            self.assertEqual(info.SessionReader(p, "codex", "different").update(), {})
            p.write_text(json.dumps(records[0]) + "\n")
            self.assertEqual(reader.update(), {})  # truncation cannot retain old model

    def test_codex_header_of_the_wrong_shape_is_not_this_session(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "rollout.jsonl"
            for header in ([], {"type": "session_meta", "payload": "s"}, "text", {"payload": {"id": SID}}):
                p.write_text(json.dumps(header) + "\n")
                self.assertEqual(info.SessionReader(p, "codex", SID).update(), {})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_fifo_named_like_a_transcript_is_never_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / (SID + ".jsonl")
            os.mkfifo(p)
            self.assertEqual(info.SessionReader(p, "claude", SID).update(), {})
            self.assertIsNone(info.regular_mtime(p))
            self.assertIsNone(info.regular_mtime(Path(directory) / "missing.jsonl"))

    def test_claude_ignores_other_sessions_subagents_and_synthetic_models(self):
        r = info.SessionReader(Path("unused"), "claude", SID)
        row = {"type": "assistant", "sessionId": SID, "effort": "high",
               "message": {"model": "claude-fable-5-1"}}
        r.apply(row)
        for extra in [{"sessionId": "wrong"}, {"isSidechain": True},
                      {"message": {"model": "<synthetic>"}}]:
            r.apply(row | {"effort": "low"} | extra)
        self.assertEqual(r.observation["effort"], "high")
        self.assertNotIn("context", r.observation)
        r.apply(row | {"effort": None})
        self.assertEqual(r.observation["effort"], "")
        r.apply(row | {"timestamp": 5})
        self.assertNotIn("observed_at", r.observation)

    def test_newer_transcript_keeps_captured_context_for_the_same_model(self):
        captured = {"captured_at": 100, "observation": {
            "model": "Opus 5", "model_id": "claude-opus-5[1m]", "effort": "high",
            "context": info.context(250000, 1000000)}}
        newer = {"model": "Opus 5", "model_id": "claude-opus-5", "effort": "", "observed_at": 200}
        merged = info.merge_statusline(newer, captured)
        self.assertEqual(merged["context"], captured["observation"]["context"])
        self.assertEqual(merged["effort"], "high")
        self.assertEqual(info.merge_statusline(newer | {"effort": "low"}, captured)["effort"], "low")

    def test_newer_transcript_with_another_model_drops_stale_context(self):
        captured = {"captured_at": 100, "observation": {
            "model": "Opus 5", "model_id": "claude-opus-5", "context": info.context(5, 10)}}
        switched = {"model": "Sonnet 4", "model_id": "claude-sonnet-4", "observed_at": 200}
        merged = info.merge_statusline(switched, captured)
        self.assertEqual(merged["model"], "Sonnet 4")
        self.assertNotIn("context", merged)
        older = switched | {"observed_at": 50}
        self.assertEqual(info.merge_statusline(older, captured), captured["observation"])
        self.assertEqual(info.merge_statusline(switched, None), switched)

    def test_large_and_malformed_lines_do_not_block_new_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "session.jsonl"
            row = {"type": "assistant", "sessionId": SID, "message": {"model": "claude-opus-5"}}
            p.write_text("x" * (info.MAX_LINE + 10) + "\nBAD JSON\n" + json.dumps(row) + "\n")
            self.assertEqual(info.SessionReader(p, "claude", SID).update()["model"], "Opus 5")

    @posix_hook
    def test_statusline_persists_only_selected_fields_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info.atomic_json(root / "previous-statusline.json", {"command": "cat"})
            raw = json.dumps({"session_id": SID, "model": {"id": "claude-opus-5"},
                              "secret_prompt": "PRIVATE SENTINEL"}).encode()
            stdin, stdout = type("In", (), {"buffer": io.BytesIO(raw)})(), type("Out", (), {"buffer": io.BytesIO()})()
            with patch.dict(os.environ, HERDR_ENV="1", HERDR_SOCKET_PATH="/test.sock", HERDR_PANE_ID="w1:p1"), \
                    patch.object(info.sys, "stdin", stdin), patch.object(info.sys, "stdout", stdout), \
                    patch('sidebar_settings.read_settings', return_value={'collect_details': True}):
                info.claude_statusline(root)
            self.assertEqual(stdout.buffer.getvalue(), raw)
            saved = root / info.digest("/test.sock") / (info.digest(SID) + ".json")
            self.assertNotIn("PRIVATE SENTINEL", saved.read_text())
            self.assertNotIn("secret_prompt", saved.read_text())
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    @posix_hook
    def test_install_is_idempotent_preserves_other_settings_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            original = {"theme": "custom:example", "hooks": {"example": []},
                        "statusLine": {"type": "command", "command": "printf original", "padding": 2}}
            settings.write_text(json.dumps(original))
            setup_claude.install(settings, root / "state")
            after = settings.read_bytes()
            setup_claude.install(settings, root / "state")
            self.assertEqual(after, settings.read_bytes())
            changed = json.loads(after)
            self.assertEqual(changed["theme"], original["theme"])
            self.assertEqual(changed["hooks"], original["hooks"])
            self.assertEqual(changed["statusLine"]["padding"], 2)
            setup_claude.install(settings, root / "state", remove=True)
            self.assertEqual(json.loads(settings.read_text()), original)

    @posix_hook
    def test_uninstall_does_not_overwrite_later_user_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text('{}')
            setup_claude.install(settings, root / "state")
            newer = {"statusLine": {"type": "command", "command": "printf newer"}}
            settings.write_text(json.dumps(newer))
            setup_claude.install(settings, root / "state", remove=True)
            self.assertEqual(json.loads(settings.read_text()), newer)


class CollectorTests(IsolatedTestCase):
    def test_session_replacement_clears_previous_metadata(self):
        collector = self.collector()
        collector.previous = {"w1:p1"}
        status, reports, calls = self.sync(collector, [{"pane_id": "w1:p1", "agent": "hermes"}])
        self.assertEqual(reports["w1:p1"], dict.fromkeys(info.TOKEN_NAMES))
        report = next(p for m, p in calls if m == "pane.report_metadata" and p["source"] == info.SOURCE)
        self.assertEqual(report["ttl_ms"], info.TTL_MS)

    def test_one_malformed_session_blanks_only_its_own_pane(self):
        collector = self.collector()
        def observe(sid):
            if sid == "broken":
                raise AttributeError("'int' object has no attribute 'get'")
            return {"model": "local", "model_id": "local"}
        collector.opencode.observe = observe
        agents = [session("opencode", "broken", "w1:p1"), session("opencode", "fine", "w1:p2")]
        status, reports, calls = self.sync(collector, agents)
        self.assertEqual(reports["w1:p1"]["agent_info"], "OpenCode")
        self.assertEqual(reports["w1:p2"]["agent_info"], "OpenCode local")
        self.assertEqual(status["observe_errors"], {"opencode": "AttributeError"})
        self.assertEqual(status["models"], 1)

    def test_rejected_session_is_not_reported_as_a_missing_integration(self):
        collector = self.collector()
        collector.integrations.installed = Mock(return_value={})
        status, reports, calls = self.sync(collector, [session("claude", "not a valid id")])
        self.assertEqual(status["unreadable_session"], {"claude": 1})
        self.assertEqual(status["missing_session"], {})
        self.assertNotIn("w1:p1", reports)
        collector.integrations.installed.assert_not_called()

    def test_windows_rows_get_no_hook_hint_they_cannot_act_on(self):
        with patch.object(portable, "WINDOWS", True):
            status, reports, calls = self.sync(self.collector(), [session("claude", SID)])
        self.assertEqual(reports["w1:p1"]["agent_info"], "Claude")
        self.assertEqual(status["missing_context"], 1)
        self.assertFalse(status["claude_hook_supported"])

    def test_captured_context_survives_a_newer_transcript_entry(self):
        collector = self.collector()
        collector.paths = {SID: self.home / "transcript.jsonl"}
        collector.discover = lambda wanted: None
        entry = {"type": "assistant", "sessionId": SID, "timestamp": "2026-01-01T00:00:10Z",
                 "message": {"model": "claude-opus-5"}}
        collector.paths[SID].write_text(json.dumps(entry) + "\n")
        info.atomic_json(collector.directory / (info.digest(SID) + ".json"), {
            "pane_id": "w1:p1", "captured_at": 0,
            "observation": {"model": "Opus 5", "model_id": "claude-opus-5", "context": info.context(1, 4)}})
        status, reports, calls = self.sync(collector, [session("claude", SID)])
        self.assertEqual(reports["w1:p1"]["context_used_pct"], "25%")

    def test_discovery_finds_a_new_session_without_waiting_for_the_slow_walk(self):
        codex = self.home / ".codex/sessions/2026/10/02"
        claude = self.home / ".claude/projects/example"
        codex.mkdir(parents=True)
        claude.mkdir(parents=True)
        (claude / (SID + ".jsonl")).write_text("")
        collector = self.collector()
        clock = Mock(return_value=100)
        with patch.object(info.time, "monotonic", clock):
            collector.discover({SID: "claude"})
            self.assertEqual(collector.paths, {SID: claude / (SID + ".jsonl")})
            rollout = codex / ("rollout-2026-10-02T00-00-00-" + OTHER_SID + ".jsonl")
            rollout.write_text("")
            clock.return_value = 105
            collector.discover({SID: "claude", OTHER_SID: "codex"})
            self.assertEqual(collector.paths[OTHER_SID], rollout)
            newer = claude / ("again-" + SID + ".jsonl")
            newer.write_text("")
            os.utime(newer, (time.time() + 60, time.time() + 60))
            clock.return_value = 110
            collector.discover({SID: "claude", OTHER_SID: "codex"})
            self.assertEqual(collector.paths[SID], claude / (SID + ".jsonl"))
            clock.return_value = 140
            collector.discover({SID: "claude", OTHER_SID: "codex"})
            self.assertEqual(collector.paths[SID], newer)

    def test_missing_transcript_falls_back_to_the_slow_walk(self):
        collector = self.collector()
        clock = Mock(return_value=100)
        with patch.object(info.time, "monotonic", clock), \
                patch.object(info.os, "walk", return_value=[]) as walk:
            collector.discover({SID: "claude"})
            clock.return_value = 103
            collector.discover({SID: "claude"})
            self.assertEqual(walk.call_count, 2 * len(collector.transcript_roots))
            clock.return_value = 100 + info.NEW_SESSION_SECONDS + 1
            collector.discover({SID: "claude"})
            calls = walk.call_count
            clock.return_value += info.POLL_SECONDS
            collector.discover({SID: "claude"})
            self.assertEqual(walk.call_count, calls)
            clock.return_value += info.DISCOVERY_SECONDS
            collector.discover({SID: "claude"})
            self.assertGreater(walk.call_count, calls)

    def test_old_captures_of_ended_sessions_are_pruned(self):
        collector = self.collector()
        directory = collector.directory
        directory.mkdir(parents=True)
        old = time.time() - info.CAPTURE_MAX_AGE - 60
        names = {"ended": info.digest("ended") + ".json", "live": info.digest(SID) + ".json",
                 "fresh": info.digest("starting") + ".json", "status": "status.json"}
        for key, name in names.items():
            (directory / name).write_text("{}")
            if key != "fresh":
                os.utime(directory / name, (old, old))
        collector.prune_captures({SID})
        remaining = {p.name for p in directory.iterdir()}
        self.assertEqual(remaining, set(names.values()) - {names["ended"]})


class WorkerTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.home / "state"
        self.directory.mkdir()

    def test_watch_returns_while_another_worker_holds_the_lock(self):
        root = self.home / "root"
        directory = info.endpoint_state(root, "/test.sock")
        directory.mkdir(parents=True)
        with (directory / "watch.lock").open("a") as held:
            self.assertTrue(portable.try_lock(held))
            with patch.object(info, "enabled") as enabled, patch.object(info, "Collector") as collector:
                info.watch("/test.sock", root)
            enabled.assert_not_called()
            collector.assert_not_called()

    def test_watch_returns_when_the_plugin_is_disabled(self):
        with patch.object(info, "enabled", return_value=False), patch.object(info, "Collector") as collector:
            info.watch("/test.sock", self.home / "root")
        collector.assert_not_called()

    def test_collect_survives_failures_but_stops_after_too_many(self):
        collector = Mock()
        collector.sync.side_effect = [AttributeError(), None] + [NameError()] * info.MAX_FAILURES
        with patch.object(info, "enabled", return_value=True), patch.object(info.time, "sleep"):
            info.collect("/test.sock", self.directory, collector)
        self.assertEqual(collector.sync.call_count, info.MAX_FAILURES + 2)
        self.assertEqual(info.read_json(self.directory / "status.json")["error"], "NameError")

    def test_collect_stops_when_the_plugin_is_disabled(self):
        collector = Mock()
        with patch.object(info, "enabled", side_effect=[True, False]), patch.object(info.time, "sleep"):
            info.collect("/test.sock", self.directory, collector)
        collector.sync.assert_called_once()

    def test_animation_restarts_after_errors_and_records_them(self):
        stop = Mock(is_set=Mock(return_value=False))
        with patch("sidebar_state.run", side_effect=AttributeError) as run:
            info.animate("/test.sock", self.directory, stop)
        self.assertEqual(run.call_count, info.MAX_FAILURES)
        status = info.read_json(self.directory / "animation-status.json")
        self.assertEqual(status["error"], "AttributeError")

    def test_start_spawns_only_when_no_worker_runs(self):
        root = self.home / "root"
        with patch("installation.active", return_value=True), patch.object(portable, "spawn") as spawn:
            info.start("/test.sock", root)
            spawn.assert_called_once()
            spawn.reset_mock()
            directory = info.endpoint_state(root, "/test.sock")
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "watch.lock").open("a") as held:
                portable.try_lock(held)
                info.start("/test.sock", root)
            spawn.assert_not_called()

    def test_start_does_nothing_before_setup(self):
        with patch("installation.active", return_value=False), patch.object(portable, "spawn") as spawn:
            info.start("/test.sock", self.home / "root")
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
