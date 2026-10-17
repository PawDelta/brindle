"""The sidebar header's braid: the website mark, drawn as a sixel image.

Only where the terminal shows sixel (inside tmux: the client's termfeatures
say so). Under tmux a transparent sixel pixel shows as orange, so every pixel
is blended onto the pane's own background when tmux gives it one (a brindle
session does), else the terminal's, read once with OSC 11 before curses starts
(unanswered: the pane's background as tmux knows it); that background also
picks the light or dark art.
"""
from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import subprocess
import termios
import time
import tty
import zlib
from functools import lru_cache
from importlib import resources

ROWS = 3  # terminal rows the image spans
RECHECK_SECONDS = 10.0  # how often to look again while tmux can't show it (no client yet)


class Braid:
    """The braid image for one terminal, once tmux can show it there."""

    def __init__(self, bg: tuple[int, int, int] | None, cell: tuple[int, int] | None):
        self.bg, self.cell = bg, cell   # as the terminal answered before curses, if it did
        self._ready, self._checked = False, -RECHECK_SECONDS

    def image(self, fd: int = 1) -> tuple[str, int] | None:
        """The sixel and its width in cells; None while the terminal can't show it."""
        if not self._ready and time.monotonic() - self._checked >= RECHECK_SECONDS:
            self._checked = time.monotonic()
            # No answer to OSC 11 (tmux gives none for a window no client shows):
            # the pane's own background, which a brindle session sets.
            self.bg = self.bg or _pane_bg()
            self._ready = self.bg is not None and _shows_sixel()
        cell = _winsize_cell(fd) or self.cell  # now: a font change alters it
        if not self._ready or not cell:
            return None
        return sixel(theme(self.bg), self.bg, *cell)

    def recheck(self) -> None:
        """Look again on the next image() call: a terminal just started showing it."""
        self._checked = -RECHECK_SECONDS


