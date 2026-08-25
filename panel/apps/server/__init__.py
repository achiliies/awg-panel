"""The tunnel itself: what is in awg0.conf and what the kernel is doing with it.

The app owns no models. Every fact it serves comes from the files under
/etc/amnezia/amneziawg, read through ``awg.store`` on each request, and from the
live interface through ``awg.controller``. A database row here would be a second
copy of something the bash tools can change over SSH a second later.
"""
