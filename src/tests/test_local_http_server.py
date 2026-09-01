"""desktop.self_test.functional._local_http_server 真实 HTTP 测试。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from desktop.self_test.functional import FIXTURES_ROOT, _local_http_server


def test_local_server_serves_index_with_expected_title() -> None:
    with _local_http_server(FIXTURES_ROOT) as base_url:
        response = httpx.get(f"{base_url}/index.html", timeout=5)
        assert response.status_code == 200
        assert "微书薯自检" in response.text


def test_local_server_serves_sample_bin_with_exact_bytes() -> None:
    fixture_bytes = (FIXTURES_ROOT / "media" / "sample.bin").read_bytes()
    with _local_http_server(FIXTURES_ROOT / "media") as base_url:
        response = httpx.get(f"{base_url}/sample.bin", timeout=5)
        assert response.status_code == 200
        assert response.content == fixture_bytes


def test_local_server_thread_exits_after_shutdown() -> None:
    import socket

    root = Path(__file__).resolve().parents[1] / "desktop/self_test/fixtures"
    with _local_http_server(root) as base_url:
        # 获取 server 状态没有直接句柄；通过确认端口关闭来证明线程退出。
        host, port = base_url.replace("http://", "").split(":")
        port = int(port)
        with socket.create_connection((host, port), timeout=1):
            pass
    # 短暂等待 shutdown 完成，然后确认端口已释放。
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                time.sleep(0.05)
        except OSError:
            break
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        assert sock.connect_ex((host, port)) != 0
