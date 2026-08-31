"""Rules and help text for the obfuscation parameters.

Every failure here is a configuration that would come up clean and then fail
handshakes with nothing in any log: junk bounds the wrong way round, two header
ranges that overlap, an imitation packet the Amnezia app refuses to import. That
is the whole reason validate.py exists, so these tests check the messages as
well as the verdicts - an admin who cannot tell which field is wrong is no
better off than one who got no error at all.

Self-contained: nothing here touches the filesystem, so no fixtures are needed.
"""

import pytest

from awg import validate

# What the mock controller reports and what a current AmneziaWG install has.
# Passing the full set keeps the feature check out of the way of the rules
# under test; the "unsupported by features" path has tests of its own.
ALL_FEATURES = {
    "header_ranges": True,
    "imitation_packets": True,
    "header_protection_key": True,
    "content_padding": True,
    "timers": True,
    "random_trailers": True,
    "disable_cookies": True,
}

# The MTUs worth sweeping, read off the panel's own bounds. Literals here went
# on testing 1420 after the ceiling moved below it, which is both a value nobody
# can save and the one value the sweep then stopped covering.
MTUS = (validate.PARAMS["MTU"].min, 1360, validate.DEFAULT_MTU, validate.PARAMS["MTU"].max)

# A well-formed 32-byte base64 key. Not a real one and never used to connect.
SAMPLE_KEY = "SMHDaqUpBCB0LJvMCyWkm0aCzWTOMNM3ORNqRTaHY2Y="

# Four adjacent ranges, the layout install.sh generates.
GOOD_HEADERS = {
    "H1": "5-500000000",
    "H2": "500000001-1000000000",
    "H3": "1000000001-1500000000",
    "H4": "1500000001-2000000000",
}


def check(values: dict[str, str], features: dict | None = None) -> dict[str, str]:
    return validate.validate_params(values, features=features or ALL_FEATURES)


# ------------------------------------------------------------------ junk


def test_jmin_equal_to_jmax_is_rejected_with_a_readable_message():
    errors = check({"Jmin": "70", "Jmax": "70"})

    assert "Jmax" in errors
    message = errors["Jmax"]
    # The message has to name both bounds and their current values, or the
    # admin has to go and look up which of the two fields to move.
    assert "Jmin" in message and "Jmax" in message
    assert "70" in message
    assert message.endswith(".")
    assert len(message.split()) > 6


def test_jmin_greater_than_jmax_is_rejected():
    assert "Jmax" in check({"Jmin": "200", "Jmax": "40"})


def test_jmin_below_jmax_is_accepted():
    assert check({"Jc": "4", "Jmin": "40", "Jmax": "70"}) == {}


def test_junk_bounds_switched_off_do_not_trigger_the_cross_check():
    """0 means "off" in every bash tool here, so it is not a size to compare."""
    assert check({"Jmin": "0", "Jmax": "0"}) == {}


# ----------------------------------------------------------------- headers


def test_overlapping_header_ranges_are_rejected():
    values = {
        "H1": "5-500000000",
        "H2": "400000000-900000000",  # runs back into H1
        "H3": "1000000001-1500000000",
        "H4": "1500000001-2000000000",
    }
    errors = check(values)

    assert "H2" in errors
    assert "H1" in errors["H2"]
    assert "overlap" in errors["H2"].lower()


def test_header_range_touching_the_previous_one_by_a_single_value_is_rejected():
    """Inclusive bounds: sharing one value is enough to make two types ambiguous."""
    values = {**GOOD_HEADERS, "H2": "500000000-1000000000"}
    assert "H2" in check(values)


def test_adjacent_header_ranges_are_accepted():
    assert check(GOOD_HEADERS) == {}


def test_single_integer_headers_are_accepted():
    """AmneziaWG 3.0 takes a bare number as well as a range."""
    assert check({"H1": "17", "H2": "4242", "H3": "999999", "H4": "1234567"}) == {}


def test_wireguard_default_headers_are_valid_if_unwise():
    """The compatible profile ships 1-4; it is legal, and warnings_for says so."""
    values = {"H1": "1", "H2": "2", "H3": "3", "H4": "4"}
    assert check(values) == {}
    assert any("H1-H4" in text for text in validate.warnings_for(values))


def test_two_identical_single_value_headers_are_rejected():
    values = {"H1": "7", "H2": "7", "H3": "8", "H4": "9"}
    assert "H2" in check(values)


def test_backwards_header_range_is_rejected():
    errors = check({**GOOD_HEADERS, "H1": "500-5"})
    assert "H1" in errors
    assert "backwards" in errors["H1"].lower()


def test_setting_only_some_headers_is_rejected():
    """One packet type left on WireGuard's constant gives the whole flow away."""
    errors = check({"H1": "5-500000000", "H2": "", "H3": "", "H4": ""})
    assert set(errors) == {"H2", "H3", "H4"}


def test_header_range_needs_a_module_that_supports_ranges():
    features = {**ALL_FEATURES, "header_ranges": False}
    errors = check(GOOD_HEADERS, features)
    assert "H1" in errors
    assert "single number" in errors["H1"]
    # A bare integer still works on an old module, so it must not be rejected.
    assert check({"H1": "17", "H2": "18", "H3": "19", "H4": "20"}, features) == {}


# --------------------------------------------------------------- imitation


@pytest.mark.parametrize(
    "value",
    [
        "<r>",  # no byte count
        "<r 0>",  # zero bytes is not a packet
        "<r abc>",
        "<b 0xZZ>",  # not hex
        "<b 0x123>",  # odd number of hex digits
        "<b deadbeef>",  # missing the 0x
        "<q 4>",  # no such tag
        "<r 2> stray text <b 0x00>",
        "plain bytes with no tags at all",
        "<r 2",  # unterminated
    ],
)
def test_malformed_imitation_tags_are_rejected(value):
    errors = check({"I1": value})
    assert "I1" in errors
    assert errors["I1"]


def test_well_formed_imitation_packet_is_accepted():
    assert check({"I4": "<b 0x000100002112a442><r 12>"}) == {}


def test_oversized_imitation_packet_is_rejected():
    """A decoy that needs fragmenting is a signature of its own."""
    errors = check({"I1": "<r 1500>"})
    assert "I1" in errors


def test_t_tag_is_importer_unsafe_rather_than_fatal():
    """The kernel accepts <t>; the Amnezia app refuses the whole file over it.

    So it cannot be a validation error - a hand-configured pair of peers may
    legitimately use it - but the admin has to be told, loudly, before they hand
    the config to somebody with a phone.
    """
    values = {"I1": "<t><r 4>"}

    assert check(values) == {}

    warnings = validate.warnings_for(values)
    hostile = [text for text in warnings if "<t>" in text]
    assert hostile, warnings
    assert "1000" in hostile[0]
    assert "I1" in hostile[0]


