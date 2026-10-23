"""A person's terminal, for e2e/pty.e2e.ts: a plain `tmux attach` on a pty
of its own size, as a terminal window runs it. What comes on stdin is typed
into it; what it draws is read and dropped. It goes (the client detaches)
when stdin closes or it is killed.

    python3 native_client.py <tmux> <server's socket path> <session> <cols> <rows>
"""

import fcntl
import os
import pty
import select
import struct
import sys
import termios

tmux, server, session, cols, rows = sys.argv[1:6]
pid, master = pty.fork()
if pid == 0:
    os.environ.pop("TMUX", None)
    os.environ["TERM"] = "xterm-256color"
    os.execvp(tmux, [tmux, "-S", server, "attach-session", "-t", f"={session}"])
fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
print("attached", flush=True)
stdin = sys.stdin.fileno()
try:
    while True:
        ready, _, _ = select.select([master, stdin], [], [])
        if master in ready:
            try:
                if not os.read(master, 65536):
                    break
            except OSError:
                break
        if stdin in ready:
            data = os.read(stdin, 4096)
            if not data:
                break
            os.write(master, data)
finally:
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass
