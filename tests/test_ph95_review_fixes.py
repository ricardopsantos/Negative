"""PH95 — review fixes 2026-11-24: gateway trust, quiet access log, hidden Live Photo clips out of People."""
import server


def test_ph95_default_gateway_is_trusted(monkeypatch):
    monkeypatch.setattr(server, "_GATEWAY", "192.168.240.1")
    assert server._is_trusted_proxy("192.168.240.1")
    assert not server._is_trusted_proxy("192.168.240.7")
    monkeypatch.setattr(server, "_GATEWAY", "")
    assert not server._is_trusted_proxy("192.168.240.1")


def test_ph95_gateway_parse(monkeypatch, tmp_path):
    f = tmp_path / "route"
    f.write_text("Iface\tDestination\tGateway\tFlags\neth0\t00000000\t01F0A8C0\t0003\neth0\t00F0A8C0\t00000000\t0001\n")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(f if p == "/proc/net/route" else p, *a, **k))
    assert server._default_gateway() == "192.168.240.1"


def test_ph95_healthz_not_logged():
    import logging
    import logfilter
    f = logfilter.NoHealthz()
    mk = lambda m: logging.LogRecord("a", 20, "", 0, m, (), None)
    assert not f.filter(mk('1.2.3.4 "GET /healthz HTTP/1.1" 200'))
    assert f.filter(mk('1.2.3.4 "GET /api/people HTTP/1.1" 200'))


def test_ph95_people_query_runs(client, auth, lib):
    assert client.get("/api/people", headers=auth).status_code == 200