@pytest.mark.parametrize("tag", ["<t>", "<c>", "<rc>", "<rd>"])
def test_every_app_hostile_tag_is_warned_about(tag):
    values = {"I2": tag + "<r 8>"}
    assert check(values) == {}
    assert any("1000" in text for text in validate.warnings_for(values))


def test_imitation_needs_a_module_that_supports_it():
    features = {**ALL_FEATURES, "imitation_packets": False}
    errors = check({"I4": "<b 0x000100002112a442><r 12>"}, features)
    assert "I4" in errors
    assert "imitation packets" in errors["I4"]


# ------------------------------------------------------------------ ports


@pytest.mark.parametrize("port", ["1", "53", "443", "1024", "51820", "65535"])
def test_any_real_port_is_accepted_including_the_privileged_ones(port):
    """443/udp is the port that gets through a filtered network.

    awg-quick runs as root, so a low port binds like any other; refusing them
    only means the one place the tunnel can survive is the one place the panel
    will not let you put it.
    """
    assert check({"ListenPort": port}) == {}


@pytest.mark.parametrize("port", ["0", "65536", "-1", "https"])
def test_a_port_outside_the_range_is_still_rejected(port):
    assert "ListenPort" in check({"ListenPort": port})


# --------------------------------------------------------- the address line


def test_a_dual_stack_address_is_accepted():
    """What install.sh writes on a server with IPv6, read back as one line.

    Refusing it was not a wrong error message about the address: a save is
    checked against the whole merged configuration, so it blocked every save
    the panel could make - obfuscation included - on a field nobody had
    touched.
    """
    assert check({"Address": "10.13.0.1/20, 2a0a:8dc0:70b5:1::1/64"}) == {}


def test_a_dual_stack_address_is_accepted_without_the_space():
    """A hand-edited config need not be spaced the way the installer spaces it."""
    assert check({"Address": "10.13.0.1/20,fd7a:1e5f:22::1/64"}) == {}


def test_an_address_with_no_ipv4_half_is_rejected():
    """The allocator hands out v4 addresses and derives the v6 ones from them."""
    errors = check({"Address": "fd7a:1e5f:22::1/64"})

    assert "IPv4" in errors["Address"]


@pytest.mark.parametrize(
    "address",
    [
        "10.13.0.1/20, fd7a:1e5f:22::1/48",  # a block, not a link
        "10.13.0.1/20, fe80::1/64",  # link-local: routes nothing
        "10.13.0.1/20, 2001:db8::1/64",  # the documentation prefix
    ],
)
def test_an_ipv6_half_the_tunnel_cannot_use_is_rejected(address):
    """The same rules awg/subnet6.py reads it back with, asked before it is written."""
    assert "Address" in check({"Address": address})


def test_the_server_has_to_be_first_in_its_ipv6_prefix_too():
    """A client's v6 address is its offset in the v4 pool, so ::5 is the fifth client's."""
    errors = check({"Address": "10.13.0.1/20, fd7a:1e5f:22::5/64"})

    assert "fd7a:1e5f:22::1/64" in errors["Address"]


def test_a_second_address_of_one_family_is_rejected_rather_than_ignored():
    """Only the first entry of a family is ever read; a silent spare is a lie."""
    errors = check({"Address": "10.13.0.1/20, 10.14.0.1/20"})

    assert "10.14.0.1/20" in errors["Address"]


# ---------------------------------------------------------------- warnings


def test_a_header_protection_key_is_not_warned_about_on_its_own():
    """The advisory that used to fire here listed the whole AmneziaWG 3.0 group
    on every save that set any of it, and told the operator to clear it unless
    every peer was new enough. The clients caught up and the generator now fills
    that group in as a matter of course, so the sentence was firing on its own
    output and then standing in server/status for the life of the server. Which
    release a parameter needs is a fact about the parameter, and it stays in the
    catalog where it is read once."""
    padded = {key: "32" for key in validate.HEADER_PROTECTED}
    assert validate.warnings_for({**padded, "HeaderProtectionKey": SAMPLE_KEY}) == []


def test_header_protection_key_is_still_valid_input():
    padded = {key: "32" for key in validate.HEADER_PROTECTED}
    assert check({**padded, "HeaderProtectionKey": SAMPLE_KEY}) == {}
    assert check({**padded, "HeaderProtectionKey": "0" * 64}) == {}
    assert "HeaderProtectionKey" in check({"HeaderProtectionKey": "not-a-key"})


def test_a_whole_generated_advanced_group_is_warned_about_at_all():
    """The set the button draws has to arrive with nothing to say for itself.
    Anything else is a generator arguing with the page it just filled in."""
    padded = {key: "32" for key in validate.HEADER_PROTECTED}
    assert validate.warnings_for({**padded, **validate.randomize_advanced()}) == []


def test_s4_larger_than_the_mtu_headroom_warns():
    warnings = validate.warnings_for({"S4": "80", "MTU": "1420"})
    assert any("S4" in text and "1420" in text for text in warnings)


# ------------------------------------------------------- PostUp vs PostDown

# What install.sh writes, and what _ensure_hooks puts back: three firewall rules
# with their three undos, and the panel's own enforce hook above them.
FIREWALL_UP = (
    "iptables -t nat -A POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE\n"
    "iptables -A FORWARD -i %i -j ACCEPT\n"
    "iptables -A FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
)
FIREWALL_DOWN = (
    "iptables -t nat -D POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE\n"
    "iptables -D FORWARD -i %i -j ACCEPT\n"
    "iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
)


def test_the_panels_own_posthook_does_not_count_against_postdown():
    """Every server carries it, so counting it warned every server about nothing.

    The enforce hook adds no firewall rule, so it has nothing for a PostDown to
    undo. Counted as one, a stock config reads as four PostUp against three
    PostDown, and the settings page tells the admin their rules are unbalanced -
    about a line the panel wrote itself and puts back if they delete it.
    """
    values = {
        "PostUp": f"{validate.POSTUP_HOOK}\n{FIREWALL_UP}",
        "PostDown": FIREWALL_DOWN,
    }
    assert not [text for text in validate.warnings_for(values) if "PostDown" in text]


