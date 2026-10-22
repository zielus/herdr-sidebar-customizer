"""Animated expanded-sidebar state tokens, without changing agent lifecycle state.

Runs beside the slow model/context collector, so animation never rereads a
transcript or reassigns colors. Only changed frames and periodic refreshes are
published. Name tokens persist, so the sidebar stays readable if this worker
stops; Restore clears them. Herdr's native renderer owns the compact rail.
"""
import time

from agent_info import PLUGIN, RECOVERABLE, atomic_json, rpc, read_json, endpoint_state, state_root
from filecache import file_stamp

SOURCE = PLUGIN + ".animation"
# Herdr takes at most 16 tokens per report, so grouping reports on its own.
GROUP_SOURCE = PLUGIN + ".group"
# Herdr colors the whole icon/name token, and an invisible marker after the
# glyph selects the shade, so the name stays steady while the glyph animates.
SHADE_MARKERS = {'dark': '', 'medium': '\u2063', 'light': '\u2063\u2063'}
RENEW_SECONDS = 5
# Herdr draws an entry's first row at column 1 and the rest at column 3, and
# trims ordinary spaces. Two blank Braille cells line every agent row up under
# a group heading, whether or not the heading is that agent's own first row.
INDENT = '\u2800' * 2
# Grouped agents: a heading on each space's first agent, details indented
# under the name, and a blank row after each space's last agent.
GROUP_TOKENS = ('group_info', 'group_gap')


def shade_marker(shade):
    # Bound both ends so shorter shade markers cannot match longer ones.
    return '\u2062' + SHADE_MARKERS[shade] + '\u2062'


def label_tokens(status, now, name, color, settings=None, show_name=None, indent=''):
    from space_colors import HUES
    from sidebar_settings import DEFAULTS
    settings = settings or DEFAULTS
    show_name = settings['space_names'] if show_name is None else show_name
    values = {'space_'+hue: None for hue,*_ in HUES}
    if color:
        hue,shade = color.rsplit('_', 1)
        glyph = symbol(status,now,settings) if settings['state_icons'] else ''
        label = ' '.join(p for p in (glyph, name[:65] if show_name else '') if p)
        label = indent + label if label else ''
        values['space_'+hue] = label[:1] + shade_marker(shade) + label[1:] if label else None
    return values


def heading_tokens(name, color, settings):
    """A space's colored name above its group; the hue rules match the name tokens'."""
    from space_colors import HUES
    values = {'group_'+hue: None for hue,*_ in HUES}
    if color and name:
        hue,shade = color.rsplit('_', 1)
        values['group_'+hue] = name[:1] + shade_marker(shade) + name[1:65]
    return values


def agent_row_name(agent, space, settings, pane_label=None):
    """What an agent row shows after its symbol: its space, its name, or nothing.

    The name is the pane's label, which need not be unique and is what OpenRig
    names its seats by, then Herdr's agent name, then the agent type.
    """
    from sidebar_settings import AGENT_NAMES
    choice = settings['agent_row_shows']
    if choice == 'space':
        return space
    if choice == 'agent':
        kind = agent.get('agent') or ''
        return (pane_label or agent.get('name') or settings.get(kind + '_label')
                or AGENT_NAMES.get(kind, kind.title()))
    return ''


def group_positions(agents, workspaces):
    """{pane: (first, last, final group)} in the Agents list's space order."""
    panes = {}
    for agent in agents:
        panes.setdefault(agent.get('workspace_id'), []).append(agent['pane_id'])
    spaces = [w['workspace_id'] for w in workspaces if w['workspace_id'] in panes]
    spaces += [space for space in panes if space not in spaces]
    result = {}
    for space in spaces:
        group = panes[space]
        for pane in group:
            result[pane] = (pane == group[0], pane == group[-1], space == spaces[-1])
    return result


def group_tokens(agent, name, color, position, settings):
    """Heading, indented details and trailing gap for one agent; all clear when off."""
    from runtime import HERDR_TEXT_LIMIT
    values = heading_tokens(None, None, settings) | dict.fromkeys(GROUP_TOKENS)
    if not settings['group_agents']:
        return values
    first, last, final = position
    if first:
        values |= heading_tokens(name, color, settings)
    info = (agent.get('tokens') or {}).get('agent_info')
    if info:
        values['group_info'] = (INDENT + info)[:HERDR_TEXT_LIMIT]
    if last and not final and settings['groups_gap']:
        values['group_gap'] = '⠀'
    return values


