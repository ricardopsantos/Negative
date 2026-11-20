"""PH92 — X-Forwarded-For only from our own proxy (card 20261112-185835)."""
import server


def test_ph92_trusted_proxy_peers():
    for ok in ("127.0.0.1", "::1", "172.18.0.1"):
        assert server._is_trusted_proxy(ok), ok
    for bad in ("192.168.1.50", "10.0.0.2", "100.64.0.1", "8.8.8.8", "junk"):
        assert not server._is_trusted_proxy(bad), bad