def test_a_genuinely_unbalanced_firewall_still_warns():
    """The check still has to do its job: the hook is excluded, not the count."""
    values = {
        "PostUp": f"{validate.POSTUP_HOOK}\n{FIREWALL_UP}",
        "PostDown": "iptables -t nat -D POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE",
    }
    warnings = [text for text in validate.warnings_for(values) if "PostDown" in text]
    assert warnings, "an unbalanced pair must still be reported"
    assert "3 rule(s)" in warnings[0] and "1" in warnings[0]


# ------------------------------------------------------- per-packet padding


def test_padding_that_does_not_fit_the_mtu_is_refused():
    """The one overrun with no symptom at the moment it is made: the tunnel comes
    up, small requests work, and only full-size packets vanish."""
    errors = check({"MTU": "1400", "S4": "80"})
    assert "S4" in errors
    assert "1400" in errors["S4"] and "80" in errors["S4"]


def test_content_padding_is_not_charged_to_the_mtu():
    """It rides on every data packet as S4 does, and that is where the
    resemblance stops. S4 is pushed onto the front of a finished packet and
    nothing bounds it but this check; content padding goes inside the encrypted
    payload, and the sender clamps each packet's share of it to what that packet
    leaves below the MTU - so it cannot make a full-size packet any larger, and
    charging it here would only refuse configurations that work."""
    assert check({"MTU": "1400", "ContentPaddingAddition": "512"}) == {}
    assert check({"MTU": "1400", "S4": "20", "ContentPaddingAddition": "0-900"}) == {}


def test_an_overrun_is_reported_against_every_field_that_can_fix_it():
    """The MTU and S4 are edited on two different pages, so an error left only
    on the field the operator cannot see is one they cannot act on."""
    errors = check({"MTU": "1400", "S4": "60"})
    assert set(errors) == {"MTU", "S4"}


def test_padding_exactly_on_the_budget_is_accepted():
    """A bound that rejects the value it tells you to use is a bound nobody
    can satisfy. Read off MTU_BUDGET rather than written down, so it stays on
    the budget when the budget moves."""
    assert check({"MTU": "1400", "S4": str(validate.MTU_BUDGET - 1400)}) == {}


def test_lowering_the_mtu_makes_the_same_padding_fit():
    over = {"MTU": "1400", "S4": "120"}
    assert check(over)
    assert check({**over, "MTU": str(validate.MTU_BUDGET - 120)}) == {}


def test_padding_switched_off_never_overruns():
    """0 means off in every tool here, so it is not a byte to charge for."""
    assert check({"MTU": str(validate.PARAMS["MTU"].max), "S4": "0"}) == {}


def test_padding_without_an_mtu_is_left_alone():
    """Callers may hand over a partial set; every other cross-field rule waits
    for both sides too."""
    assert check({"S4": "400"}) == {}


def test_an_overrun_written_by_hand_is_reported_on_the_status_page():
    """The panel refuses to save one, so reaching it means awg0.conf was edited
    by something else - and the symptom is transfers hanging, not a tunnel that
    will not start, so nobody finds it without being told."""
    warnings = validate.warnings_for({"MTU": "1400", "S4": "60"})
    assert any("data packet" in text for text in warnings), warnings


# --------------------------------------------- the header protection floor


def _protected(**overrides: str) -> dict[str, str]:
    """A key with all four padding sizes above the floor, before overrides."""
    return {key: "32" for key in validate.HEADER_PROTECTED} | {
        "HeaderProtectionKey": SAMPLE_KEY,
        **overrides,
    }


@pytest.mark.parametrize("key", validate.HEADER_PROTECTED)
def test_a_key_with_padding_under_the_nonce_is_refused(key):
    """The kernel reads the nonce it decrypts the header with off the front of
    whichever prefix the packet carries, so all four have to be able to hold
    one - and it refuses the entire device configuration when one cannot, not
    the single parameter. An obfuscation save restarts the interface, so this
    combination is a tunnel that goes down and does not come back."""
    errors = check(_protected(**{key: str(validate.HEADER_NONCE - 1)}))
    assert key in errors and "HeaderProtectionKey" in errors
    assert str(validate.HEADER_NONCE) in errors[key]


def test_exactly_the_nonce_length_is_enough():
    """The kernel's test is `< 12`, so 12 is the smallest that works and a floor
    that refused it would be a floor the generator itself could not meet."""
    assert check(_protected(S4=str(validate.HEADER_NONCE))) == {}


def test_padding_the_config_omits_counts_as_too_short():
    """A parameter that is not written out is one the kernel sees as zero. This
    is the case that actually happens: an install whose S4 was drawn before the
    floor existed, meeting a freshly generated key."""
    values = _protected()
    del values["S4"]
    assert "HeaderProtectionKey" in check(values)


def test_padding_below_the_nonce_is_fine_with_no_key():
    """Without a key the prefix is junk nobody reads, so the sizes are free."""
    assert check({key: "4" for key in validate.HEADER_PROTECTED}) == {}


def test_a_short_prefix_written_by_hand_is_reported_on_the_status_page():
    """The panel will not save one, and the symptom - an interface that stopped
    coming up at some restart - points nowhere near the cause."""
    warnings = validate.warnings_for(_protected(S2="8"))
    assert any("S2" in text and "header protection" in text for text in warnings), warnings


# --------------------------------------------------------------- randomize


# 40 rounds rather than one: every assertion below is about a distribution, and
# a generator that is wrong one time in ten passes a single-draw test happily.
ROUNDS = 40


def test_randomize_output_passes_validation():
    for _ in range(ROUNDS):
        assert check(validate.randomize()) == {}


def test_randomize_output_survives_a_second_validation_pass():
    """Re-validating a saved profile must not start failing on a cross-field rule."""
    for _ in range(ROUNDS):
        values = validate.randomize(mtu=1400)
        assert check({**values, "MTU": "1400", "ListenPort": "41234"}) == {}


def test_randomize_raises_no_advisories():
    """Reconfigure is one click, so it must never hand back a set the save bar
    then complains about - an unimportable tag, or S4 over the MTU headroom."""
    for _ in range(ROUNDS):
        values = validate.randomize(mtu=1400)
        assert validate.warnings_for({**values, "MTU": "1400"}) == []


def test_randomize_shares_nothing_between_servers():
    """The whole point. Two servers that agree on any of these agree on a
    signature, so every group has to come out different."""
    draws = [validate.randomize() for _ in range(ROUNDS)]
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1"):
        assert len({draw[key] for draw in draws}) > 1, f"{key} is the same on every server"


def test_randomize_keeps_s4_inside_the_mtu_headroom():
    """S4 rides on every data packet, so a draw that does not fit silently
    fragments full-size packets rather than failing loudly."""
    for mtu in MTUS:
        for _ in range(ROUNDS):
            s4 = int(validate.randomize(mtu=mtu)["S4"])
            assert s4 <= validate.MTU_BUDGET - mtu, f"S4 {s4} does not fit an MTU of {mtu}"


