"""clients.env round trips, including the parts bash cares about.

install.sh edits this file with `sed -i "s|^KEY=.*|KEY=\\"new\\"|"`, which drops the
aligned comment that explains the setting. The panel is expected to do better
than that and worse than nothing: keep every comment, every unknown key and
every blank line, and still emit a value bash reads back exactly as given.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from awg import clientsenv, validate
from awg.errors import ValidationError

# What install.sh writes, aligned trailing comments included - as it wrote it
# before the CLI was retired, header and all. Deliberately the old spelling: an
# upgraded server keeps the header it was installed with, nothing here reads
# one, and a parser that only handles the current wording would fail on every
# box in the field.
INSTALLED = """\
# awg-client settings. Sourced by bash - every value must be QUOTED.
ENDPOINT_HOST="203.0.113.10"    # blank = auto-detect
ENDPOINT_PORT=""                # blank = ListenPort from the server config
CLIENT_DNS="8.8.8.8, 8.8.4.4"
CLIENT_MTU="1400"
CLIENT_ALLOWED_IPS="0.0.0.0/0"  # "10.13.13.0/24" for split tunnel
SUBNET_BASE="10.13.13"
KEEPALIVE="25"
"""

# Values an admin can legitimately type into the DNS or endpoint fields, plus the
# characters bash still expands inside double quotes.
AWKWARD = [
    "8.8.8.8, 8.8.4.4",
    'a "quoted" host',
    "cost $5 for 100%",
    "back\\slash",
    "`hostname`",
    "trailing space ",
    "",
]


def test_missing_file_gives_the_defaults_a_client_config_is_built_from() -> None:
    """A deleted clients.env must not change what a client config comes out as."""
    assert clientsenv.read_env() == clientsenv.DEFAULTS
    assert clientsenv.DEFAULTS["CLIENT_DNS"] == "8.8.8.8, 8.8.4.4"
    assert clientsenv.DEFAULTS["CLIENT_MTU"] == "1372"
    # Blank, not a network: the tunnel subnet is the server's own Address and
    # these two only mirror it, so a default here would override an older
    # file's customised SUBNET_BASE and move the allocator.
    assert clientsenv.DEFAULTS["SUBNET_CIDR"] == ""
    assert clientsenv.DEFAULTS["SUBNET_BASE"] == ""


def test_the_defaults_are_the_values_install_sh_writes() -> None:
    """Read out of the installer rather than restated here.

    The two are one setting with two spellings: install.sh writes the line and
    this file stands in for it when the line is gone, so a client issued by a
    panel whose clients.env was deleted has to come out as one issued by a panel
    whose file is intact. Asserting the constants twice is what let them drift -
    CLIENT_DNS said 1.1.1.1 against an installer writing 8.8.8.8, under a comment
    claiming they agreed.

    CLIENT_ALLOWED_IPS is deliberately not here: the installer appends ::/0 when
    the tunnel carries IPv6, and this file cannot know whether it does.
    """
    installer = Path(__file__).resolve().parents[2] / "install.sh"
    if not installer.is_file():
        pytest.skip("no install.sh beside the panel; this is an installed tree")

    text = installer.read_text(encoding="utf-8")

    def assigned(name: str) -> str:
        # The first plain assignment, which is the default. Later ones are the
        # conditional IPv6 variants and the command-line overrides.
        found = re.search(rf'^{name}="?([^"\n]*)"?', text, re.MULTILINE)
        assert found, f"{name} is not assigned in install.sh any more"
        return found.group(1)

    assert clientsenv.DEFAULTS["CLIENT_DNS"] == assigned("CLIENT_DNS_DEFAULT")
    assert clientsenv.DEFAULTS["CLIENT_MTU"] == assigned("MTU")
    # And the third copy of that number, which the two above do not reach.
    # validate.DEFAULT_MTU is what the panel offers on the Server page and
    # what S4 is drawn against when a config carries no MTU of its own, so a
    # default that moved in the installer and not here would hand every new
    # server a padding set drawn for an MTU it is not running.
    assert clientsenv.DEFAULTS["CLIENT_MTU"] == str(validate.DEFAULT_MTU)
    assert clientsenv.DEFAULTS["KEEPALIVE"] == "25"
    assert f'KEEPALIVE="{clientsenv.DEFAULTS["KEEPALIVE"]}"' in text


def test_values_are_unquoted_and_comments_are_not_part_of_them(clients_env: Path) -> None:
    values = clientsenv.read_env(clients_env)
    assert values["ENDPOINT_HOST"] == "203.0.113.10"
    assert values["ENDPOINT_PORT"] == ""
    assert values["CLIENT_DNS"] == "8.8.8.8, 8.8.4.4"
    assert values["CLIENT_ALLOWED_IPS"] == "0.0.0.0/0"
    assert values["KEEPALIVE"] == "25"


def test_partial_file_is_topped_up_from_the_defaults(conf_dir: Path) -> None:
    target = conf_dir / "clients.env"
    target.write_text('SUBNET_BASE="10.66.66"\n', encoding="utf-8")
    values = clientsenv.read_env(target)
    assert values["SUBNET_BASE"] == "10.66.66"
    assert values["CLIENT_MTU"] == clientsenv.DEFAULTS["CLIENT_MTU"]


def test_editing_one_key_keeps_every_other_byte(clients_env: Path) -> None:
    clientsenv.update_env({"CLIENT_DNS": "9.9.9.9"}, clients_env)
    assert clients_env.read_text(encoding="utf-8") == INSTALLED.replace(
        'CLIENT_DNS="8.8.8.8, 8.8.4.4"', 'CLIENT_DNS="9.9.9.9"'
    )


def test_inline_comments_survive_and_stay_aligned(clients_env: Path) -> None:
    """The comments are the only documentation an admin has in this file."""
    clientsenv.update_env({"ENDPOINT_HOST": "198.51.100.7"}, clients_env)
    lines = clients_env.read_text(encoding="utf-8").splitlines()
    # Untouched, including a header this panel would no longer write itself.
    assert lines[0] == INSTALLED.splitlines()[0]
    assert lines[1] == 'ENDPOINT_HOST="198.51.100.7"    # blank = auto-detect'
    assert lines[1].index("#") == INSTALLED.splitlines()[1].index("#")


def test_a_longer_value_pushes_its_comment_right_instead_of_eating_it(clients_env: Path) -> None:
    clientsenv.update_env({"ENDPOINT_HOST": "vpn.a-rather-long-hostname.example"}, clients_env)
    line = clients_env.read_text(encoding="utf-8").splitlines()[1]
    assert line == 'ENDPOINT_HOST="vpn.a-rather-long-hostname.example" # blank = auto-detect'


def test_unknown_keys_and_blank_lines_are_left_alone(conf_dir: Path) -> None:
    """Somebody's own variable, or one from a newer version, is not ours to delete."""
    target = conf_dir / "clients.env"
    target.write_text(
        '# local overrides\nSITE_NAME="hq"\n\nCLIENT_MTU="1400"\n\n# end\n', encoding="utf-8"
    )
    clientsenv.update_env({"CLIENT_MTU": "1380"}, target)
    assert target.read_text(encoding="utf-8") == (
        '# local overrides\nSITE_NAME="hq"\n\nCLIENT_MTU="1380"\n\n# end\n'
    )
    assert clientsenv.read_env(target)["SITE_NAME"] == "hq"


