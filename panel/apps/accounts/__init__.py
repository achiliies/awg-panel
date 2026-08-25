"""Who may drive the panel, and how they prove it.

Most of what authenticates a request is somebody else's table. An operator is a
``django.contrib.auth`` user, the second factor is a ``django_otp`` TOTP device,
the session is ``django.contrib.sessions``, and the lockout counter belongs to
``axes`` - four things that already have models, and a fifth here would only be
a second place for the same facts.

What this app owns is the two rows those tables cannot hold. ``LoginSession``
records what a Django session cannot say about itself - when it began, from
where, and on what - so the question "what else is signed in to my panel"
has an answer. ``ApiToken`` is the credential for a caller that never makes a
session at all, and the reason it is a model rather than a value in a config
file is the same: a token has to be nameable in the log, expirable on its own
and revocable without touching what a person signs in with.

It also owns the login handshake, which is the part with an adversary. The panel
is reachable from the open internet on whatever port the operator chose, so a
failed sign-in has to cost something (axes), a stolen password must not be enough
on its own (TOTP), and the answer must never say which of the two halves was
wrong.
"""
