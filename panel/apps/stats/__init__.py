"""Today's traffic, the live blob, and the process that produces both.

Two very different things live here. The API side is nearly all read, and the
reads are cheap: they serve a file the collector wrote and one row of one table,
and they never touch `awg` - a dashboard polling every two seconds must not fork
a process every two seconds. The exception is the one write, which clears every
traffic figure on the server and is the only thing in the panel that takes both
halves of this package to do.

The collector is the other half. It is the only part of the panel that runs
without a request behind it: one loop that polls the interface, keeps traffic.db
in step with the bash tools, adds up the day's traffic, and enforces quotas and
expiry. Everything it writes is either a file the CLI already understands or a
table that holds what a config file cannot.
"""