def test_a_new_key_is_appended_quoted(clients_env: Path) -> None:
    clientsenv.update_env({"CLIENT_KEEPALIVE_NOTE": "set by the panel"}, clients_env)
    text = clients_env.read_text(encoding="utf-8")
    assert text.endswith('CLIENT_KEEPALIVE_NOTE="set by the panel"\n')
    assert text.startswith(INSTALLED)


def test_a_duplicate_key_is_rewritten_everywhere(conf_dir: Path) -> None:
    """Bash takes the last assignment; leaving one behind would make the save a no-op."""
    target = conf_dir / "clients.env"
    target.write_text('CLIENT_DNS="1.1.1.1"\nCLIENT_DNS="8.8.8.8"\n', encoding="utf-8")
    clientsenv.update_env({"CLIENT_DNS": "9.9.9.9"}, target)
    assert target.read_text(encoding="utf-8") == 'CLIENT_DNS="9.9.9.9"\nCLIENT_DNS="9.9.9.9"\n'
    assert clientsenv.read_env(target)["CLIENT_DNS"] == "9.9.9.9"


def test_the_file_is_created_when_it_does_not_exist(conf_dir: Path) -> None:
    """The one case where the panel writes the header rather than preserving one.

    Asserted against HEADER itself, not a copy of its text: the header is a
    sentence an admin reads in a file they are invited to edit, and the last
    copy of it in a test was how it went on saying "awg-client settings" after
    that tool was deleted.
    """
    target = conf_dir / "clients.env"
    clientsenv.update_env({"CLIENT_DNS": "9.9.9.9"}, target)
    assert target.read_text(encoding="utf-8") == clientsenv.HEADER + 'CLIENT_DNS="9.9.9.9"\n'
    assert "awg-client" not in clientsenv.HEADER


def test_written_files_are_not_world_readable(clients_env: Path) -> None:
    """This file names the server's endpoint; install.sh keeps it 0600 and so do we."""
    clientsenv.update_env({"CLIENT_DNS": "9.9.9.9"}, clients_env)
    assert clients_env.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", AWKWARD)
def test_awkward_values_survive_a_write_and_a_read(clients_env: Path, value: str) -> None:
    clientsenv.update_env({"CLIENT_DNS": value}, clients_env)
    assert clientsenv.read_env(clients_env)["CLIENT_DNS"] == value


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is the second reader of this file")
@pytest.mark.parametrize("value", AWKWARD)
def test_bash_reads_back_exactly_what_was_written(clients_env: Path, value: str) -> None:
    """The format is shell assignment syntax, so ask a shell what it reads.

    Nothing sources this file any more, and the escaping still has to be exactly
    right: the syntax is what an admin edits by hand and what install.sh writes
    with sed, and a value this panel wrote that bash reads differently is one
    that would come back changed the moment anything else touched the file.
    """
    clientsenv.update_env({"CLIENT_DNS": value}, clients_env)
    result = subprocess.run(
        ["bash", "-c", f'. "{clients_env}"; printf "%s" "$CLIENT_DNS"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == value


def test_a_line_break_is_refused_rather_than_written(clients_env: Path) -> None:
    """A newline would turn one assignment into two lines bash reads as commands."""
    with pytest.raises(ValidationError) as caught:
        clientsenv.update_env({"CLIENT_DNS": "1.1.1.1\nrm -rf /"}, clients_env)
    assert "line break" in str(caught.value)
    assert clients_env.read_text(encoding="utf-8") == INSTALLED
