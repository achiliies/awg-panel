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
}

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


def test_header_protection_key_warns_about_the_app_importer():
    """It works between hand-configured peers and silently breaks every app user."""
    warnings = validate.warnings_for({"HeaderProtectionKey": SAMPLE_KEY})

    matching = [text for text in warnings if "HeaderProtectionKey" in text]
    assert matching, warnings
    text = matching[0]
    assert "Amnezia app" in text
    assert "no error" in text
    # Never echo key material back into a message that ends up in the UI.
    assert SAMPLE_KEY not in text


def test_header_protection_key_is_still_valid_input():
    padded = {key: "32" for key in validate.HEADER_PROTECTED}
    assert check({**padded, "HeaderProtectionKey": SAMPLE_KEY}) == {}
    assert check({**padded, "HeaderProtectionKey": "0" * 64}) == {}
    assert "HeaderProtectionKey" in check({"HeaderProtectionKey": "not-a-key"})


def test_empty_advanced_parameters_produce_no_importer_warning():
    values = {"HeaderProtectionKey": "", "ContentPaddingAddition": "", "RekeyAfterTime": ""}
    assert not [text for text in validate.warnings_for(values) if "Amnezia app" in text]


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
    assert check({"MTU": "1400", "S4": "30", "ContentPaddingAddition": "0-900"}) == {}


def test_an_overrun_is_reported_against_every_field_that_can_fix_it():
    """The MTU and S4 are edited on two different pages, so an error left only
    on the field the operator cannot see is one they cannot act on."""
    errors = check({"MTU": "1400", "S4": "60"})
    assert set(errors) == {"MTU", "S4"}


def test_padding_exactly_on_the_budget_is_accepted():
    """A bound that rejects the value it tells you to use is a bound nobody
    can satisfy."""
    assert check({"MTU": "1400", "S4": "40"}) == {}


def test_lowering_the_mtu_makes_the_same_padding_fit():
    over = {"MTU": "1400", "S4": "120"}
    assert check(over)
    assert check({**over, "MTU": "1320"}) == {}


def test_padding_switched_off_never_overruns():
    """0 means off in every tool here, so it is not a byte to charge for."""
    assert check({"MTU": "1420", "S4": "0"}) == {}


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
    black-holes full-size packets rather than failing loudly."""
    for mtu in (1280, 1360, 1400, 1420):
        for _ in range(ROUNDS):
            s4 = int(validate.randomize(mtu=mtu)["S4"])
            assert s4 <= 1440 - mtu, f"S4 {s4} does not fit an MTU of {mtu}"


@pytest.mark.parametrize("mtu", ["1420", 1420])
def test_randomize_takes_the_mtu_as_text_or_a_number(mtu):
    """Config values arrive as text about as often as they arrive as integers,
    and the string form used to reach the arithmetic and raise TypeError."""
    for _ in range(ROUNDS):
        assert int(validate.randomize(mtu=mtu)["S4"]) <= 20


@pytest.mark.parametrize("mtu", [None, 0, "", "not a number"])
def test_randomize_falls_back_to_the_default_mtu(mtu):
    """An unreadable MTU is not a reason to refuse; the default leaves the most
    room, so falling back to it is the conservative direction."""
    for _ in range(ROUNDS):
        assert 8 <= int(validate.randomize(mtu=mtu)["S4"]) <= 40


def test_randomize_header_ranges_do_not_overlap_and_are_wide():
    for _ in range(ROUNDS):
        values = validate.randomize()
        bounds = sorted(
            tuple(int(part) for part in values[f"H{n}"].split("-")) for n in range(1, 5)
        )
        for (_low, high), (next_low, _next_high) in zip(bounds, bounds[1:], strict=False):
            assert high < next_low, bounds
        assert bounds[0][0] >= validate.H_FLOOR
        assert bounds[-1][1] <= validate.H_MAX
        # Narrow ranges repeat values, which is the constant they exist to avoid.
        for low, high in bounds:
            assert high - low > 100_000_000, (low, high)


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
    for mtu in (1280, 1360, 1400, 1420):
        for _ in range(ROUNDS):
            s4 = int(validate.randomize(mtu=mtu, profile=profile)["S4"])
            assert s4 <= 1440 - mtu, f"{profile}: S4 {s4} does not fit an MTU of {mtu}"


@pytest.mark.parametrize("profile", PROFILE_KEYS)
def test_every_profile_stays_above_the_header_protection_floor(profile):
    """The cheapest profile has to be usable with the strongest setting.

    The two are drawn by different buttons, and neither can see what the other
    has been asked for, so the only way they cannot contradict each other is for
    no profile to go below the floor at all. Twelve bytes on a data packet is
    what that costs, out of a headroom that is never less than twenty."""
    for mtu in (1280, 1360, 1400, 1420):
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
    before the handshake replacing it lands."""
    for _ in range(ROUNDS):
        values = validate.randomize_advanced(profile=profile)
        reject = int(values["RejectAfterTime"])
        rekey = int(values["RekeyAfterTime"])
        cycle = int(values["KeepaliveTimeout"]) + int(values["RekeyTimeout"])
        assert reject > rekey + cycle, values
        # The far end measures its own key against RejectAfterTime minus that
        # wait, and a responder that reaches it first starts handshaking on top
        # of the initiator - twice the handshakes, which is the one event on the
        # wire that none of this can disguise.
        assert reject - cycle > rekey, values


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
    }, key
    assert spec.kind in {"int", "range", "imitation", "key", "text", "cidr", "port", "iplist"}, key


def test_catalog_covers_every_mirrored_parameter():
    """AWG_PARAMS is what gets copied into client configs; a parameter missing
    from the catalog would be uneditable and unexplained in the panel."""
    assert set(validate.AWG_PARAMS) <= set(validate.PARAMS)
    assert set(validate.NETWORK_PARAMS) <= set(validate.PARAMS)
    assert set(validate.HOOK_PARAMS) <= set(validate.PARAMS)


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
    }


def test_unknown_keys_are_ignored():
    """Callers hand over a whole parsed [Interface]; PrivateKey is not ours to check."""
    assert check({"PrivateKey": "whatever", "Table": "off"}) == {}
