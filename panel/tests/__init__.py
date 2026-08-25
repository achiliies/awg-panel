# A package, not a bare directory, so `tests/` gets its own import namespace and
# pytest puts panel/ on sys.path instead of tests/ - which is what makes
# `from awg import conf` resolve the same way it does under gunicorn.
