#!/usr/bin/env python3
"""Persistent space colors and a popup picker, owned by herdr-sidebar-customizer.

One mutually exclusive name token per hue fits Herdr's 16-token row limit.
Static rules cover each shade; sidebar_state publishes
combined icon/name values to both workspaces and agent panes.
"""
import argparse
import json
import os
from pathlib import Path
import random

from agent_info import PLUGIN, atomic_json, endpoint_state, rpc, state_root
import portable

# Exact xterm-256 colors, so no two swatches collapse to the same terminal color.
# Three pastel shades per hue, dark to light; new spaces get the default shade.
HUES = (
    ("red", "#d78787", "#ff8787", "#ffafaf"), ("orange", "#d7af5f", "#ffaf5f", "#ffd7af"),
    ("yellow", "#d7d75f", "#ffff5f", "#ffffaf"), ("lime", "#afd75f", "#afff5f", "#d7ffaf"),
    ("green", "#5fd787", "#5fff87", "#afffaf"), ("teal", "#5fd7af", "#5fffd7", "#afffd7"),
    ("cyan", "#5fafd7", "#5fd7ff", "#afffff"), ("blue", "#5f87d7", "#5fafff", "#afd7ff"),
    ("indigo", "#8787d7", "#afafff", "#d7d7ff"), ("violet", "#af87d7", "#d787ff", "#d7afff"),
    ("magenta", "#d787d7", "#ff87ff", "#ffafff"), ("rose", "#d75f87", "#ff5faf", "#ffafd7"),
)
PALETTE = tuple((hue + "_" + shade, shade, color)
                for hue, dark, medium, light in HUES
                for shade, color in [("dark", dark), ("medium", medium), ("light", light)])
KEYS = [p[0] for p in PALETTE]
# Palette names from older releases, still found in users' space-colors.json.
LEGACY = {"rosewater": "red_light", "flamingo": "rose_light", "pink": "magenta_light",
          "mauve": "violet_light", "red": "red_light", "peach": "orange_light",
          "yellow": "yellow_light", "green": "green_light", "teal": "teal_light",
          "sky": "cyan_light", "blue": "blue_light", "lavender": "indigo_light"}
# These markers are already written into users' config.toml; keep the spelling.
STYLE_BEGIN = "# BEGIN herdr-sidebar-customizer space colours"
STYLE_END = "# END herdr-sidebar-customizer space colours"
GROUP_BEGIN = "# BEGIN herdr-sidebar-customizer group headings"
GROUP_END = "# END herdr-sidebar-customizer group headings"


def style_block(settings=None, token="space"):
    """Static per-hue rules keep all three shades steady across animation frames.

    `token` "group" styles the group headings with the same colors as names.
    """
    from sidebar_state import shade_marker
    from sidebar_settings import DEFAULTS
    settings = settings or DEFAULTS
    begin, end = (STYLE_BEGIN, STYLE_END) if token == "space" else (GROUP_BEGIN, GROUP_END)
    lines = [begin]
    for hue, dark, medium, light in HUES:
        rules = []
        for shade, color in [("dark", dark), ("medium", medium), ("light", light)]:
            if not settings['space_colours']:
                color = settings['neutral_colour']
            if color != 'theme':
                rules.append('{ contains = ' + json.dumps(shade_marker(shade))
                             + ', fg = "' + color + '" }')
        fg = medium if settings['space_colours'] else settings['neutral_colour']
        style = '    { token = "$' + token + '_' + hue + '", bold = ' + str(settings['name_bold']).lower()
        if fg != 'theme':
            style += ', fg = "' + fg + '"'
        lines.append(style + ', dim = false, rules = [' + ', '.join(rules) + '] },')
    lines.append("    " + end)
    return "\n".join(lines)


