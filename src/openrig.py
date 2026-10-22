"""Sessions and context for agents that OpenRig runs, which Herdr sees only as a tile.

An OpenRig seat lives in the OpenRig daemon's tmux server, so Herdr's own agent
integration never learns which session it runs. Herdr shows it through a tile
whose foreground process is `tmux attach -t <member>@<rig>`. The local OpenRig
daemon knows that seat's session ID and its context use, so the collector can
describe it like any agent Herdr reports. Only a loopback daemon is asked, and
only while detail collection is on.
"""
import json
import math
import os
import time
from urllib.parse import quote, urlparse
from urllib.request import urlopen

DEFAULT_URL = "http://127.0.0.1:7433"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# Herdr agent kinds by OpenRig runtime; other runtimes keep Herdr's own reporting.
KINDS = {"claude-code": "claude", "codex": "codex"}
TIMEOUT = 1
# A seat's session changes only on relaunch or handover; context changes slowly.
NODES_SECONDS = 10
# What a seat is working on: its newest item still being worked or waiting.
QUEUE_STATES = "in-progress,blocked"
# A tile's attach target never changes while its process lives.
PANE_SECONDS = 30
# Without a daemon, a refused connection is not retried every sync.
BACKOFF_SECONDS = 30
# tmux options before the command that take a value, e.g. the daemon's own -L server.
TMUX_VALUE_FLAGS = {"-L", "-S", "-f", "-c", "-T"}
ATTACH = {"attach", "attach-session", "a"}


def daemon_url(environ=None):
    """The OpenRig daemon to ask, or None when it is not on this machine."""
    environ = os.environ if environ is None else environ
    url = environ.get("OPENRIG_URL") or DEFAULT_URL
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    return url.rstrip("/") if host in LOOPBACK else None


def attached_session(argv):
    """The tmux session a `tmux attach` command line targets, or None."""
    if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
        return None
    if os.path.basename(argv[0]) != "tmux":
        return None
    args = [arg for arg in argv[1:] if isinstance(arg, str)]
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in TMUX_VALUE_FLAGS else 1
    if index >= len(args) or args[index] not in ATTACH:
        return None
    rest = args[index + 1:]
    for position, arg in enumerate(rest):
        if arg == "-t" and position + 1 < len(rest):
            target = rest[position + 1]
        elif arg.startswith("-t") and len(arg) > 2:
            target = arg[2:]
        else:
            continue
        # "=name" asks tmux for an exact match; ":window" picks a window.
        return target.lstrip("=").partition(":")[0] or None
    return None


def fetch_json(url):
    with urlopen(url, timeout=TIMEOUT) as response:
        return json.load(response)


def node_list(body):
    """Nodes from a rig's nodes response, whichever envelope the daemon uses."""
    if isinstance(body, dict):
        body = body.get("nodes")
    return [node for node in body if isinstance(node, dict)] if isinstance(body, list) else []


def agent_session(node, kind):
    """A Herdr-shaped session for a seat of the agent's own kind, else None."""
    token = node.get("resumeToken")
    if KINDS.get(node.get("runtime")) != kind or not isinstance(token, str) or not token:
        return None
    return {"agent": kind, "kind": "id", "source": "openrig", "value": token}


def context_reading(node):
    """(used tokens, window, percent) from a known, fresh context reading, else None.

    OpenRig samples it from the seat's own status line; percent and window are
    what that reports, and the used count is derived from them.
    """
    usage = node.get("contextUsage")
    if not isinstance(usage, dict) or usage.get("availability") != "known" or usage.get("fresh") is not True:
        return None
    window, percent = usage.get("contextWindowSize"), usage.get("usedPercentage")
    return None if window is None or percent is None else (None, window, percent)


class Bridge:
    """Maps agent panes to OpenRig seats; Herdr and HTTP calls are injected for tests."""

    def __init__(self, endpoint, rpc, fetch=fetch_json, url=None, clock=time.monotonic):
        self.endpoint, self.rpc, self.fetch, self.clock = endpoint, rpc, fetch, clock
        self.url = daemon_url() if url is None else url
        self.panes = {}  # pane -> (attached session or None, checked at)
        self.nodes = {}  # rig name -> ({session name: node}, fetched at)
        self.items, self.items_at = [], -math.inf
        self.down_until = -math.inf

    def seat(self, pane):
        """The OpenRig node a pane's tile attaches to, or None."""
        session = self.session(pane)
        if not session or "@" not in session:
            return None
        return self.rig_nodes(session.rpartition("@")[2]).get(session)

    def session(self, pane):
        now = self.clock()
        cached = self.panes.get(pane)
        if cached and now - cached[1] < PANE_SECONDS:
            return cached[0]
        info = self.rpc(self.endpoint, "pane.process_info", {"pane_id": pane}).get("process_info") or {}
        processes = info.get("foreground_processes") or []
        session = next((found for found in (attached_session(p.get("argv")) for p in processes
                                            if isinstance(p, dict)) if found), None)
        self.panes[pane] = (session, now)
        return session

    def rig_nodes(self, rig):
        now = self.clock()
        cached = self.nodes.get(rig)
        if self.url is None or (cached and now - cached[1] < NODES_SECONDS) or now < self.down_until:
            return cached[0] if cached else {}
        try:
            rigs = self.fetch(self.url + "/api/ps")
            rig_id = next((r.get("rigId") for r in rigs if isinstance(r, dict)
                           and (r.get("rigName") or r.get("name")) == rig), None)
            body = self.fetch(f"{self.url}/api/rigs/{quote(rig_id, safe='')}/nodes") if rig_id else []
        except (OSError, ValueError, TypeError):
            self.down_until = now + BACKOFF_SECONDS
            return cached[0] if cached else {}
        found = {node["canonicalSessionName"]: node for node in node_list(body)
                 if isinstance(node.get("canonicalSessionName"), str)}
        self.nodes[rig] = (found, now)
        return found

    def task(self, session):
        """The summary of the newest in-progress or blocked queue item for a seat, or None."""
        items = [item for item in self.queue() if item.get("destinationSession") == session
                 and isinstance(item.get("summary"), str) and item["summary"].strip()]
        newest = max(items, key=lambda item: str(item.get("tsUpdated") or ""), default=None)
        return newest["summary"].strip() if newest else None

    def queue(self):
        now = self.clock()
        if self.url is None or now - self.items_at < NODES_SECONDS or now < self.down_until:
            return self.items
        try:
            body = self.fetch(self.url + "/api/queue/list?state=" + QUEUE_STATES)
        except (OSError, ValueError, TypeError):
            self.down_until = now + BACKOFF_SECONDS
            return self.items
        self.items = [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []
        self.items_at = now
        return self.items

    def forget(self, live_panes):
        self.panes = {pane: value for pane, value in self.panes.items() if pane in live_panes}
