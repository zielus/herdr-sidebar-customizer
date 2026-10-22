#!/usr/bin/env python3
"""Report local session metadata to Herdr, never conversation text.

Claude's statusLine is authoritative, including changes made between turns,
and is the only source of its context. A newer transcript entry updates the
model and effort, and the transcript bootstraps existing panes until their
first statusLine update.
Codex has no equivalent statusLine callback: follow the exact native session's
turn_context and latest token_count, never the user's configured default or
cumulative token usage. Its labels describe the last recorded turn.

Only normalized scalar observations are persisted. JSONL input is discarded
after parsing; no prompt, answer, path from a message, or raw hook payload is
written to state or logs. File reads are bounded and incremental. Python's
standard library and the local Herdr socket are the only dependencies.
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time

import portable
import runtime
from runtime import PLUGIN, state_root

# The plugin's other modules import names from this one, so they are imported
# where they are used to keep `python3 agent_info.py` free of import cycles.

SOURCE = PLUGIN
POLL_SECONDS = 3
TTL_MS = 15000
MAX_READ = 32 * 1024 * 1024
MAX_LINE = 1024 * 1024
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
# Agents whose model and context the collector can read locally.
SUPPORTED = {"claude", "codex", "opencode", "pi", "omp"}
TOKEN_NAMES = ("model", "model_id", "effort", "model_effort", "context",
               "context_used", "context_left", "context_window",
               "context_used_pct", "context_left_pct", "context_compact", "agent_info",
               "claude_icon", "codex_icon", "model_context", "agent_work")
# Transcript files of known sessions move rarely; walk for them slowly.
DISCOVERY_SECONDS = 30
# A new session's transcript usually appears within seconds, so walk every poll
# for this long; after that a still-missing transcript (for example one under
# another CLAUDE_CONFIG_DIR) waits for the slow walk instead of costing a full
# walk every poll forever.
NEW_SESSION_SECONDS = 60
SKIPPED_DIRS = {"subagents", "tool-results", ".git"}
# Status-line captures of ended sessions are removed once they are this old.
CAPTURE_MAX_AGE = 24 * 60 * 60
PRUNE_SECONDS = 60
CAPTURE_NAME = re.compile(r"[0-9a-f]{24}\.json")
MAX_FAILURES = 10
# Errors that local data or a briefly unavailable Herdr can cause.
RECOVERABLE = (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError)


def text(value):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value if c.isprintable()).split())[:100]


def number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and value >= 0) else None


def model_name(model, display=""):
    model, display = text(model), text(display)
    display = re.sub(r"\s+\([\d.]+[kKmM] context\)$", "", display)
    if display and re.search(r"\d", display):
        return display
    core = re.sub(r"\[.*\]$", "", model)
    core = re.sub(r"-\d{8}$", "", core)
    match = re.fullmatch(r"claude-(opus|sonnet|haiku|fable)-(\d+(?:-\d+)?)", core)
    if match:
        return f"{match[1].title()} {match[2].replace('-', '.')}"
    match = re.fullmatch(r"gpt-([\d.]+)-(astra|sol|luna|terra)", model)
    if match:
        return f"GPT-{match[1]} {match[2].title()}"
    return display or model


def context(used=None, window=None, percent=None):
    used, window, percent = number(used), number(window), number(percent)
    if window == 0:
        window = None
    if percent is not None and percent > 100:
        percent = None
    if percent is None and used is not None and window:
        percent = min(100, 100 * used / window)
    if used is None and percent is not None and window:
        used = round(window * percent / 100)
    return {k: v for k, v in {"used": used, "window": window, "percent": percent}.items()
            if v is not None}


def claude_observation(payload):
    model = payload.get("model") or {}
    effort = (payload.get("effort") or {}).get("level")
    ctx = payload.get("context_window") or {}
    usage = ctx.get("current_usage") or {}
    fields = [number(usage.get(k)) for k in
              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    used = sum(v or 0 for v in fields) if any(v is not None for v in fields) else None
    return {"model": model_name(model.get("id"), model.get("display_name")),
            "model_id": text(model.get("id")),
            "effort": effort if effort in EFFORTS else "",
            "context": context(used, ctx.get("context_window_size"), ctx.get("used_percentage"))}


def compact(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if value >= 1000:
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(round(value))


def tokens(observation, agent="", settings=None):
    result = dict.fromkeys(TOKEN_NAMES)
    for key in ("model", "model_id", "effort"):
        result[key] = text(observation.get(key)) or None
    if result["model"]:
        result["model"] = model_name(result["model_id"], result["model"])
        result["model_effort"] = result["model"] + (f" · {result['effort']}" if result["effort"] else "")
    ctx = observation.get("context") or {}
    used, window, pct = (number(ctx.get(k)) for k in ("used", "window", "percent"))
    if used is not None:
        result["context_used"] = compact(used)
    if window:
        result["context_window"] = compact(window)
        if used is not None:
            result["context_left"] = compact(max(0, window - used))
    if pct is not None:
        pct = min(100, round(pct))
        result["context_used_pct"] = f"{pct}%"
        result["context_left_pct"] = f"{100 - pct}%"
        result["context"] = f"ctx {pct}% used / {100 - pct}% left"
        capacity = (f"{window / 1000:.0f}k" if window and 1000 <= window < 1_000_000
                    else compact(window).replace("m", "M") if window else "")
        result["context_compact"] = f"{pct}%" + (f"/{capacity}" if capacity else "")
    if agent in {"claude", "codex"}:
        result[agent + "_icon"] = {"claude": "\uec82", "codex": "\uec81"}[agent]
    result["model_context"] = " · ".join(part for part in
        (result["model_effort"], result["context_compact"]) if part) or None
    result["agent_work"] = text(observation.get("work")) or None
    from sidebar_settings import DEFAULTS, agent_label
    result["agent_info"] = agent_label(result, agent, settings or DEFAULTS)
    return result


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".info-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.write("\n")
        portable.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def endpoint_state(root, endpoint):
    return root / digest(endpoint)


class RpcError(RuntimeError):
    def __init__(self, code):
        super().__init__("Herdr rejected metadata request")
        self.code = code


def rpc(endpoint, method, params=None):
    request = {"id": "agent-info", "method": method, "params": params or {}}
    raw = portable.request(endpoint, json.dumps(request).encode() + b"\n")
    response = json.loads(raw)
    if not isinstance(response, dict):
        raise RpcError(None)
    if "error" in response:
        error = response["error"]
        raise RpcError(error.get("code") if isinstance(error, dict) else None)
    if "result" not in response:
        raise RpcError(None)
    return response["result"]


def regular_mtime(path):
    """A regular file's mtime, or None for anything else: opening a FIFO would block."""
    try:
        result = path.stat()
    except OSError:
        return None
    return result.st_mtime if stat.S_ISREG(result.st_mode) else None


