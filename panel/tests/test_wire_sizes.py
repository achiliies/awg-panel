"""What the panel's generator and validator put on the wire, in bytes.

`MTU_BUDGET` is the one number in validate.py that nothing else in the suite
can check. Every other bound is a rule about a config - a range that must not
overlap, a floor a nonce needs - and the tests read like the rule. This one is
a claim about a packet, and the only way to check a claim about a packet is to
build the packet. tests/wire.py does that, from the module install.sh pins, and
this file measures the panel against it.

The distinction that matters here is between two different things a test can
say. That a full-size datagram fits a 1500-byte link is arithmetic: the link is
the widest untunnelled path that exists, the module's own `dev->mtu` reserves
`max(sizeof(ipv6hdr), sizeof(iphdr))` for exactly this (device.c:301), and a
profile that does not fit has nowhere left to go. That is asserted. How much
margin to leave *under* 1500 - for the PPPoE link most home clients are behind,
for a carrier that tunnels, for a VPS with something in front of it - is a
decision about who this software is for, and there is no arithmetic that
settles it. That is reported, in test_the_margin_under_a_1500_byte_link, and
left to whoever picks the default.

The failure both are aimed at has no symptom when it is made. AmneziaWG clears
DF on the outer datagram (socket.c:85, `skb->ignore_df = 1` with a `df`
argument of 0), so an oversized packet is not dropped with an ICMP that
something could act on - it is fragmented on the path and arrives if its
fragments do. Nothing in the panel, in the tools, or in any log connects that
to a number typed into a form, and what the operator is told is that downloads
are slow.
"""

import sys
from pathlib import Path

import pytest

from awg import validate

# tests/wire.py lives beside the shell generator's checker rather than in the
# panel, because both halves of the suite measure against it and a model kept
# in one of the two implementations it audits is not independent of it. The
# same reach in the other direction is already how tests/obfs.sh imports
# awg.validate.
sys.path.append(str(Path(__file__).resolve().parents[2] / "tests"))

import wire  # noqa: E402  (needs the path above first)

# Enough draws that a rare combination shows up, and few enough that the file
# stays inside the second the rest of the suite budgets per test.
ROUNDS = 200

# The obfuscation bands, which is the dict randomize() looks the name up in.
# Not ADVANCED_PROFILES, whose four keys are the same four strings: the two are
# separate dicts and randomize() falls back to standard on a name it does not
# know, so reading the wrong one is a sweep that measures standard four times
# and says nothing about it.
PROFILE_KEYS = tuple(validate.PROFILES)

# What a client is handed if nobody touches anything: the installer's default
# and the panel's agree, and both are what the ParamSpec offers.
DEFAULT_MTU = validate.DEFAULT_MTU

# The MTUs worth sweeping, taken from the panel's own bounds rather than typed:
# the floor, the default, the ceiling, and one in between. A list of literals
# would keep testing 1420 on the day the ceiling moved off it, which is the one
# value nobody would then be checking.
MTUS = (validate.PARAMS["MTU"].min, 1360, DEFAULT_MTU, validate.PARAMS["MTU"].max)

# What a current AmneziaWG install reports, so the feature gate stays out of the
# way of the arithmetic under test here. Mirrors ALL_FEATURES in test_validate.py.
ALL_FEATURES = {
    "header_ranges": True,
    "imitation_packets": True,
    "header_protection_key": True,
    "content_padding": True,
    "timers": True,
    "random_trailers": True,
    "disable_cookies": True,
}


def profile_of(values: dict[str, str], mtu: int) -> dict[str, int]:
    """A drawn profile as wire.py wants it: integers, and the trailer as a flag."""
    out = {key: int(values[key]) for key in ("S1", "S2", "S3", "S4", "Jmax")}
    out["MTU"] = mtu
    out["RandomTrailers"] = 1 if values.get("RandomTrailers") == "on" else 0
    return out


def draw(mtu: int, profile: str = validate.DEFAULT_PROFILE) -> dict[str, int]:
    """One server's whole draw, measured the way that server would be.

    Both halves of it, because RandomTrailers is what decides how large a
    handshake-time packet gets and it is not in randomize()'s half - it comes
    from randomize_advanced(), which is the other button and always sets it.
    Reading only the first leaves the flag off in every round, and a burst
    measured with the trailer off is a burst four hundred bytes clear of the
    bound it is supposed to be pressed against.
    """
    values = {
        **validate.randomize(mtu=mtu, profile=profile),
        **validate.randomize_advanced(profile=profile),
    }
    return profile_of(values, mtu)


