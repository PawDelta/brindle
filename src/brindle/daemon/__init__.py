"""``brindle daemon``: the local API the desktop app talks to.

One per machine, on a Unix socket (``~/.brindle/daemon.sock``) and, opt-in,
on 127.0.0.1. Every request carries the token from ``~/.brindle/daemon.json``
(see :mod:`brindle.daemon.auth`); :mod:`brindle.daemon.app` holds the routes
and :mod:`brindle.daemon.lifecycle` starts, finds and stops the process."""