class SessionReader:
    def __init__(self, path, agent, session):
        self.path, self.agent, self.session = path, agent, session
        self.position, self.inode, self.observation = 0, None, {}
        # Claude's titles outlive each turn's observation: a /rename one, then its own.
        self.titles = {}

    def update(self):
        current = self.path.stat()
        if not stat.S_ISREG(current.st_mode):
            return {}
        if current.st_ino != self.inode or current.st_size < self.position:
            self.position, self.observation, self.titles = 0, {}, {}
            self.inode = current.st_ino
        with self.path.open("rb") as handle:
            if not self.owns_file(handle):
                return {}
            start = max(self.position, current.st_size - MAX_READ)
            handle.seek(start)
            if start > self.position:
                self.observation = {}
                handle.readline(MAX_LINE)  # discard a partial first line
            while handle.tell() < current.st_size:
                pos = handle.tell()
                line = handle.readline(MAX_LINE + 1)
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(MAX_LINE + 1)
                    self.position = handle.tell()
                    continue
                if not line.endswith(b"\n"):
                    self.position = pos  # retry an unfinished write on the next poll
                    break
                self.position = handle.tell()
                try:
                    self.apply(json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    continue
        title = self.titles.get("custom") or self.titles.get("ai")
        return dict(self.observation, title=title) if title else dict(self.observation)

    def owns_file(self, handle):
        """Whether the file belongs to this session; Codex proves it with a session_meta header."""
        if self.agent != "codex":
            return True
        try:
            first = json.loads(handle.readline(MAX_LINE))
        except ValueError:
            return False
        payload = first.get("payload") if isinstance(first, dict) else None
        return (isinstance(payload, dict) and first.get("type") == "session_meta"
                and payload.get("id") == self.session)

    def apply(self, entry):
        if self.agent == "claude":
            self.apply_claude(entry)
        else:
            self.apply_codex(entry)

    def apply_claude(self, entry):
        if entry.get("sessionId", entry.get("session_id")) != self.session or entry.get("isSidechain"):
            return
        for kind, key in (("custom", "customTitle"), ("ai", "aiTitle")):
            if entry.get("type") == kind + "-title" and text(entry.get(key)):
                self.titles[kind] = text(entry[key])
        if entry.get("type") != "assistant":
            return
        msg = entry.get("message") or {}
        model = text(msg.get("model"))
        if not model or model.startswith("<"):
            return
        effort = entry.get("effort")
        # No context capacity is assumed from model family or subscription.
        self.observation = {"model": model_name(model), "model_id": model,
                            "effort": effort if effort in EFFORTS else ""}
        try:
            stamp = entry.get("timestamp", "").replace("Z", "+00:00")
            self.observation["observed_at"] = datetime.fromisoformat(stamp).timestamp()
        except (ValueError, TypeError, AttributeError):
            pass

    def apply_codex(self, entry):
        payload = entry.get("payload") or {}
        if entry.get("type") == "turn_context":
            model = text(payload.get("model"))
            if model != self.observation.get("model_id"):
                self.observation.pop("context", None)
            effort = payload.get("effort", payload.get("reasoning_effort"))
            self.observation.update(model=model_name(model), model_id=model,
                                    effort=effort if effort in EFFORTS else "")
        elif entry.get("type") == "compacted":
            self.observation.pop("context", None)
        elif entry.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            usage = info.get("last_token_usage") or {}
            # cached_input_tokens is already included in input_tokens.
            self.observation["context"] = context(usage.get("total_tokens"), info.get("model_context_window"))


def base_model(model_id):
    """A Claude model ID without a context suffix such as [1m], which transcripts omit."""
    return re.sub(r"\[.*\]$", "", model_id or "")


def merge_statusline(transcript, captured):
    """Claude details from the status-line capture, updated by a newer transcript entry.

    Transcripts never carry context, so a newer entry for the same model must
    not blank the captured context; a different model makes it stale.
    """
    if not captured:
        return transcript
    merged = dict(captured["observation"])
    observed_at = number(transcript.get("observed_at")) or 0
    captured_at = number(captured.get("captured_at")) or 0
    if observed_at <= captured_at or not transcript.get("model_id"):
        return merged
    if base_model(transcript["model_id"]) != base_model(merged.get("model_id")):
        merged.pop("context", None)
        merged.update(model=transcript["model"], model_id=transcript["model_id"])
    if transcript.get("effort"):
        merged["effort"] = transcript["effort"]
    return merged


def thread_names(raw):
    """{thread ID: name} from Codex's session index; a thread's latest line wins."""
    names = {}
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            names[entry["id"]] = text(entry.get("thread_name"))
    return names


def has_session(agent):
    ref = agent.get("agent_session")
    return isinstance(ref, dict) and isinstance(ref.get("value"), str)


def session_ref(agent):
    """A session ID, or for pi/omp a session file inside their own directories."""
    if not has_session(agent):
        return None
    import pi_sessions
    ref = agent["agent_session"]
    if agent.get("agent") in pi_sessions.KINDS:
        if ref.get("kind") != "path":
            return None
        return pi_sessions.allowed_path(agent["agent"], ref["value"])
    if ref.get("kind") == "id" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref["value"]):
        return ref["value"]
    return None


