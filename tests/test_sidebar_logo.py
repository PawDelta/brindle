import re

from brindle import sidebar_logo


def test_pane_style_background_beats_osc11(monkeypatch):
    # A brindle session paints its panes #17130f; the outer terminal's own colour
    # (OSC 11) differs, and the image must match the pane, not the terminal.
    monkeypatch.setattr(sidebar_logo.os, "isatty", lambda fd: True)
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setattr(sidebar_logo, "_winsize_cell", lambda fd: (8, 16))
    monkeypatch.setattr(sidebar_logo, "_ask", lambda *a: "\x1b]11;rgb:ffff/ffff/ffff\x1b\\\x1b[?62c")
    style = {"v": "default bg=#17130f,fg=#ddd"}
    monkeypatch.setattr(sidebar_logo, "_tmux_format", lambda fmt: style["v"])
    braid = sidebar_logo.probe()
    assert braid.bg == (0x17, 0x13, 0x0f) and sidebar_logo.theme(braid.bg) == "dark"
    style["v"] = "default "  # default background: the terminal's answer stands
    assert sidebar_logo.probe().bg == (255, 255, 255)


def test_every_braid_pixel_is_painted_on_the_terminal_background():
    # Under tmux an unpainted (transparent) sixel pixel shows as orange.
    bg = (28, 27, 26)
    six, cols = sidebar_logo.sixel(sidebar_logo.theme(bg), bg, 8, 16)
    assert six.startswith("\x1bP0;0;") and sidebar_logo.theme((250, 250, 250)) == "light"
    w, h = map(int, re.search(r'"1;1;(\d+);(\d+)', six).groups())
    assert cols == -(-w // 8) and h <= sidebar_logo.ROWS * 16 and h % 6 == 0
    painted = set()
    for band, text in enumerate(six[:-2].split("-")):
        for data in re.findall(r"#\d+([?-~!\d]+)\$", text):
            x = 0
            for n, ch in re.findall(r"(?:!(\d+))?([?-~])", data):
                for _ in range(int(n or 1)):
                    painted |= {(x, 6 * band + k) for k in range(6) if (ord(ch) - 63) >> k & 1}
                    x += 1
    assert painted == {(x, y) for x in range(w) for y in range(h)}


def test_background_keeps_its_exact_colour_when_the_palette_is_coarsened():
    # On a retina cell (14x34) the braid needs more than 256 colours, so the
    # encoder coarsens them; the background must stay its nearest exact sixel
    # colour, or a darker frame shows around the image (seen in iTerm2).
    bg = (13, 13, 15)
    six, _ = sidebar_logo.sixel("dark", bg, 14, 34)
    regs = {tuple(map(int, c)) for c in re.findall(r"#\d+;2;(\d+);(\d+);(\d+)", six)}
    assert tuple(round(v * 100 / 255) for v in bg) in regs
    # iTerm2 paints a strip in its own colour past the image: to the next whole
    # cell on the right, and one band down after a trailing graphics newline.
    w = int(re.search(r'"1;1;(\d+);', six)[1])
    assert w % 14 == 0 and not six.endswith("-\x1b\\")