# ------------------------------------------------------- the drawn profile


@pytest.mark.parametrize("mtu", MTUS)
def test_every_draw_fits_a_1500_byte_link(mtu):
    """The widest untunnelled path there is, so a profile that misses it misses
    everything. Both families: install.sh takes a hostname for --endpoint and a
    client resolving it to an AAAA pays twenty bytes the config never mentions."""
    for _ in range(ROUNDS):
        drawn = draw(mtu)
        spare = wire.headroom(drawn, wire.ETHERNET)
        assert spare >= 0, wire.explain(drawn, wire.ETHERNET)


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_every_profile_fits_a_1500_byte_link(profile):
    """Picking a stronger profile buys more padding, and padding is the thing
    charged against the MTU. If any band can draw past the link, it is the
    thorough one, and an operator has no way to tell that they did."""
    for mtu in MTUS:
        for _ in range(ROUNDS):
            drawn = draw(mtu, profile)
            assert wire.headroom(drawn, wire.ETHERNET) >= 0, (
                f"{profile}: {wire.explain(drawn, wire.ETHERNET)}"
            )


def test_the_default_a_new_server_gets_fits():
    """The draw nobody chose, which is the one almost every server runs. Worth
    its own test rather than a case in the parametrised one above: this is the
    combination that ships, and a failure here is every install rather than a
    corner of the band."""
    for _ in range(ROUNDS):
        drawn = draw(DEFAULT_MTU)
        assert wire.headroom(drawn, wire.ETHERNET) >= 0, wire.explain(drawn, wire.ETHERNET)


def test_the_largest_mtu_the_panel_accepts_fits():
    """The ParamSpec's ceiling is a promise that everything up to it works.

    S4 is drawn against what the MTU leaves, so the tightest case is not the
    largest MTU or the largest S4 but the pair at the top of the band together -
    which is what the ceiling permits and therefore what somebody will save."""
    spec = validate.PARAMS["MTU"]
    drawn = {
        "MTU": spec.max,
        "S4": validate.MTU_BUDGET - spec.max,
        "S1": 320,
        "S2": 320,
        "S3": 320,
        "Jmax": 320,
        "RandomTrailers": 1,
    }
    assert wire.headroom(drawn, wire.ETHERNET) >= 0, wire.explain(drawn, wire.ETHERNET)


# ------------------------------------------------------------- the validator


def test_a_combination_that_fragments_is_refused():
    """The validator owns the rule, so it has to hold the line the arithmetic
    draws - not a number that happens to sit near it. Anything the packet layout
    says does not fit a 1500-byte link has to come back as an error on a field.

    Both halves of the pair are taken from the panel's own bounds rather than
    written down: the largest MTU the ParamSpec accepts, and the largest S4 the
    budget then leaves. A test that picked its own numbers could pick a pair the
    validator already refuses for an unrelated reason - an MTU over the ceiling
    is rejected on the ceiling, and the check would pass having measured
    nothing."""
    spec = validate.PARAMS["MTU"]
    mtu, s4 = spec.max, validate.MTU_BUDGET - spec.max
    drawn = {"MTU": mtu, "S4": s4, "S1": 24, "S2": 24, "S3": 24, "Jmax": 64, "RandomTrailers": 1}

    accepted = validate.validate_params({"MTU": str(mtu), "S4": str(s4)}, features=ALL_FEATURES)
    assert accepted == {}, "the pair under test has to be one the panel would save"
    assert wire.headroom(drawn, wire.ETHERNET) >= 0, (
        f"the validator accepted a config that fragments: {wire.explain(drawn, wire.ETHERNET)}"
    )

    # And the other side of that line, which is the half the name is about. One
    # more byte of S4 is a datagram the layout says will not cross, and it has
    # to come back as an error rather than be saved - on both fields, because
    # the MTU is on the Server page and S4 is on Obfuscation, and an error only
    # on the one the operator cannot see is one they cannot act on.
    over = {**drawn, "S4": s4 + 1}
    assert wire.headroom(over, wire.ETHERNET) < 0, (
        "the pair meant to overrun does not, so the refusal below proves nothing"
    )
    refused = validate.validate_params({"MTU": str(mtu), "S4": str(s4 + 1)}, features=ALL_FEATURES)
    assert "MTU" in refused and "S4" in refused, (
        f"the layout says this fragments and the validator saved it: "
        f"{wire.explain(over, wire.ETHERNET)}"
    )


