"""自检网络守卫测试。"""

from __future__ import annotations

import pytest

from desktop.self_test.network_guard import assert_loopback, is_loopback_host


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.2", "::1"],
)
def test_loopback_hosts_allowed(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["weibo.com", "8.8.8.8", "192.168.1.1"],
)
def test_non_loopback_hosts_rejected(host: str) -> None:
    assert is_loopback_host(host) is False


def test_assert_loopback_rejects_external_url() -> None:
    with pytest.raises(ValueError):
        assert_loopback("https://weibo.com/")
    assert_loopback("http://127.0.0.1:18080/healthz")