def probe(fd_in: int = 0, fd_out: int = 1) -> Braid | None:
    """The braid for this terminal, None outside tmux. Asks the terminal its
    background (and cell size) and reads the answers, so call it before curses."""
    if not (os.isatty(fd_in) and os.isatty(fd_out) and os.environ.get("TMUX")):
        return None
    cell = _winsize_cell(fd_out)
    # Primary device attributes last: every terminal answers it, after the others.
    reply = _ask(fd_in, fd_out, "\x1b]11;?\x1b\\" + ("" if cell else "\x1b[16t") + "\x1b[c")
    if not cell and (px := re.search(r"\[6;(\d+);(\d+)t", reply)):
        cell = (int(px[2]), int(px[1]))
    m = re.search(r"\]11;rgb:([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)", reply, re.I)
    bg = tuple(int(v, 16) * 255 // (16 ** len(v) - 1) for v in m.groups()) if m else None
    # A pane with its own background (a brindle session sets one) is drawn in it,
    # whatever the outer terminal's is: image and palette must match the pane.
    return Braid(_pane_bg() or bg, cell)  # type: ignore[arg-type]


def theme(bg: tuple[int, int, int]) -> str:
    """The art for a background: light or dark."""
    return "light" if sum(bg) / 3 > 140 else "dark"


def show(fd: int, sixel: str, row: int = 0, col: int = 1) -> None:
    """Draw the image with its top-left at (row, col), leaving the cursor where it was."""
    os.write(fd, f"\x1b7\x1b[{row + 1};{col + 1}H{sixel}\x1b8".encode())


@lru_cache(maxsize=8)
def sixel(theme: str, bg: tuple[int, int, int], cw: int, ch: int) -> tuple[str, int]:
    """The braid sized for ROWS rows of ch-pixel cells, and its width in cells."""
    pad = max(1, ch // 5)
    # Whole sixel bands (a part-painted last band shows orange under tmux), within ROWS rows.
    height = -(-(ROWS * ch - pad) // 6) * 6
    if height > ROWS * ch:
        height -= 6
    rows = _box(_art(theme), min(ROWS * ch - 2 * pad, height - pad), bg)
    # Out to whole cells: iTerm2 fills the rest of a part-covered cell in its own colour.
    width = -(-len(rows[0]) // cw) * cw
    rows = [line + [bg] * (width - len(line)) for line in rows]
    # Background bands above (to centre it) and below (to the band's end).
    rows = [[bg] * width for _ in range(pad)] + rows
    rows += [[bg] * width for _ in range(height - len(rows))]
    return _encode(rows, bg), -(-width // cw)


def _tmux_format(fmt: str) -> str:
    target = ["-t", os.environ["TMUX_PANE"]] if os.environ.get("TMUX_PANE") else []
    try:
        return subprocess.run(["tmux", "display", "-p", *target, fmt],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def viewer() -> str:
    """The tty of the terminal showing this pane, "" while none is."""
    # Without a client of its own, tmux names whichever client is most recent.
    return _tmux_format("#{?session_attached,#{client_tty},}")


def _shows_sixel() -> bool:
    return "sixel" in _tmux_format("#{client_termfeatures}").split(",")


def _pane_bg() -> tuple[int, int, int] | None:
    # pane_bg stays "default" until tmux has drawn the window; the style says it sooner.
    m = re.search(r"(?:^|bg=)#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})\b",
                  _tmux_format("#{pane_bg} #{window-style}"), re.I)
    return tuple(int(v, 16) for v in m.groups()) if m else None  # type: ignore[return-value]


def _winsize_cell(fd: int) -> tuple[int, int] | None:
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return None
    return (xp // cols, yp // rows) if rows and cols and xp and yp else None


def _ask(fd_in: int, fd_out: int, query: str, timeout: float = 0.5) -> str:
    """Send query and read the replies, up to the device attributes one."""
    old = termios.tcgetattr(fd_in)
    reply, end = "", time.monotonic() + timeout
    try:
        tty.setraw(fd_in)
        os.write(fd_out, query.encode())
        while (left := end - time.monotonic()) > 0 and select.select([fd_in], [], [], left)[0]:
            reply += os.read(fd_in, 256).decode(errors="ignore")
            if re.search(r"\x1b\[\?[\d;]*c", reply):
                break
    finally:
        termios.tcsetattr(fd_in, termios.TCSAFLUSH, old)  # a late reply mustn't reach curses as keys
    return reply


@lru_cache(maxsize=2)
def _art(theme: str) -> tuple[int, int, list[bytes]]:
    return _png(resources.files("brindle").joinpath("assets", f"braid_{theme}.png").read_bytes())


def _png(data: bytes) -> tuple[int, int, list[bytes]]:
    """Width, height and RGBA rows of an 8-bit RGBA, non-interlaced PNG."""
    w, h, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if (depth, colour, interlace) != (8, 6, 0):
        raise ValueError("not an 8-bit RGBA PNG")
    pos, idat = 8, bytearray()
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw, stride = zlib.decompress(bytes(idat)), 4 * w
    rows, prev = [], bytearray(stride)
    for y in range(h):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 2:
            line = bytearray((v + u) & 255 for v, u in zip(line, prev))
        elif kind:
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                b, c = prev[i], prev[i - 4] if i >= 4 else 0
                if kind == 3:
                    a = (a + b) // 2
                elif kind == 4:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    a = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + a) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, rows


def _box(art: tuple[int, int, list[bytes]], height: int, bg: tuple[int, int, int]) -> list[list]:
    """The art's opaque part scaled to height by box filter, blended onto bg: no
    pixel is left transparent."""
    w, h, src = art
    ys = [y for y in range(h) if any(src[y][3::4])]
    xs = [x for x in range(w) if any(src[y][4 * x + 3] for y in ys)]
    x0, y0 = xs[0], ys[0]
    s = (ys[-1] + 1 - y0) / height
    width = round((xs[-1] + 1 - x0) / s)
    out = []
    for ty in range(height):
        ay = y0 + int(ty * s)
        by = max(y0 + int((ty + 1) * s), ay + 1)
        line = []
        for tx in range(width):
            ax = x0 + int(tx * s)
            bx = max(x0 + int((tx + 1) * s), ax + 1)
            r = g = b = a = n = 0
            for y in range(ay, min(by, h)):
                row = src[y]
                for x in range(ax, min(bx, w)):
                    al = row[4 * x + 3]
                    r += row[4 * x] * al
                    g += row[4 * x + 1] * al
                    b += row[4 * x + 2] * al
                    a += al
                    n += 1
            cover = a / max(n, 1) / 255
            line.append(tuple((sum_ / a if a else 0) * cover + base * (1 - cover)
                              for sum_, base in zip((r, g, b), bg)))
        out.append(line)
    return out


def _encode(rows: list[list], bg: tuple[int, int, int]) -> str:
    """Sixel for rows of RGB pixels, every pixel painted (P2=0, nothing transparent)."""
    exact = tuple(round(v * 100 / 255) for v in bg)
    step = 1
    while True:  # sixel colours are percentages; coarsen until the palette fits 256 registers
        # ...but never the background, or the image shows a frame against the pane.
        img = [[exact if max(abs(a - b) for a, b in zip(px, bg)) < 1.5
                else tuple(round(v * 100 / 255 / step) * step for v in px) for px in line] for line in rows]
        palette = {c: i for i, c in enumerate(sorted({c for line in img for c in line}))}
        if len(palette) <= 256:
            break
        step += 1
    width, height = len(img[0]), len(img)
    out = [f'\x1bP0;0;0q"1;1;{width};{height}']
    out += [f"#{i};2;{r};{g};{b}" for (r, g, b), i in palette.items()]
    for top in range(0, height, 6):
        band = img[top:top + 6]
        for colour in sorted({c for line in band for c in line}, key=palette.get):
            bits = "".join(chr(63 + sum(1 << k for k, line in enumerate(band) if line[x] == colour))
                           for x in range(width))
            out.append(f"#{palette[colour]}" + _runs(bits) + "$")  # full rows: nothing left unset
        out.append("-")
    # No graphics newline after the last band: iTerm2 paints the band it opens.
    return "".join(out[:-1]) + "\x1b\\"


def _runs(s: str) -> str:
    """Sixel run-length encoding: !n<ch> for a run of more than three."""
    return re.sub(r"(.)\1{3,}", lambda m: f"!{len(m[0])}{m[1]}", s)
