"""The offline quickstart's local site: it binds without a reverse DNS lookup (no macOS Local Network prompt)."""
import importlib.util
import pathlib
import socket
import sys
from unittest import mock

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_FIXTURE = _HERE / "examples" / "quickstart" / "fixture_site.py"


def _fixture_module():
    spec = importlib.util.spec_from_file_location("quickstart_fixture_site", _FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixture_server_binds_without_a_reverse_dns_lookup():
    fx = _fixture_module()
    sock = mock.Mock()
    sock.getsockname.return_value = ("127.0.0.1", 48001)
    # Keep real TCPServer construction and LoopbackServer.server_bind, but
    # replace the OS socket so this remains a strictly offline unit test.
    with mock.patch.object(socket, "socket", return_value=sock) as create_socket, \
            mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS lookup")):
        server = fx.LoopbackServer(("127.0.0.1", 0), fx.Handler)
    try:
        assert server.server_name == "127.0.0.1" and server.server_port == server.server_address[1] > 0
        create_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind.assert_called_once_with(("127.0.0.1", 0))
        sock.listen.assert_called_once()
    finally:
        server.server_close()
    sock.close.assert_called_once()


def test_the_fixture_uses_the_loopback_server():
    assert "LoopbackServer((\"127.0.0.1\", args.port), Handler)" in _FIXTURE.read_text()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