@pytest.mark.parametrize("mtu", [str(validate.PARAMS["MTU"].max), validate.PARAMS["MTU"].max])
def test_randomize_takes_the_mtu_as_text_or_a_number(mtu):
    """Config values arrive as text about as often as they arrive as integers,
    and the string form used to reach the arithmetic and raise TypeError.

    Taken at the panel's ceiling and asserted exactly, because that is the one
    MTU with a single answer: it leaves HEADER_NONCE, and every profile's band
    is clamped onto that. An upper bound here would be met by the default-MTU
    fallback as well - and a string thrown away rather than raised on is the
    other half of what this is watching for, not something apart from it."""
    room = validate.MTU_BUDGET - validate.PARAMS["MTU"].max
    for _ in range(ROUNDS):
        assert int(validate.randomize(mtu=mtu)["S4"]) == room


@pytest.mark.parametrize("mtu", [None, 0, "", "not a number"])
def test_randomize_falls_back_to_the_default_mtu(mtu):
    """An unreadable MTU is not a reason to refuse; the default leaves the most
    room, so falling back to it is the conservative direction."""
    for _ in range(ROUNDS):
        assert 8 <= int(validate.randomize(mtu=mtu)["S4"]) <= 40


def test_randomize_header_ranges_do_not_overlap_and_stay_narrow():
    for _ in range(ROUNDS):
        values = validate.randomize()
        bounds = sorted(
            tuple(int(part) for part in values[f"H{n}"].split("-")) for n in range(1, 5)
        )
        for (_low, high), (next_low, _next_high) in zip(bounds, bounds[1:], strict=False):
            assert high < next_low, bounds
        assert bounds[0][0] >= validate.H_FLOOR
        assert bounds[-1][1] <= validate.H_MAX
        # Narrow, and that is the point rather than a compromise. RandomTrailers
        # makes the kernel accept a handshake by minimum length, so the width of
        # these ranges is the rate at which data packets are misfiled as
        # handshakes and dropped. Wide ranges cost about a quarter of every data
        # packet per direction; this ceiling holds the same figure under three in
        # a million. The entropy lives in where the range sits, asserted below.
        for low, high in bounds:
            assert validate.H_WIDTH_LO <= high - low + 1 <= validate.H_WIDTH_HI, (low, high)


def test_randomize_header_ranges_are_placed_across_the_whole_quarter():
    """Narrowing the width moved the entropy into the start, so that is what has
    to be spread. Each range must land somewhere different every draw, across
    most of its quarter, or a narrow range becomes a constant per install rather
    than per server.

    On the offset inside the quarter rather than on the start, because the two
    are not the same assertion. The shuffle hands H1 a random quarter, and the
    four quarters are a slot apart, so raw starts are spread that far by the
    shuffle alone - they clear any span worth asserting while the placement
    inside the quarter is a constant. Taking the start modulo the slot removes
    the quarter and leaves only the draw this is here to watch.
    """
    slot = (validate.H_MAX - validate.H_FLOOR + 1) // 4
    starts = [int(validate.randomize()["H1"].split("-")[0]) for _ in range(ROUNDS)]
    offsets = [(start - validate.H_FLOOR) % slot for start in starts]
    assert max(offsets) - min(offsets) > slot // 2, offsets


def test_wide_header_ranges_warn_only_while_random_trailers_is_on():
    """Either setting alone is fine; together they drop a share of every data
    packet equal to the share of the header space H1-H3 cover."""
    wide = {**GOOD_HEADERS, "H1": "5-500000000"}
    assert not [t for t in validate.warnings_for(wide) if "misfiled" in t]

    on = validate.warnings_for({**wide, "RandomTrailers": "on"})
    assert [t for t in on if "H1" in t and "misfiled" in t]

    drawn = {**validate.randomize(), "RandomTrailers": "on"}
    assert not [t for t in validate.warnings_for(drawn) if "misfiled" in t]


def test_the_wide_header_warning_reports_a_rate_a_reader_can_act_on():
    """The threshold sits far below where the loss shows up as a percentage, so
    a percentage would read "about 0.0%" over most of the band this warns on -
    and a warning about silent packet loss that reports zero loss is worse than
    no warning at all."""
    at_threshold = {
        **validate.randomize(),
        "RandomTrailers": "on",
        "H1": f"5-{validate.H_WIDTH_WARN + 5}",
    }
    text = next(t for t in validate.warnings_for(at_threshold) if "misfiled" in t)
    assert "0.0%" not in text, text
    assert "1 in " in text, text

    heavy = {**at_threshold, "H1": "5-500000005"}
    text = next(t for t in validate.warnings_for(heavy) if "misfiled" in t)
    assert "11.6% of data packets" in text, text


def test_randomize_does_not_always_hand_h1_the_lowest_range():
    """The quarters are shuffled before they are assigned. Without that, the
    range a value falls in would say which packet type it is - which is the one
    thing H1-H4 exist to hide."""
    lowest_is_h1 = 0
    for _ in range(ROUNDS):
        values = validate.randomize()
        starts = [int(values[f"H{n}"].split("-")[0]) for n in range(1, 5)]
        lowest_is_h1 += starts.index(min(starts)) == 0
    assert 0 < lowest_is_h1 < ROUNDS


def test_randomize_fills_the_imitation_slots_from_the_front():
    """A gap would leave a decoy configured that never gets sent, and the
    callers spell 'remove this line' as an empty value."""
    for _ in range(ROUNDS):
        values = validate.randomize()
        filled = [bool(values[f"I{n}"]) for n in range(1, 6)]
        assert 3 <= sum(filled) <= 5
        assert filled == sorted(filled, reverse=True), filled


def test_randomize_builds_each_decoy_set_from_one_protocol():
    """Mixing families is worse than sending nothing: no host speaks DNS, STUN
    and QUIC down one socket pair, so a set that does stands out by itself."""
    starts = {"<b 0x0001": "stun", "<b 0x0101": "stun", "<b 0x80": "stun", "<b 0xc3": "quic"}
    for _ in range(ROUNDS):
        values = validate.randomize()
        families = set()
        for n in range(1, 6):
            packet = values[f"I{n}"]
            if not packet:
                continue
            match = [family for prefix, family in starts.items() if packet.startswith(prefix)]
            families.add(match[0] if match else "quic" if packet.startswith("<b 0x") else "dns")
        assert len(families) == 1, f"mixed decoy families: {families}"


# ---------------------------------------------------------------- profiles


