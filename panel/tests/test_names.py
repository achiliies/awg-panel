"""The random names the panel gives a client or a token that arrives without one.

Two things are worth pinning about a generator this small, and neither is the
randomness.

The first is that what it draws is always a legal name for both of the things
that ask for one. A client's name becomes a file name and is matched against
awg.store.NAME_RE; a token's is any printable text. A single character outside
that intersection would be a suggestion the panel offers and then refuses,
which is the sort of bug that only appears one draw in a thousand.

The second is that `unique_name` really does keep drawing. It is the whole of
what stands between a generated name and an identifier that already belongs to
somebody, and its callers give it a different question about "taken" each time -
so the loop is checked here against a question with a known answer rather than
inferred from the endpoints that use it.
"""

from awg import names, store


def test_a_name_is_nine_characters_of_the_alphabet():
    for _ in range(200):
        name = names.random_name()
        assert len(name) == names.LENGTH
        assert set(name) <= set(names.ALPHABET)


def test_the_alphabet_leaves_out_the_characters_that_are_read_wrong():
    """These names are read off a screen and typed back into a shell."""
    assert not set("l1o0") & set(names.ALPHABET)


def test_a_drawn_name_is_a_legal_client_name():
    """Offered by the panel's own form, so it must pass the rule that form obeys."""
    for _ in range(200):
        assert store.NAME_RE.match(names.random_name())


def test_names_do_not_repeat_across_calls():
    """Not a randomness test: the draw must not be a constant or a counter."""
    assert len({names.random_name() for _ in range(100)}) == 100


def test_unique_name_keeps_drawing_until_the_answer_is_free():
    seen: list[str] = []

    def taken(candidate: str) -> bool:
        seen.append(candidate)
        return len(seen) < 3

    name = names.unique_name(taken)

    assert name == seen[-1]
    assert len(seen) == 3


def test_unique_name_gives_up_rather_than_spinning():
    """Every draw taken means something is wrong with the question, not the space."""
    attempts = 0

    def taken(candidate: str) -> bool:
        nonlocal attempts
        attempts += 1
        return True

    assert names.unique_name(taken) is None
    assert attempts == names.ATTEMPTS
