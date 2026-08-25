"""The panel's Django applications.

Six of them, split by the thing they own rather than by layer:

* ``accounts``  login, logout, session bootstrap, TOTP enrolment
* ``panel``     panel settings, backup/restore, update check, shared serializers
* ``server``    the server config, parameter catalog, interface status
* ``clients``   peers: CRUD, config download, QR, quota and expiry metadata
* ``stats``     live blob, summaries, today's traffic, the collector command
* ``events``    the event log every other app writes to, and the service journal

They all reach the VPN through ``awg.store``; none of them parses a config file
itself, so the bash tools and the panel can never disagree about the format.
"""