PROFILE_KEYS = ("standard", "dpi", "fast", "random")


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_every_profile_draws_a_set_that_validates(profile):
    """A profile changes the band, never whether the result is usable. One that
    hands back a set the save then rejects is worse than not offering it."""
    for _ in range(ROUNDS):
        values = validate.randomize(mtu=1400, profile=profile)
        assert check({**values, "MTU": "1400", "ListenPort": "41234"}) == {}


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_no_profile_raises_an_advisory(profile):
    """Same rule as the default draw: the save bar must have nothing to complain
    about, or picking the thorough profile looks like picking a broken one."""
    for _ in range(ROUNDS):
        values = validate.randomize(mtu=1400, profile=profile)
        assert validate.warnings_for({**values, "MTU": "1400"}) == []


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_every_profile_keeps_s4_inside_the_mtu_headroom(profile):
    for mtu in MTUS:
        for _ in range(ROUNDS):
            s4 = int(validate.randomize(mtu=mtu, profile=profile)["S4"])
            assert s4 <= validate.MTU_BUDGET - mtu, (
                f"{profile}: S4 {s4} does not fit an MTU of {mtu}"
            )


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_every_profile_stays_above_the_header_protection_floor(profile):
    """The cheapest profile has to be usable with the strongest setting.

    The two are drawn by different buttons, and neither can see what the other
    has been asked for, so the only way they cannot contradict each other is for
    no profile to go below the floor at all. Twelve bytes on a data packet is
    what that costs, out of a headroom that is never less than the floor
    itself: the largest MTU the panel accepts is MTU_BUDGET - HEADER_NONCE, so
    the tightest draw there is leaves exactly twelve."""
    for mtu in MTUS:
        for _ in range(ROUNDS):
            values = validate.randomize(mtu=mtu, profile=profile)
            short = validate.header_protection_short({**values, "HeaderProtectionKey": SAMPLE_KEY})
            assert short == [], f"{profile} at MTU {mtu}: {short} below the floor"


@pytest.mark.parametrize("profile", PROFILE_KEYS)
@pytest.mark.parametrize("other", PROFILE_KEYS)
def test_the_two_generators_agree_whichever_profiles_are_picked(profile, other):
    """Nothing stops an admin drawing Fast obfuscation and a DPI-resistant 3.0
    group, and the key the second writes is checked against the padding the
    first drew. Every one of the sixteen pairings has to save."""
    for _ in range(ROUNDS):
        values = {
            **validate.randomize(mtu=1400, profile=profile),
            **validate.randomize_advanced(profile=other),
            "MTU": "1400",
        }
        assert check(values) == {}


def test_the_profiles_are_ordered_by_what_they_cost():
    """Fast, standard and DPI-resistant are a scale, not three flavours: if the
    thorough one did not actually send more than the cheap one, the choice on
    the page would be telling the admin something untrue."""
    cost = {}
    for profile in ("fast", "standard", "dpi"):
        draws = [validate.randomize(mtu=1280, profile=profile) for _ in range(ROUNDS)]
        cost[profile] = sum(
            int(d["Jc"]) * int(d["Jmax"]) + sum(bool(d[f"I{n}"]) for n in range(1, 6))
            for d in draws
        )
    assert cost["fast"] < cost["standard"] < cost["dpi"]


def test_the_random_profile_spans_the_other_three():
    """It exists so that the profile itself is not a fingerprint, which only
    works if its draws reach into the bands either side of standard."""
    draws = [int(validate.randomize(profile="random")["Jc"]) for _ in range(200)]
    assert min(draws) < validate.JC_RANGE[0]
    assert max(draws) > validate.JC_RANGE[1]


def test_an_unknown_profile_falls_back_to_standard():
    """A profile is a preference. Refusing to generate because of a typo in one
    would be refusing to do the useful thing over the cosmetic one."""
    for _ in range(ROUNDS):
        assert validate.JC_RANGE[0] <= int(validate.randomize(profile="nonsense")["Jc"])
        assert int(validate.randomize(profile="nonsense")["Jc"]) <= validate.JC_RANGE[1]


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_every_profile_keeps_the_decoy_slots_packed_from_the_front(profile):
    for _ in range(ROUNDS):
        values = validate.randomize(profile=profile)
        filled = [bool(values[f"I{n}"]) for n in range(1, 6)]
        assert 1 <= sum(filled) <= 5
        assert filled == sorted(filled, reverse=True), filled


# ------------------------------------------------------- randomize_advanced


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_advanced_draws_a_set_that_validates(profile):
    """Against the obfuscation it will be saved beside, because that is what the
    save sees: the key this draws is checked against padding on the other card,
    and a set validated on its own would be a set validated against nothing."""
    for _ in range(ROUNDS):
        values = {
            **validate.randomize(mtu=1400),
            **validate.randomize_advanced(profile=profile),
            "MTU": "1400",
        }
        assert check(values) == {}


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_advanced_timers_leave_room_to_renegotiate(profile):
    """RejectAfterTime is derived rather than drawn precisely because these
    interlock, and they add up rather than competing: a peer rekeys at
    RekeyAfterTime and may then wait a keepalive and a retry before it hears
    back, so a key that expires above only the largest of the three can still go
    before the handshake replacing it lands.

    With ranges the question is asked of the worst draw rather than a typical
    one. The kernel picks a fresh value inside each range every time it arms the
    timer, so a set that holds on average is a set that stalls the tunnel on the
    unluckiest cycle in a few thousand - which is exactly the kind of fault that
    never gets traced back to here.
    """
    for _ in range(ROUNDS):
        values = validate.randomize_advanced(profile=profile)
        reject = validate._parse_range(values["RejectAfterTime"])
        rekey = validate._parse_range(values["RekeyAfterTime"])
        cycle_hi = (
            validate._parse_range(values["KeepaliveTimeout"])[1]
            + validate._parse_range(values["RekeyTimeout"])[1]
        )
        cycle_lo = (
            validate._parse_range(values["KeepaliveTimeout"])[0]
            + validate._parse_range(values["RekeyTimeout"])[0]
        )
        assert reject[0] > rekey[1] + cycle_hi, values
        # The far end measures its own key against RejectAfterTime minus that
        # wait, and a responder that reaches it first starts handshaking on top
        # of the initiator - twice the handshakes, which is the one event on the
        # wire that none of this can disguise. receive.c subtracts the *bottom*
        # of the two waits, so that is the end this is asked of.
        assert reject[0] - cycle_lo > rekey[1], values


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_advanced_timers_are_ranges_rather_than_numbers(profile):
    """The whole of what this change buys is that the kernel has something to
    draw from. u16_range_pick_one runs every time a timer is armed, so a range
    turns the handshake cadence - the one event on the wire that no amount of
    padding can disguise - from a constant into a distribution. A generator that
    quietly went back to single values would still validate, and every server
    would still differ from every other; each one would just be a metronome
    again."""
    for _ in range(ROUNDS):
        values = validate.randomize_advanced(profile=profile)
        for key in validate.TIMER_PARAMS:
            low, high = validate._parse_range(values[key])
            assert high > low, f"{key} came back as a single value: {values[key]}"


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_advanced_timer_ranges_are_not_the_band(profile):
    """A range every server shares is a signature exactly the way a value every
    server shares is one. The band is what the range is drawn *inside*, so both
    where a server's range sits and how wide it is have to vary - emitting the
    band itself would trade a per-server constant for a per-profile one."""
    drawn = {validate.randomize_advanced(profile=profile)["RekeyAfterTime"] for _ in range(200)}
    ends = [validate._parse_range(value) for value in drawn]
    assert len({low for low, _ in ends}) > 5, drawn
    assert len({high - low for low, high in ends}) > 3, drawn