def test_the_budget_the_validator_enforces_matches_the_packet():
    """MTU_BUDGET is the largest MTU that leaves room for no S4 at all, so it is
    the packet layout with S4 set to zero and nothing else. Asserting it against
    the layout is what makes it a derived number rather than a constant two
    files happen to share."""
    drawn = {
        "MTU": validate.MTU_BUDGET,
        "S4": 0,
        "S1": 24,
        "S2": 24,
        "S3": 24,
        "Jmax": 64,
        "RandomTrailers": 1,
    }
    assert wire.headroom(drawn, wire.ETHERNET) == 0, wire.explain(drawn, wire.ETHERNET)

    # Exactly zero, and one byte more is negative. `>= 0` alone would hold for
    # any budget at or under the real one, so a constant that had drifted
    # downwards would leave every test here green while every profile quietly
    # gave up padding it was entitled to. The budget is a maximum, and only the
    # pair of assertions says so.
    assert wire.headroom({**drawn, "MTU": validate.MTU_BUDGET + 1}, wire.ETHERNET) < 0


# ------------------------------------------- what the handshake burst costs


def test_the_handshake_burst_stays_inside_the_data_packet():
    """With RandomTrailers on, the initiation and the response are padded to a
    random length inside a window derived from the largest data packet the peer
    has sent (peer.h:98, send.c:243). So the burst costs nothing the data
    packets have not already cost, which is the whole argument for leaving the
    switch on without a budget of its own.

    The junk and the decoys used to be drawn into that window too, and since
    v3.1.20260906 they are not - they go out at the size the config gives them,
    which is smaller. That makes this bound looser than it was and leaves the
    assertion measuring the same thing: what is being checked is that no
    handshake-time packet outgrows a data packet, whichever of them is the
    largest.

    It is asserted rather than assumed because the two ends of it are set on
    different pages: the window comes from the MTU and S4, the burst from S1-S3
    and Jmax, and nothing in the panel shows them together."""
    for _ in range(ROUNDS):
        drawn = draw(DEFAULT_MTU)
        assert drawn["RandomTrailers"], "the switch this test is about was not drawn"
        assert wire.handshake_burst(drawn) <= wire.data_packet(DEFAULT_MTU, drawn["S4"]), (
            "a handshake-time packet is larger than a full data packet: "
            f"S1={drawn['S1']} S2={drawn['S2']} S3={drawn['S3']} Jmax={drawn['Jmax']}"
        )


def test_a_burst_can_overrun_when_the_trailer_is_off():
    """The other direction, so the test above is known to be measuring something.

    Without the trailer the handshake packets are their own size - padding plus
    body - and nothing relates them to the MTU at all. S1 is bounded at 320 and
    an initiation is 148 bytes, so it takes a hand-typed S1 to get there, which
    is exactly the config the bound on S1 exists to prevent."""
    drawn = {
        "MTU": 1400,
        "S4": 0,
        "S1": 9000,
        "S2": 24,
        "S3": 24,
        "Jmax": 64,
        "RandomTrailers": 0,
    }
    assert wire.handshake_burst(drawn) > wire.data_packet(1400, 0)


# ------------------------------------------------------------- the margin


def test_the_margin_under_a_1500_byte_link():
    """Reported, not enforced. See the module docstring: a 1500-byte link is
    arithmetic and everything below it is a product decision.

    PPPoE is the case worth printing because it is not exotic - it is DSL and
    VDSL, it is 1492, and a client behind one has no way to tell the server so.
    A negative number here is not a failing test; it is the reason a config that
    passes everything above can still stall on a real connection."""
    tightest = min(wire.headroom(draw(DEFAULT_MTU), wire.PPPOE) for _ in range(ROUNDS))
    print(f"\n  MTU {DEFAULT_MTU}: {tightest} byte(s) to spare on {wire.PPPOE.name}")

    # The assertion is on the model rather than on the margin, because the
    # margin is the thing this test declines to have an opinion about. What has
    # to hold is that the two are related the way the arithmetic says: a byte
    # off the MTU is a byte back, one for one, so whoever reads the number above
    # can act on it without re-deriving anything.
    drawn = {"MTU": DEFAULT_MTU, "S4": 20, "S1": 24, "S2": 24, "S3": 24, "Jmax": 64}
    drawn["RandomTrailers"] = 1
    lowered = {**drawn, "MTU": DEFAULT_MTU - 40}
    assert wire.headroom(lowered, wire.PPPOE) == wire.headroom(drawn, wire.PPPOE) + 40