def transcript_candidates(name):
    """Session IDs a transcript file could belong to: its whole stem, or any part after a '-'.

    Claude names a transcript `<id>.jsonl` and Codex `rollout-<date>-<id>.jsonl`,
    where the ID itself may contain '-'.
    """
    stem = name.removesuffix(".jsonl")
    yield stem
    for index, char in enumerate(stem):
        if char == "-":
            yield stem[index + 1:]


def run_guarded(steps):
    """Run each named step; returns {name: exception type} for the steps that failed."""
    errors = {}
    for name, step in steps.items():
        try:
            step()
        except RECOVERABLE as error:
            errors[name] = type(error).__name__
    return errors


@dataclass
class Tally:
    """What one sync published, for status.json and the fallback markers."""
    reported: dict = field(default_factory=dict)  # pane -> tokens published this sync
    models: int = 0
    contexts: int = 0
    missing_context: int = 0
    missing_session: dict = field(default_factory=dict)
    unreadable_session: dict = field(default_factory=dict)
    observe_errors: dict = field(default_factory=dict)


def count(counts, kind):
    counts[kind] = counts.get(kind, 0) + 1


class Collector:
    def __init__(self, endpoint, root):
        import opencode
        import pi_sessions
        from detail_hints import HookCheck, IntegrationCheck
        from setup_claude import settings_path
        from sidebar_settings import SettingsReader
        from space_rows import SpaceRows
        self.endpoint, self.directory = endpoint, endpoint_state(root, endpoint)
        self.settings = SettingsReader()
        self.readers, self.paths, self.previous = {}, {}, set()
        self.first_seen = {}
        self.last_discovery = self.last_prune = -math.inf
        self.space_rows = SpaceRows(endpoint)
        from tab_names import TabNamer
        self.tab_namer = TabNamer(endpoint, self.directory)
        self.hook = HookCheck(settings_path(), root)
        self.integrations = IntegrationCheck(os.environ.get("HERDR_BIN_PATH") or "herdr")
        self.opencode = opencode.Reader()
        self.pi = pi_sessions.Reader()
        import openrig
        self.openrig = openrig.Bridge(endpoint, rpc)
        self.openrig_context, self.openrig_nodes = {}, {}
        self.tab_labels = {}
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        from filecache import StampCache
        self.codex_titles = StampCache(codex_home / "session_index.jsonl", thread_names, {})
        self.transcript_roots = [("codex", codex_home / "sessions"),
                                 ("claude", settings_path().parent / "projects")]

    def sync(self):
        agents = rpc(self.endpoint, "agent.list")["agents"]
        settings = self.settings.load()
        errors = {}
        if settings["collect_details"]:
            agents, failure = self.bridge(agents)
            if failure:
                errors["openrig_error"] = failure
        else:
            self.openrig_context, self.openrig_nodes = {}, {}
        errors |= self.sync_spaces(agents, settings)
        sessioned, unsessioned, rejected = self.partition(agents, settings)
        hook = self.hook.installed()
        # Only asked while some agent lacks a session, and then once a minute.
        integrations = self.integrations.installed() if unsessioned else {}
        tally = Tally()
        for agent in rejected:
            count(tally.unreadable_session, agent["agent"])
        self.tab_labels = self.read_tab_labels(settings)
        self.report_unsessioned(unsessioned, settings, integrations, tally)
        self.report_sessioned(sessioned, settings, hook, tally)
        self.clear_stale(agents, set(tally.reported))
        labelled = self.plugin_rows(agents, settings, tally)
        errors |= run_guarded({"fallback_error": lambda: self.sync_fallback(agents, labelled)})
        if settings["tab_names"]:
            errors |= run_guarded({"tab_names_error": lambda: self.tab_namer.sync(agents, settings["tab_name_format"])})
        self.forget(sessioned)
        self.prune_captures({ref for agent, ref in sessioned if agent["agent"] == "claude"})
        status = self.status(tally, len(sessioned), hook, integrations, errors)
        atomic_json(self.directory / "status.json", status)
        return status

    def sync_spaces(self, agents, settings):
        """Space colors and rows; a failure is recorded and never stops agent details."""
        import space_colors
        def sync_colors():
            return space_colors.sync(self.endpoint, self.directory, load=self.settings.load)
        return run_guarded({
            "space_colors_error": sync_colors,
            "space_rows_error": lambda: self.space_rows.sync(settings, self.team_dirs(agents))})

    def team_dirs(self, agents):
        """{space: sorted working directories of the OpenRig seats in it}."""
        result = {}
        for agent in agents:
            node = self.openrig_nodes.get(agent["pane_id"])
            if node and isinstance(node.get("cwd"), str) and node["cwd"]:
                result.setdefault(agent.get("workspace_id"), set()).add(node["cwd"])
        return {space: sorted(dirs) for space, dirs in result.items()}

    def bridge(self, agents):
        """Give agents that OpenRig runs the session and context its daemon knows.

        Returns the agents and the type of the first lookup that failed, if any;
        a failed lookup skips only its own pane.
        """
        import openrig
        self.openrig_context, self.openrig_nodes = {}, {}
        self.openrig.forget({agent["pane_id"] for agent in agents})
        result, failure = [], None
        for agent in agents:
            kind = agent.get("agent")
            if kind in openrig.KINDS.values() and not has_session(agent):
                try:
                    node = self.openrig.seat(agent["pane_id"])
                except RECOVERABLE as error:
                    failure, node = failure or type(error).__name__, None
                if node:
                    # A seat counts for its space's branch even before its session is known.
                    self.openrig_nodes[agent["pane_id"]] = node
                session = openrig.agent_session(node, kind) if node else None
                if session:
                    agent = dict(agent, agent_session=session)
                    self.openrig_context[agent["pane_id"]] = openrig.context_reading(node)
            result.append(agent)
        return result, failure

    def partition(self, agents, settings):
        """Supported agents as (agent, ref) pairs with a readable session, plus those
        that report no session and those whose session reference is rejected."""
        sessioned, unsessioned, rejected = [], [], []
        if not settings["collect_details"]:
            self.paths.clear()
            return sessioned, unsessioned, rejected
        for agent in agents:
            if agent.get("agent") not in SUPPORTED:
                continue
            ref = session_ref(agent)
            if ref:
                sessioned.append((agent, ref))
            elif has_session(agent):
                rejected.append(agent)
            else:
                unsessioned.append(agent)
        # Only Claude and Codex transcripts are found by searching; others are direct.
        wanted = {ref: agent["agent"] for agent, ref in sessioned if agent["agent"] in ("claude", "codex")}
        if wanted:
            self.discover(wanted)
        return sessioned, unsessioned, rejected

    def discover(self, wanted):
        """Find transcript files for {session ID: agent}, walking quickly only for new sessions."""
        now = time.monotonic()
        self.first_seen = {sid: self.first_seen.get(sid, now) for sid in wanted}
        searching = any(sid not in self.paths and now - self.first_seen[sid] < NEW_SESSION_SECONDS
                        for sid in wanted)
        if now - self.last_discovery < (POLL_SECONDS if searching else DISCOVERY_SECONDS):
            return
        self.last_discovery = now
        newest = {sid: (regular_mtime(path), path) for sid, path in self.paths.items() if sid in wanted}
        newest = {sid: found for sid, found in newest.items() if found[0] is not None}
        for kind, root in self.transcript_roots:
            for directory, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
                for name in files:
                    if name.endswith(".jsonl"):
                        self.consider(Path(directory) / name, kind, wanted, newest)
        self.paths = {sid: path for sid, (mtime, path) in newest.items()}

    @staticmethod
    def consider(path, kind, wanted, newest):
        """Keep the most recently written transcript for the session this file names."""
        sid = next((c for c in transcript_candidates(path.name) if wanted.get(c) == kind), None)
        if sid is None:
            return
        mtime = regular_mtime(path)
        if mtime is not None and (sid not in newest or mtime > newest[sid][0]):
            newest[sid] = (mtime, path)

    def report(self, pane, values):
        rpc(self.endpoint, "pane.report_metadata",
            {"pane_id": pane, "source": SOURCE, "tokens": values, "ttl_ms": TTL_MS})

    def report_unsessioned(self, unsessioned, settings, integrations, tally):
        """Supported agents without a session ID need Herdr's own integration."""
        import detail_hints
        from sidebar_settings import agent_label
        for agent in unsessioned:
            kind, pane = agent["agent"], agent["pane_id"]
            count(tally.missing_session, kind)
            if settings["detail_hints"]:
                values = dict.fromkeys(TOKEN_NAMES)
                values["agent_info"] = detail_hints.with_hint(agent_label({}, kind, settings),
                                                              detail_hints.session_hint(kind, integrations))
                values["agent_work"] = self.tab_fallback(agent)
                self.report(pane, values)
                tally.reported[pane] = values

    def report_sessioned(self, sessioned, settings, hook, tally):
        import detail_hints
        # The hook needs /bin/sh, so Windows rows get no hint they cannot act on.
        context_hint = settings["detail_hints"] and not hook and not portable.WINDOWS
        for agent, ref in sessioned:
            kind, pane = agent["agent"], agent["pane_id"]
            values = self.describe(kind, ref, pane, settings, tally)
            values["agent_work"] = values["agent_work"] or self.tab_fallback(agent)
            if kind == "claude" and not values["context"]:
                tally.missing_context += 1
                if context_hint:
                    values["agent_info"] = detail_hints.with_hint(values["agent_info"], detail_hints.CONTEXT_HINT)
            self.report(pane, values)
            tally.reported[pane] = values
            tally.models += bool(values["model"])
            tally.contexts += bool(values["context"])

    def read_tab_labels(self, settings):
        """{tab: label}, read only while agents' work stands in for their tab's name."""
        if not settings["show_work"]:
            return {}
        tabs = rpc(self.endpoint, "tab.list").get("tabs") or []
        return {tab["tab_id"]: tab["label"] for tab in tabs
                if isinstance(tab, dict) and isinstance(tab.get("label"), str)}

    def tab_fallback(self, agent):
        """A plain agent with no known work keeps its tab's name; an OpenRig seat's says nothing."""
        if agent["pane_id"] in self.openrig_nodes:
            return None
        return text(self.tab_labels.get(agent.get("tab_id"))) or None

    def describe(self, kind, ref, pane, settings, tally):
        """Tokens for one agent; a malformed session source blanks only its own pane."""
        try:
            observation = self.observe(kind, ref, pane)
            work = self.work(kind, ref, pane, observation)
            return tokens(dict(observation, work=work), kind, settings)
        except Exception as error:  # Recorded in status.json, so a code error stays visible.
            tally.observe_errors[kind] = type(error).__name__
            return tokens({}, kind, settings)

    def work(self, kind, ref, pane, observation):
        """What the agent works on: its OpenRig task, else its session's own title."""
        from runtime import HERDR_TEXT_LIMIT
        node = self.openrig_nodes.get(pane)
        task = self.openrig.task(node.get("canonicalSessionName")) if node else None
        if kind == "claude":
            title = observation.get("title")
        else:
            title = self.codex_titles.load().get(ref) if kind == "codex" else None
        # OpenRig titles each Claude seat after the seat itself, which says nothing new.
        if node and title == node.get("canonicalSessionName"):
            title = None
        found = task or title
        return found[:HERDR_TEXT_LIMIT] if found else None

    def observe(self, kind, ref, pane):
        import pi_sessions
        if kind == "opencode":
            return self.opencode.observe(ref)
        if kind in pi_sessions.KINDS:
            return self.pi.observe(kind, ref)
        observation = self.read_transcript(kind, ref)
        if kind == "claude":
            captured = self.captured(ref, pane)
            # The status line knows nothing of titles, so the transcript's survives the merge.
            title = observation.get("title")
            observation = merge_statusline(observation, captured)
            if title:
                observation = dict(observation, title=title)
            # An OpenRig seat runs its own status line, so its daemon reports the context.
            reading = None if captured else self.openrig_context.get(pane)
            if reading:
                observation = dict(observation, context=context(*reading))
        return observation

    def read_transcript(self, kind, sid):
        path = self.paths.get(sid)
        if not path:
            return {}
        reader = self.readers.get(sid)
        if reader is None or reader.path != path:
            reader = self.readers[sid] = SessionReader(path, kind, sid)
        try:
            return reader.update()
        except OSError:
            return {}

    def captured(self, sid, pane):
        """The status-line hook's capture for this session in this pane, or None."""
        captured = read_json(self.directory / (digest(sid) + ".json"))
        if (isinstance(captured, dict) and captured.get("pane_id") == pane
                and isinstance(captured.get("observation"), dict)):
            return captured
        return None

    def clear_stale(self, agents, current):
        """Blank panes described last time but not now, such as after a session ends."""
        live = {agent["pane_id"] for agent in agents}
        for pane in (self.previous - current) & live:
            self.report(pane, dict.fromkeys(TOKEN_NAMES))
        self.previous = current

    @staticmethod
    def plugin_rows(agents, settings, tally):
        """Panes whose details row belongs to the plugin and must not show native text.

        With details hidden or collection off the user chose an empty row, so every
        agent counts. Otherwise only agents this sync described do, and the rest
        (unsupported agents, or sessions it cannot read) keep Herdr's native text.
        """
        if not (settings["agent_text"] and settings["collect_details"]):
            return {agent["pane_id"] for agent in agents}
        return {pane for pane, values in tally.reported.items() if values["agent_info"]}

    def sync_fallback(self, agents, labelled):
        """Mark the plugin's rows and unmark the rest, so only those show Herdr's native text."""
        import fallback
        fallback.sync(self.endpoint, [agent for agent in agents if agent["pane_id"] in labelled])
        for agent in agents:
            if agent["pane_id"] not in labelled and fallback.marked(agent):
                fallback.clear(self.endpoint, agent["pane_id"])

    def forget(self, sessioned):
        import pi_sessions
        refs = {ref for agent, ref in sessioned}
        self.readers = {sid: reader for sid, reader in self.readers.items() if sid in refs}
        self.pi.forget({ref for agent, ref in sessioned if agent["agent"] in pi_sessions.KINDS})

    def prune_captures(self, live_sessions):
        """Delete old status-line captures of Claude sessions that are no longer running.

        The age check spares a capture the hook wrote before Herdr listed its session.
        """
        if time.monotonic() - self.last_prune < PRUNE_SECONDS:
            return
        self.last_prune = time.monotonic()
        keep = {digest(sid) + ".json" for sid in live_sessions}
        cutoff = time.time() - CAPTURE_MAX_AGE
        for path in self.directory.glob("*.json"):
            if not CAPTURE_NAME.fullmatch(path.name) or path.name in keep:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def status(self, tally, sessions, hook, integrations, errors):
        status = {"checked_at": int(time.time()), "agents": sessions,
                  "models": tally.models, "contexts": tally.contexts, "pid": os.getpid(),
                  "missing_session": tally.missing_session, "missing_context": tally.missing_context,
                  "claude_hook": hook, "claude_hook_supported": not portable.WINDOWS,
                  "integrations": integrations, **errors}
        if tally.unreadable_session:
            status["unreadable_session"] = tally.unreadable_session
        if tally.observe_errors:
            status["observe_errors"] = tally.observe_errors
        if self.settings.error:
            status["settings_error"] = self.settings.error
        return status