# --------------------------------------------------------- timer cross-checks


def test_timers_still_accept_the_single_values_already_in_the_field():
    """Every server installed before this was a range wrote plain numbers, and
    they are still what `awg showconf` prints for a range of width zero. A
    validator that started refusing them would reject every unrelated save on
    those servers, because the merged set is what each save is checked against.
    """
    assert (
        check(
            {
                "RekeyAfterTime": "120",
                "RekeyTimeout": "5",
                "RejectAfterTime": "180",
                "KeepaliveTimeout": "10",
                "MaxHandshakeAttempts": "18",
            }
        )
        == {}
    )


def test_reject_after_time_is_measured_against_the_top_of_the_rekey_range():
    """180 is inside 120-180, so the draw that lands there expires a key at the
    same moment its replacement is due. Comparing the bottoms would pass this."""
    errors = check(
        {"RekeyAfterTime": "120-180", "RejectAfterTime": "175-400", "RekeyTimeout": "4-7"}
    )
    assert "must be longer than RekeyAfterTime" in errors["RejectAfterTime"]


def test_reject_after_time_is_measured_against_the_bottom_of_the_waits():
    """receive.c computes reject - lo(keepalive) - lo(rekey_timeout) into a
    signed int on every packet a responder decrypts. Below zero is not a stall,
    it is a responder that decides its key is stale every time it hears
    anything."""
    errors = check(
        {"RejectAfterTime": "25-40", "KeepaliveTimeout": "20-30", "RekeyTimeout": "20-30"}
    )
    assert "KeepaliveTimeout + RekeyTimeout" in errors["RejectAfterTime"]


def test_the_responder_has_to_stay_behind_the_initiator():
    """The one bound that was a comment rather than a check. It does not break
    the tunnel, which is why it survived as prose - it just doubles the one
    event on the wire that none of this can disguise."""
    errors = check(
        {
            "RekeyAfterTime": "120-180",
            "RejectAfterTime": "190-400",
            "RekeyTimeout": "4-7",
            "KeepaliveTimeout": "8-15",
        }
    )
    assert "both ends handshake every cycle" in errors["RejectAfterTime"]


def test_the_initiators_key_has_to_outlast_its_own_negotiation():
    """The bound randomize_advanced derives RejectAfterTime from, asked of a set
    typed in by hand. wg_timers_data_sent waits hi(keepalive) +
    pick_one(rekey_timeout), so a peer rekeying at the top of 120-180 can be
    202s in before it hears back - and a RejectAfterTime whose bottom is 200
    expires the key while the handshake replacing it is still in flight.

    It clears every other bound: 200 is above the top of the rekey range, above
    the waits, and above the responder's threshold, which reads the *bottoms* of
    those waits. Only this one, which reads the tops, catches it."""
    values = {
        "RekeyAfterTime": "120-180",
        "KeepaliveTimeout": "8-15",
        "RekeyTimeout": "4-7",
        "RejectAfterTime": "200-260",
    }
    errors = check(values)
    assert "expires a key mid-negotiation" in errors["RejectAfterTime"]
    # The message names what it would take, so acting on it has to be enough.
    assert check({**values, "RejectAfterTime": "203-260"}) == {}


@pytest.mark.parametrize("key", ["H1", *validate.TIMER_PARAMS])
def test_a_range_with_a_space_in_it_is_refused(key):
    """`awg setconf` reads the low end with strtoul and then insists the next
    byte is the dash, so `120 - 180` is a line it cannot parse - and it answers
    one of those by refusing the whole file, which is an interface that does not
    come up. A validator that took what the tools reject is worse than one that
    is merely strict."""
    low = validate.PARAMS[key].min
    assert key in check({key: f"{low} - {low + 1}"})
    assert check({key: f"{low}-{low + 1}"}).get(key) is None


def test_a_timer_with_an_error_of_its_own_is_left_out_of_the_arithmetic():
    """Its value is not a number anyone chose. Reporting a second failure
    derived from it buries the one the admin can act on."""
    errors = check({"RekeyAfterTime": "nonsense", "RejectAfterTime": "180"})
    assert set(errors) == {"RekeyAfterTime"}


@pytest.mark.parametrize("key", validate.TIMER_PARAMS)
def test_a_timer_range_cannot_exceed_what_the_kernel_stores(key):
    """These are u16 in the kernel, packed two to a u32, and the tools' parser
    truncates to that without checking - `RekeyAfterTime = 70000` is not
    refused, it is silently 4464. The per-parameter caps are what stops a value
    arriving on the wire that nobody chose."""
    assert validate.PARAMS[key].max <= 65535
    assert key in check({key: f"1-{validate.PARAMS[key].max + 1}"})


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_advanced_content_padding_is_a_range_or_nothing(profile):
    """A single number is worse than leaving it empty. The kernel uses content
    padding *instead of* the 16-byte rounding it does anyway, so a constant
    addition trades a length known to within 16 bytes for one known exactly;
    only a range that reaches past the rounding it displaced buys that back."""
    for _ in range(ROUNDS):
        value = validate.randomize_advanced(profile=profile)["ContentPaddingAddition"]
        if not value:
            continue
        low, _, high = value.partition("-")
        assert high, f"drawn as a bare number: {value}"
        assert int(low) < int(high), value
        assert int(high) >= validate.PADDING_MULTIPLE, value


def test_advanced_header_protection_keys_are_fresh_every_time():
    """The key is the whole point of the setting; one that repeats between
    servers is a shared secret that is not secret."""
    keys = {validate.randomize_advanced()["HeaderProtectionKey"] for _ in range(ROUNDS)}
    assert len(keys) == ROUNDS


