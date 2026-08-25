"""Random names, for the things that are created without one.

A client and an API token are both named by whoever creates them, and in both
cases the name is an identifier rather than a label: a client's name is what its
config file is called and what every per-client route is addressed by, and a
token's name is what the activity log writes beside everything that token does.
So something has to be in the box, and an operator adding the fortieth client of
an afternoon should not have to invent the fortieth word.

Nine characters out of the alphabet below, drawn here so that both callers draw
them the same way.

The alphabet is lower case and leaves out `l`, `1`, `o` and `0`. These names are
read off a screen and typed back into a shell, a support message or another
panel, and those four are the pairs that get read wrong when they are; dropping
them costs a quarter of a bit per character and removes the one failure mode a
random name has. What is left satisfies both name rules the panel has - the
client name pattern in awg.store and the printable-text rule for a token - so a
name from here is never refused by the thing it was drawn for.

Thirty-two characters over nine positions is forty-five bits, which is why
`unique_name` exists and why it is not a loop anybody should expect to go round
twice. A collision is not the reason for the check: the reason is that the name
is an identifier, and an identifier that is merely very unlikely to be taken is
still one that can be, on the one server where it happens, silently overwriting
somebody's client if nothing looks first.
"""

import secrets
from collections.abc import Callable

# Deliberately not `random`: nothing here is a secret, but the process-wide
# generator can be seeded by anything that imports it, and names repeating after
# a restart is exactly what this module exists to avoid.
ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

LENGTH = 9

# How many draws before giving up and saying so. Reaching the end of this means
# the caller's "is it taken" answered yes thirty-two times running, which on a
# space this size is not a full table - it is something wrong with the question.
ATTEMPTS = 32


def random_name(length: int = LENGTH) -> str:
    """One name, with nothing checked about it."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def unique_name(
    taken: Callable[[str], bool], *, length: int = LENGTH, attempts: int = ATTEMPTS
) -> str | None:
    """A name `taken` says nothing already answers to, or None if there was none.

    The check is the caller's, because what "already taken" means differs: a
    client is taken by a peer in the server config or by a file left behind in
    the clients directory, and a token by a row on the account issuing it.

    None rather than an exception, so each caller can raise the error its own
    layer knows how to word.
    """
    for _ in range(attempts):
        candidate = random_name(length)
        if not taken(candidate):
            return candidate
    return None
