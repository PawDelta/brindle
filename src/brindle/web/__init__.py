"""`brindle web`: a local browser UI for the current session.

``server`` is the core (auth, routing, serving); every other module in this
package registers its routes with ``server.route`` and is imported when the
server starts, so a new panel is a new module here."""