def test_advanced_covers_the_whole_group():
    """Every field the card offers gets a value, or clearing and regenerating
    would leave a half-filled group nobody can reason about."""
    values = validate.randomize_advanced()
    assert set(values) == set(validate.ADVANCED_PARAMS)


def test_dns_name_hex_matches_the_shell_encoder():
    # 03 'com' style length-prefixed labels, terminated by a root label of 00.
    assert validate.dns_name_hex("apple.com") == "056170706c6503636f6d00"
    assert validate.dns_name_hex("a.b") == "01610162" + "00"


# ------------------------------------------------------------------ catalog


@pytest.mark.parametrize("key", sorted(validate.PARAMS))
def test_every_param_is_explained(key):
    """The UI requirement: no field is ever shown without a label and both help
    texts, so nothing in the Server Config page is left for the admin to guess."""
    spec = validate.PARAMS[key]

    assert spec.label.strip(), key
    assert spec.help_short.strip(), key
    assert spec.help_long.strip(), key
    # help_short sits under the field on one line; help_long is the popover and
    # has to actually say what breaks, which takes more than a restated label.
    assert len(spec.help_short) <= 100, key
    assert len(spec.help_long) >= 200, key
    assert spec.help_long != spec.help_short, key
    assert spec.group in {
        "network",
        "junk",
        "sizes",
        "headers",
        "imitation",
        "advanced",
        "hooks",
        "protection",
    }, key
    assert spec.kind in {
        "int",
        "range",
        "imitation",
        "key",
        "text",
        "cidr",
        "port",
        "iplist",
        "bool",
    }, key


def test_catalog_covers_every_mirrored_parameter():
    """AWG_PARAMS is what gets copied into client configs; a parameter missing
    from the catalog would be uneditable and unexplained in the panel."""
    assert set(validate.AWG_PARAMS) <= set(validate.PARAMS)
    assert set(validate.NETWORK_PARAMS) <= set(validate.PARAMS)
    assert set(validate.HOOK_PARAMS) <= set(validate.PARAMS)
    assert set(validate.SERVER_ONLY_PARAMS) <= set(validate.PARAMS)


def test_param_list_is_json_serialisable_and_ordered():
    import json

    rows = validate.param_list()
    assert [row["key"] for row in rows] == list(validate.PARAMS)
    json.dumps(rows)  # the API serves this verbatim
    for row in rows:
        assert set(row) >= {"key", "group", "label", "kind", "help_short", "help_long", "feature"}


def test_importer_unsafe_params_are_exactly_the_documented_ones():
    unsafe = {key for key, spec in validate.PARAMS.items() if not spec.importer_safe}
    assert unsafe == {
        "HeaderProtectionKey",
        "ContentPaddingAddition",
        "RekeyAfterTime",
        "RekeyTimeout",
        "RejectAfterTime",
        "KeepaliveTimeout",
        "MaxHandshakeAttempts",
        "RandomTrailers",
    }


def test_unknown_keys_are_ignored():
    """Callers hand over a whole parsed [Interface]; PrivateKey is not ours to check."""
    assert check({"PrivateKey": "whatever", "Table": "off"}) == {}


# --------------------------------------------------------- random trailers


def test_random_trailers_accepts_on():
    """AmneziaWG 3.1 appends a random-length trailer to every packet when enabled."""
    assert check({"RandomTrailers": "on"}) == {}


@pytest.mark.parametrize("value", ["off", "", "OFF", " off "])
def test_random_trailers_off_and_empty_read_as_unset(value):
    """Off or omitted means no random trailer padding is configured, so no line
    is emitted and no importer warning is raised."""
    assert validate._is_set(validate.PARAMS["RandomTrailers"], value) is False
    assert check({"RandomTrailers": value}) == {}
    assert not [
        text
        for text in validate.warnings_for({"RandomTrailers": value})
        if "RandomTrailers" in text
    ]


@pytest.mark.parametrize("value", ["yes", "true", "invalid", "enabled", "0x10", "-1"])
def test_random_trailers_rejects_what_the_tools_cannot_read(value):
    """A value parse_bool refuses is a config the interface will not come up on,
    and the page has to say so rather than skip the line.

    Not everything that is not a word, though - see below. "0x10" is here
    because it is worse than refused: the tool reads the leading zero, finds
    the x, and exits in the middle of parsing the config.
    """
    errors = check({"RandomTrailers": value})
    assert "RandomTrailers" in errors
    assert "Use on or off" in errors["RandomTrailers"]


@pytest.mark.parametrize("value, on", [("0", False), ("00", False), ("1", True), ("5", True)])
def test_a_switch_reads_a_number_the_way_the_tools_do(value, on):
    """parse_bool takes on and off through strcasecmp and then anything that
    parses as a plain number, zero being off. The panel used to refuse the
    numbers, which made a hand-written `RandomTrailers = 0` - a config the
    tools accept and bring the interface up on - reject every unrelated save,
    because the value is in the merged set each save is validated against.
    """
    assert check({"RandomTrailers": value}) == {}
    assert validate._is_set(validate.PARAMS["RandomTrailers"], value) is on
    assert validate.is_set("RandomTrailers", value) is on


@pytest.mark.parametrize("key", ["Jc", "S1", "H1", "I1", "HeaderProtectionKey", "RekeyAfterTime"])
def test_off_is_only_unset_for_a_switch(key):
    """The word "off" reads as unset for the one parameter that is a switch, and
    as the malformed value it is for every other member of those groups.

    The groups that treat "0" as unset hold numbers, ranges and keys as well as
    the switch. Reading the word "off" as unset across all of them would let a
    hand-written `Jc = off` past the settings page - the tools parse Jc with
    parse_uint16 and refuse that config - leaving nothing to warn the admin
    before the interface fails to come up.
    """
    assert validate._is_set(validate.PARAMS[key], "off") is True
    assert key in check({key: "off"})


def test_random_trailers_is_drawn_on_with_the_rest_of_the_group():
    """It used to be the one member left off: it arrived in AmneziaWG 3.1 rather
    than 3.0, and a peer without it drops an arriving handshake for being longer
    than it expects. Where a header protection key is drawn beside it that costs
    the peers on exactly 3.0 and no more, anything older having already failed on
    the key; where the MTU left no room for one it costs every peer below 3.1,
    since nothing else here fails outright. Either way it is drawn like the rest
    rather than waiting for somebody to find the switch.

    "on" and not "off": off is spelled by removing the line, and a written "off"
    is a line every client config would then carry a meaningless copy of."""
    assert validate.randomize_advanced()["RandomTrailers"] == "on"
    assert validate.warnings_for({"RandomTrailers": "on"}) == []