def enabled(endpoint):
    from installation import active
    if not active():
        return False
    plugins = rpc(endpoint, "plugin.list")["plugins"]
    return any(p.get("plugin_id") == PLUGIN and p.get("enabled") for p in plugins)


def record_error(path, error):
    atomic_json(path, {"checked_at": int(time.time()), "error": type(error).__name__, "pid": os.getpid()})


def animate(endpoint, directory, stop):
    """Run the animation loop, restarting it after an unexpected error.

    It stops for good after MAX_FAILURES crashes in a row, so a programming
    error shows in animation-status.json instead of looping forever.
    """
    import sidebar_state
    failures = 0
    while not stop.is_set() and failures < MAX_FAILURES:
        started = time.monotonic()
        try:
            sidebar_state.run(endpoint, directory, stop)
        except Exception as error:
            # A crash after a long healthy run starts a new count.
            failures = 1 if time.monotonic() - started > 60 else failures + 1
            try:
                record_error(directory / "animation-status.json", error)
            except OSError:
                pass
            stop.wait(1)


def collect(endpoint, directory, collector):
    """Sync until the plugin is disabled or MAX_FAILURES syncs in a row fail.

    Any exception counts, so malformed local data cannot end the worker at
    once, while a persistent programming error still stops it with its type
    recorded in status.json.
    """
    failures = 0
    while True:
        try:
            if not enabled(endpoint):
                return
            collector.sync()
            failures = 0
        except Exception as error:
            failures += 1
            record_error(directory / "status.json", error)
            if failures >= MAX_FAILURES:
                return
        time.sleep(POLL_SECONDS)