def assignments(directory, workspaces, change=None, settings=None):
    """Serialize picker/watcher updates; IDs preserve colors across renames."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "space-colors.lock").open("a") as lock:
        portable.lock(lock)
        path = directory / "space-colors.json"
        # Corrupt state must fail visibly rather than silently rerandomize colors.
        saved = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(saved, dict) or any(v not in KEYS and v not in LEGACY for v in saved.values()):
            raise ValueError("Invalid space color state")
        current = {w["workspace_id"] for w in workspaces}
        colors = {k: LEGACY.get(v, v) for k, v in saved.items() if k in current}
        if change:
            wid, color = change
            if wid not in current or color not in KEYS:
                raise ValueError("Space no longer exists or color is invalid")
            colors[wid] = color
        from sidebar_settings import read_settings
        settings = settings or read_settings()
        for workspace in workspaces:
            wid = workspace["workspace_id"]
            if wid not in colors:
                colors[wid] = random.SystemRandom().choice(new_space_choices(settings))
        if colors != saved:
            atomic_json(path, colors)
        return colors


def sync(endpoint, directory, *, change=None, settings=None, load=None):
    """Save color assignments for the live spaces and return them.

    `load` supplies settings when none are given; it defaults to the strict
    read_settings, while the collector passes its last-valid SettingsReader.
    """
    # One lock across listing and saving keeps an older watcher's workspace
    # list from overwriting a picker result or resurrecting a closed space.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "space-colors-sync.lock").open("a") as lock:
        portable.lock(lock)
        # Read settings only after taking the lock, which Apply also holds, so
        # a collector waiting on Apply never assigns from stale preferences.
        if settings is None:
            from sidebar_settings import read_settings
            settings = (load or read_settings)()
        workspaces = rpc(endpoint, "workspace.list")["workspaces"]
        return assignments(directory, workspaces, change, settings)


def new_space_choices(settings):
    if settings['default_hue'] == 'random':
        hues = [hue for hue, *_ in HUES]
    else:
        hues = [settings['default_hue']]
    return [hue + '_' + settings['default_shade'] for hue in hues]


def target_workspace():
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    return context.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")


def open_picker(endpoint):
    wid = target_workspace()
    if not wid:
        raise ValueError("Select a space first")
    # Popup placement is always over the active pane. Herdr rejects an explicit
    # workspace_id here; preserve the color target in the popup environment.
    rpc(endpoint, "plugin.pane.open", {"plugin_id": PLUGIN,
        "entrypoint": "space-colors",
        "env": {"SIDEBAR_CUSTOMIZER_WORKSPACE": wid}})


def nearest_color(hex_value):
    if hex_value == 'theme':
        return -1
    rgb = tuple(int(hex_value[i:i+2], 16) for i in (1, 3, 5))
    # Swatches use xterm colors without redefining any terminal palette entries.
    levels = (0, 95, 135, 175, 215, 255)
    candidates = [(16+36*r+6*g+b, (levels[r], levels[g], levels[b]))
                  for r in range(6) for g in range(6) for b in range(6)]
    candidates += [(232+i, (8+i*10,)*3) for i in range(24)]
    return min(candidates, key=lambda p: sum((a-b)**2 for a,b in zip(rgb,p[1])))[0]


def columns_for(width):
    # Three small squares per hue, with a wider gutter between hue groups.
    return 3 * max(1, min(3, width//18))


def cells(width):
    columns = columns_for(width)
    return [(i, 3 + (i//columns)*3,
             2 + ((i%columns)//3)*18 + (i%3)*5) for i in range(len(PALETTE))]


def clicked(width, x, y):
    for i, row, col in cells(width):
        if col <= x < col+4 and row <= y < row+2:
            return i
    return None


class PickerState:
    """Tab changes only the color-edit target, never the user's pane focus."""
    def __init__(self, endpoint, directory, wid):
        self.endpoint, self.directory, self.wid = endpoint, directory, wid
        self.message = ""
        self.refresh()

    def refresh(self, step=0):
        workspaces = rpc(self.endpoint, "workspace.list")["workspaces"]
        if not workspaces:
            raise ValueError("No spaces available")
        ids = [w["workspace_id"] for w in workspaces]
        index = ids.index(self.wid) if self.wid in ids else 0
        if step:
            index = (index+step) % len(ids)
        self.workspace = workspaces[index]
        self.wid = self.workspace["workspace_id"]
        self.position = f"{index+1}/{len(ids)}"
        self.colors = assignments(self.directory, workspaces)
        self.selected = KEYS.index(self.colors[self.wid])
        self.message = ""

    def apply(self, index):
        sync(self.endpoint, self.directory, change=(self.wid, KEYS[index]))
        self.selected = index
        self.message = "Saved"


