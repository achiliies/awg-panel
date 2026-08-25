"""What happened to this server, and what its services said about it.

Two kinds of log, kept apart on purpose, because they answer different
questions and are made of different stuff.

The first is the event log: a row per thing somebody or something did. A client
created, a limit changed, a backup restored, a peer the collector switched off
for its quota. It is the panel's own record, written by the code that performs
the action, and it survives a service restart, a log rotation and a reboot
because it is in the database beside everything else the panel keeps. What it
is for is the question nobody could answer before it existed: this client
stopped working last Tuesday - why, and who did it?

The second is the service log: whatever ``awg-panel-web``,
``awg-panel-collector`` and ``awg-quick@<iface>`` have written to the journal.
Nothing is stored here at all - it is read out of journald on request, exactly
as ``sudo awg-panel logs`` would - and it is the other half of the answer: when
the event log says a save failed, this is where the traceback is.

Neither replaces the ``logging`` calls the rest of the panel already makes. A
line in the journal is for whoever is debugging the panel and can be as long and
as technical as it needs to be; an event is for whoever is running it, has to
mean something a year later, and is read in two languages - so it travels as a
kind and a few named values, and the sentence around them is assembled in the
browser. Recording an event is therefore an addition at a call site, never a
replacement for the log line beside it.
"""