def watch(endpoint, root):
    directory = endpoint_state(root, endpoint)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "watch.lock").open("a") as lock:
        if not portable.try_lock(lock):
            return
        if not enabled(endpoint):
            return
        (directory / "config-path").write_text(str(runtime.config_path()))
        (directory / "endpoint").write_text(endpoint)
        stop_animation = threading.Event()
        animation = threading.Thread(target=animate, args=(endpoint, directory, stop_animation), daemon=True)
        animation.start()
        try:
            collect(endpoint, directory, Collector(endpoint, root))
        finally:
            stop_animation.set()
            animation.join(timeout=3)


def start(endpoint, root):
    from installation import active
    if not active() or runtime.worker_running(endpoint_state(root, endpoint)):
        return
    portable.spawn([sys.executable, str(Path(__file__).resolve()), "watch"])


def claude_statusline(root):
    raw = sys.stdin.buffer.read(MAX_LINE)
    try:
        from sidebar_settings import read_settings
        payload = json.loads(raw) if read_settings()["collect_details"] else {}
        sid = payload.get("session_id")
        endpoint, pane = os.environ.get("HERDR_SOCKET_PATH"), os.environ.get("HERDR_PANE_ID")
        if os.environ.get("HERDR_ENV") == "1" and endpoint and pane and isinstance(sid, str) and sid:
            observation = claude_observation(payload)
            atomic_json(endpoint_state(root, endpoint) / (digest(sid) + ".json"),
                        {"pane_id": pane, "captured_at": time.time(), "observation": observation})
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # An optional label must never prevent the existing status line.
    previous = read_json(root / "previous-statusline.json")
    if isinstance(previous, dict) and isinstance(previous.get("command"), str):
        # This is the user's existing status-line command, not transcript data.
        try:
            result = subprocess.run(previous["command"], shell=True, input=raw,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
            sys.stdout.buffer.write(result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "watch", "sync", "status", "claude-statusline"])
    args = parser.parse_args()
    root = state_root()
    if args.command == "claude-statusline":
        claude_statusline(root)
        return
    endpoint = os.environ.get("HERDR_SOCKET_PATH")
    if not endpoint or (os.environ.get("HERDR_ENV") != "1" and not os.environ.get("HERDR_PLUGIN_ROOT")):
        parser.error("Run inside a Herdr pane or through its plugin actions")
    if args.command == "status":
        directory = endpoint_state(root, endpoint)
        status = read_json(directory / "status.json")
        status["animation"] = read_json(directory / "animation-status.json")
        print(json.dumps(status))
    elif args.command == "sync":
        from installation import active
        if not active():
            parser.error("Run the Sidebar Customizer setup action first")
        print(json.dumps(Collector(endpoint, root).sync()))
        start(endpoint, root)
    elif args.command == "start":
        start(endpoint, root)
    else:
        watch(endpoint, root)


if __name__ == "__main__":
    main()