def test_random_trailers_requires_module_feature_support():
    """On a kernel module without random trailer support, enabling the setting would
    be ignored and clients expecting trailers would fail to handshake."""
    assert check({"RandomTrailers": "on"}, {**ALL_FEATURES, "random_trailers": True}) == {}
    errors = check({"RandomTrailers": "on"}, {**ALL_FEATURES, "random_trailers": False})
    assert "RandomTrailers" in errors
    assert "random packet trailers" in errors["RandomTrailers"]


# ---------------------------------------------------------- disable cookies


def test_disable_cookies_is_server_only_and_never_mirrored():
    """The whole reason it is not in AWG_PARAMS: that tuple is copied into every
    client config, and a client has nothing to do with this switch."""
    assert "DisableCookies" in validate.SERVER_ONLY_PARAMS
    assert "DisableCookies" not in validate.AWG_PARAMS
    assert validate.PARAMS["DisableCookies"].must_match_client is False


def test_disable_cookies_is_not_part_of_the_advanced_group():
    """ADVANCED_PARAMS is read off the group and drives the generator. Drawing a
    server-side DoS switch as part of an obfuscation profile would switch off
    flood protection on every Generate."""
    assert "DisableCookies" not in validate.ADVANCED_PARAMS
    assert "DisableCookies" not in validate.randomize()
    assert "DisableCookies" not in validate.randomize_advanced()


def test_disable_cookies_accepts_on_and_reads_off_as_unset():
    assert check({"DisableCookies": "on"}) == {}
    assert validate._is_set(validate.PARAMS["DisableCookies"], "off") is False
    assert check({"DisableCookies": "off"}) == {}


@pytest.mark.parametrize("value", ["yes", "true", "disabled"])
def test_disable_cookies_rejects_what_the_tools_cannot_read(value):
    """What parse_bool refuses, the interface does not come back up on. The
    numbers it does take are held one test up, with the other switch."""
    errors = check({"DisableCookies": value})
    assert "DisableCookies" in errors
    assert "Use on or off" in errors["DisableCookies"]


def test_disable_cookies_warns_about_what_it_costs_when_on():
    """Nothing about a flood is visible from a settings page, so the save is where
    an admin first hears the protection is going away - and server/status keeps
    saying it for as long as the switch is on, which test_api_server holds.

    What it costs is the CPU spent verifying a flood nobody asked for a cookie,
    not the clients that used to be dropped with it: the module stopped counting
    itself under load at all in 3.1.20260828, and the wording followed."""
    matching = [t for t in validate.warnings_for({"DisableCookies": "on"}) if "DisableCookies" in t]
    assert matching, "enabling it must say what it costs"
    assert "cookie" in matching[0] and "flood" in matching[0]
    assert "CPU" in matching[0], "the cost is the work, and the warning has to name it"
    assert not [
        t for t in validate.warnings_for({"DisableCookies": "off"}) if "DisableCookies" in t
    ]


def test_disable_cookies_is_not_flagged_as_importer_unsafe():
    """importer_safe is about the Amnezia app dropping lines from a client config.
    This one never reaches a client config, so warning about the importer would
    send an admin looking for a client to fix that does not exist."""
    assert validate.PARAMS["DisableCookies"].importer_safe is True
    assert not [
        text for text in validate.warnings_for({"DisableCookies": "on"}) if "Amnezia app" in text
    ]


def test_disable_cookies_unsupported_message_blames_no_client():
    """A server-only setting an old module cannot do is simply not done. The
    stock wording talks about clients failing to connect, which is a wrong
    trail here."""
    assert check({"DisableCookies": "on"}, {**ALL_FEATURES, "disable_cookies": True}) == {}
    errors = check({"DisableCookies": "on"}, {**ALL_FEATURES, "disable_cookies": False})
    assert "DisableCookies" in errors
    assert "do nothing at all" in errors["DisableCookies"]
    assert "clients" not in errors["DisableCookies"]


# ------------------------------------------------------------------ is_set


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("RandomTrailers", "off", False),
        ("RandomTrailers", "", False),
        ("RandomTrailers", "OFF", False),
        ("RandomTrailers", "on", True),
        ("Jc", "0", False),
        ("Jc", "", False),
        ("Jc", "4", True),
        ("S1", "0", False),
        ("S1", "", False),
        ("S1", "32", True),
        ("H1", "0", False),
        ("H1", "", False),
        ("H1", "5-500000000", True),
        ("I1", "0", False),
        ("I1", "", False),
        ("I1", "<r 4>", True),
    ],
)
def test_is_set_delegates_to_the_private_helper_for_known_parameters(key, value, expected):
    """The public lookup by name delegates to _is_set for all modeled parameters.

    Switches are unset on "off" or empty and set on "on", while numeric and
    pattern parameters treat "0" and empty as unset and real values as set,
    matching what the config writer needs without reaching for the spec.
    """
    assert validate.is_set(key, value) is expected


def test_is_set_treats_zero_as_off_for_a_switch():
    """ "0" is one of the two spellings of off, so nothing is written for it.

    The rule is still on the kind rather than on the group - a switch says off
    in a word as well, which no number group does - but the answer for "0" is
    the same either way, because parse_bool reads a number and calls zero off.
    """
    assert validate.is_set("RandomTrailers", "0") is False
    assert validate.is_set("DisableCookies", "0") is False


@pytest.mark.parametrize(
    "value, expected",
    [
        ("0", True),
        ("x", True),
        ("", False),
        ("   ", False),
    ],
)
def test_is_set_falls_back_to_emptiness_for_unknown_keys(value, expected):
    """A key PARAMS does not contain falls back to plain emptiness.

    The caller had a reason to carry a key this module does not model, and
    dropping its "0" would silently rewrite it. Only empty or whitespace-only
    values are omitted.
    """
    assert validate.is_set("UnknownKey", value) is expected


@pytest.mark.parametrize("key", validate.AWG_PARAMS)
def test_is_set_agrees_with_client_conf_emission_needs(key):
    """_emit_client_conf relies on is_set to omit unconfigured settings.

    For every key in AWG_PARAMS, an empty value is omitted and a value of "0" is
    omitted with it - for the numbers because "0" is how the config spells off,
    and for a switch because parse_bool reads the same number the same way.
    Parametrizing over AWG_PARAMS ensures any future obfuscation parameter joins
    this guarantee automatically.
    """
    assert validate.is_set(key, "") is False
    assert validate.is_set(key, "0") is False
