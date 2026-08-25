"""Panel-wide concerns: settings, backup and restore, the update check.

Everything here is about the panel itself rather than about the VPN. The VPN
lives in files under /etc/amnezia/amneziawg and is reached through ``awg.store``;
what this app owns is the small pile of state a config file cannot express.
"""