def symbol(status, now, settings=None):
    from sidebar_settings import ANIMATIONS, DEFAULTS, frame_interval
    settings = settings or DEFAULTS
    if status == "working" and settings['animate']:
        frames = ANIMATIONS[settings['animation']][1]
        return frames[int(now / frame_interval(settings)) % len(frames)]
    return settings.get(status+'_symbol', settings['unknown_symbol'])


class Animator:
    def __init__(self, endpoint, directory=None):
        from sidebar_settings import SettingsReader
        self.endpoint = endpoint
        self.settings = SettingsReader()
        self.previous = {}
        self.directory = directory or endpoint_state(state_root(), endpoint)
        self.palette_stamp = None
        self.colors = {}

    def palette(self):
        path = self.directory / 'space-colors.json'
        try:
            stamp = file_stamp(path)
        except FileNotFoundError:
            return {}
        if stamp != self.palette_stamp:
            from space_colors import KEYS
            colors = read_json(path)
            if not isinstance(colors, dict) or any(v not in KEYS for v in colors.values()):
                raise ValueError('Invalid saved space colors')
            self.colors, self.palette_stamp = colors, stamp
        return self.colors

    def tick(self, now=None):
        settings = self.settings.load()
        now = time.monotonic() if now is None else now
        agents = rpc(self.endpoint, "agent.list")["agents"]
        workspaces = rpc(self.endpoint, "workspace.list")["workspaces"]
        names = {w['workspace_id']: w.get('label', '') for w in workspaces}
        colors = self.palette()
        positions = group_positions(agents, workspaces)
        labels = self.pane_labels(settings)
        current = set()
        working = 0
        for kind, items, id_key in [("pane", agents, "pane_id"),
                                   ("workspace", workspaces, "workspace_id")]:
            for item in items:
                key = (kind, item[id_key])
                current.add(key)
                status = item.get("agent_status", "unknown")
                working += status == "working" and settings['animate'] and settings['state_icons']
                name = names.get(item.get('workspace_id'), '')
                color = colors.get(item.get('workspace_id'))
                # One styled value avoids Herdr's separator between icon/name.
                # Keep below the API's 80-character cap, including invisible markers.
                if kind == "pane":
                    position = positions[item['pane_id']]
                    grouped = settings['group_agents']
                    show = settings['space_names'] if settings['agent_row_shows'] == 'space' else True
                    # The heading is the first agent's own first row, so only
                    # the other agents need the indent to line up under it.
                    indent = INDENT if grouped and not position[0] else ''
                    row_name = agent_row_name(item, name, settings, labels.get(item['pane_id']))
                    values = label_tokens(status, now, row_name, color,
                                          settings, show_name=show, indent=indent)
                    group_key = (kind, item[id_key], 'group')
                    current.add(group_key)
                    self.report(group_key, id_key, GROUP_SOURCE,
                                group_tokens(item, name, color, position, settings), now)
                else:
                    values = label_tokens(status, now, name, color, settings)
                self.report(key, id_key, SOURCE, values, now)
        # Herdr discards metadata with closed panes/workspaces.
        self.previous = {k:v for k,v in self.previous.items() if k in current}
        result = {"agents": len(agents), "spaces": len(workspaces), "working": working}
        if self.settings.error:
            result['settings_error'] = self.settings.error
        return result

    def pane_labels(self, settings):
        """{pane: label} for panes that have one; read only when agent rows show names."""
        if settings['agent_row_shows'] != 'agent':
            return {}
        panes = rpc(self.endpoint, "pane.list")["panes"]
        return {p['pane_id']: p['label'] for p in panes if isinstance(p.get('label'), str) and p['label'].strip()}

    def report(self, key, id_key, source, values, now):
        """Publish changed values, and unchanged ones again before they go stale."""
        previous = self.previous.get(key)
        if previous and previous[0] == values and now-previous[1] < RENEW_SECONDS:
            return
        rpc(self.endpoint, key[0] + ".report_metadata", {
            id_key: key[1], "source": source,
            "tokens": values})
        self.previous[key] = (values, now)


def run(endpoint, directory, stop):
    animator = Animator(endpoint, directory)
    next_health = 0
    while not stop.is_set():
        now = time.monotonic()
        try:
            state = animator.tick(now)
            from sidebar_settings import frame_interval
            delay = max(.02, min(1, frame_interval(animator.settings.value))) if state["working"] else 1
        except RECOVERABLE as error:
            state = {"error": type(error).__name__}
            delay = 1
        if now >= next_health:
            try:
                atomic_json(directory / "animation-status.json",
                            {"checked_at": int(time.time()), **state})
            except OSError:
                pass  # A failed health write must not kill the animation worker.
            next_health = now + RENEW_SECONDS
        stop.wait(delay)