def picker_screen(screen, state):
    import curses
    curses.curs_set(0)
    screen.keypad(True)
    curses.mouseinterval(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.set_escdelay(30)
    curses.start_color()
    curses.use_default_colors()
    for i, (_, _, color) in enumerate(PALETTE):
        fg = nearest_color(color) if curses.COLORS >= 256 else (i % 7)+1
        curses.init_pair(i+1, fg, -1)
    # A short terminal scrolls the square grid to keep the selection visible.
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        def put(y, x, value, attr=0):
            if 0 <= y < height and x < width-1:
                screen.addnstr(y, x, value, max(0,width-x-1), attr)
        put(0, 1, "Color: " + state.workspace["label"], curses.A_BOLD)
        put(1, 1, state.position + "  " + state.message)
        grid = cells(width)
        columns = columns_for(width)
        visible_rows = max(1,(height-7)//3)
        selected_row = state.selected//columns
        total_rows = (len(PALETTE)+columns-1)//columns
        first_row = max(0, min(selected_row-visible_rows+1, total_rows-visible_rows))
        fits = height >= 10 and width >= 18
        if not fits:
            put(3, 1, "Enlarge terminal; Esc closes")
        else:
            for i, row, col in grid:
                row -= first_row*3
                if row < 3 or row+2 > height-3:
                    continue
                for offset in range(2):
                    put(row+offset, col, "████", curses.color_pair(i+1))
                if i == state.selected:
                    put(row+1, col-1, ">", curses.A_BOLD)
                    put(row+1, col+4, "<", curses.A_BOLD)
            put(height-3, 1, "Click / Enter: save   Tab: next space")
            put(height-2, 1, "Shift+Tab: previous   Esc: close")
            put(height-1, 1, "Arrows: choose color")
        screen.refresh()
        key = screen.getch()
        if key in (27, ord('q')):
            return
        try:
            if key in (9, curses.KEY_BTAB):
                state.refresh(-1 if key == curses.KEY_BTAB else 1)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
                state.selected = (state.selected + {curses.KEY_LEFT:-1, curses.KEY_RIGHT:1,
                            curses.KEY_UP:-columns, curses.KEY_DOWN:columns}[key]) % len(PALETTE)
                state.message = ""
            elif key in (10,13,curses.KEY_ENTER) and fits:
                state.apply(state.selected)
            elif key == curses.KEY_MOUSE and fits:
                _, x, y, _, buttons = curses.getmouse()
                index = clicked(width,x,y+first_row*3) if 3 <= y < height-3 else None
                if index is not None and buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    state.apply(index)
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            state.message = "Could not save/load: " + type(error).__name__


def main():
    import curses
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["open", "picker", "sync"])
    args = parser.parse_args()
    endpoint = os.environ.get("HERDR_SOCKET_PATH")
    if not endpoint or os.environ.get("HERDR_ENV") != "1":
        parser.error("Run inside Herdr")
    directory = endpoint_state(state_root(), endpoint)
    if args.command == "open":
        open_picker(endpoint)
    elif args.command == "sync":
        sync(endpoint, directory)
    else:
        wid = os.environ.get("SIDEBAR_CUSTOMIZER_WORKSPACE") or target_workspace()
        state = PickerState(endpoint, directory, wid)
        curses.wrapper(picker_screen, state)


if __name__ == "__main__":
    main()
