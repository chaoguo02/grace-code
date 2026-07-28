from __future__ import annotations

import pytest

from server.main import validate_bind_host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost"])
def test_loopback_bind_is_allowed_without_remote_opt_in(host):
    assert validate_bind_host(host) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.test"])
def test_remote_bind_requires_explicit_opt_in(host):
    with pytest.raises(ValueError, match="only supports local"):
        validate_bind_host(host)


def test_remote_bind_has_no_bypass():
    with pytest.raises(ValueError, match="only supports local"):
        validate_bind_host("0.0.0.0")
