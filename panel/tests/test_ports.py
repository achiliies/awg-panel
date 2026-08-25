"""The port probe, against sockets this test actually binds.

Nothing here is mocked, which is the point: `ss` output is a format, the filter
is ss's own syntax, and a probe that quietly matched nothing would look exactly
like a machine where every port is free - and would then let every save through
with the reassuring silence of a check that works.

Skipped where iproute2 is not installed. That is a real deployment too, and the
module answers "cannot tell" there rather than raising; there is simply nothing
left to assert about it.
"""

import shutil
import socket

import pytest

from awg import ports

pytestmark = pytest.mark.skipif(shutil.which("ss") is None, reason="iproute2 is not installed")


@pytest.fixture
def udp_socket():
    """A bound UDP socket, and the port it landed on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    yield sock, sock.getsockname()[1]
    sock.close()


def test_a_bound_udp_port_is_busy(udp_socket):
    _, port = udp_socket
    assert ports.busy(port) is True


def test_the_process_holding_it_is_named(udp_socket):
    """The whole reason for the warning: awg-quick's failure never says who."""
    _, port = udp_socket
    # ss reads this one out of our own /proc, so it is knowable whoever runs the
    # suite. What the name is depends on how python was invoked; that it is a
    # name at all is what the message needs.
    assert ports.holder(port) != ""


def test_a_free_port_is_not_busy(udp_socket):
    """The socket is bound and then closed, so the port is one that was real."""
    sock, port = udp_socket
    sock.close()
    assert ports.busy(port) is False


def test_tcp_and_udp_are_different_questions(udp_socket):
    """One number, two ports. A tunnel is not blocked by a web server."""
    _, port = udp_socket
    assert ports.busy(port, "udp") is True
    assert ports.busy(port, "tcp") is False


def test_a_port_is_matched_and_not_grepped_for(udp_socket):
    """5182 must not answer for 51820, which a substring search would."""
    _, port = udp_socket
    shorter = port // 10
    assert ports.busy(shorter) is False or shorter == port


@pytest.mark.parametrize("port", [0, -1, 65536, "51820", None])
def test_nothing_that_is_not_a_port_reaches_ss(port):
    assert ports.busy(port) is False
    assert ports.holder(port) == ""


def test_an_unknown_protocol_is_refused(udp_socket):
    _, port = udp_socket
    assert ports.busy(port, "sctp") is False
