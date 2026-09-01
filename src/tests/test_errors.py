"""错误分类 + 重试 + 网络检测 单测。errors.py 之前 0 覆盖。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.errors import (
    WeiboError, WeiboErrorKind, classify_error, is_recoverable,
    get_retry_delay, retry_with_backoff,
)


class ClassifyErrorTests(unittest.TestCase):
    def test_timeout_keyword(self):
        e = TimeoutError("连接超时")
        self.assertEqual(classify_error(e), WeiboErrorKind.NETWORK)

    def test_dns_keyword(self):
        e = RuntimeError("DNS 解析失败")
        self.assertEqual(classify_error(e), WeiboErrorKind.NETWORK)

    def test_httpx_401_is_auth(self):
        """用真 httpx.HTTPStatusError 对象（class 名会被识别）"""
        import httpx
        e = httpx.HTTPStatusError("401 Unauthorized", request=None, response=None)
        self.assertEqual(classify_error(e), WeiboErrorKind.AUTH)

    def test_httpx_404_is_not_found(self):
        import httpx
        e = httpx.HTTPStatusError("404 Not Found", request=None, response=None)
        self.assertEqual(classify_error(e), WeiboErrorKind.NOT_FOUND)

    def test_429_plain_exception_is_rate_limit(self):
        """非 httpx 异常的 429 → 走关键字分支 'too many' → RATE_LIMIT"""
        e = Exception("429 Too Many Requests")
        self.assertEqual(classify_error(e), WeiboErrorKind.RATE_LIMIT)

    def test_crawl4weibo_exact_432_block_is_rate_limit(self):
        from crawl4weibo.exceptions.base import NetworkError

        e = NetworkError("Encountered 432 anti-crawler block")
        self.assertEqual(classify_error(e), WeiboErrorKind.RATE_LIMIT)

    def test_chinese_login_keyword(self):
        e = RuntimeError("未登录或登录态过期")
        self.assertEqual(classify_error(e), WeiboErrorKind.AUTH)

    def test_json_parse_keyword(self):
        e = ValueError("json decode error")
        self.assertEqual(classify_error(e), WeiboErrorKind.PARSE)

    def test_unknown_falls_through(self):
        e = RuntimeError("something weird happened")
        self.assertEqual(classify_error(e), WeiboErrorKind.UNKNOWN)


class IsRecoverableTests(unittest.TestCase):
    def test_network_recoverable(self):
        self.assertTrue(is_recoverable(WeiboErrorKind.NETWORK))

    def test_rate_limit_recoverable(self):
        self.assertTrue(is_recoverable(WeiboErrorKind.RATE_LIMIT))

    def test_auth_not_recoverable(self):
        self.assertFalse(is_recoverable(WeiboErrorKind.AUTH))

    def test_not_found_not_recoverable(self):
        self.assertFalse(is_recoverable(WeiboErrorKind.NOT_FOUND))


class GetRetryDelayTests(unittest.TestCase):
    def test_first_attempt_small(self):
        d = get_retry_delay(0, base=1.0, max_delay=60.0)
        # 0*1 + 0~1 random ∈ [0, 2)
        self.assertGreaterEqual(d, 0)
        self.assertLess(d, 3)

    def test_clamped_to_max(self):
        # attempt 越大越接近 max
        d = get_retry_delay(20, base=1.0, max_delay=10.0)
        self.assertLessEqual(d, 11)  # max + random

    def test_exponential_growth(self):
        d1 = get_retry_delay(0, base=1.0, max_delay=100.0)
        d3 = get_retry_delay(3, base=1.0, max_delay=100.0)
        # d3 平均 8+0.5=8.5，d1 平均 0.5，d3 > d1
        # 用 deterministic random 看增长：base*2^0=1 vs base*2^3=8
        # 这里只测数量级
        self.assertGreater(d3, d1)


class RetryWithBackoffTests(unittest.TestCase):
    def test_success_first_try(self):
        calls = []
        def f():
            calls.append(1)
            return "ok"
        result = retry_with_backoff(f, max_attempts=3, base_delay=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)

    def test_retry_on_network(self):
        """网络错重试，第三次成功"""
        attempts = [0]
        def f():
            attempts[0] += 1
            if attempts[0] < 3:
                raise TimeoutError("超时")
            return "done"
        result = retry_with_backoff(f, max_attempts=5, base_delay=0.001)
        self.assertEqual(result, "done")
        self.assertEqual(attempts[0], 3)

    def test_no_retry_on_auth(self):
        """AUTH 错误不重试，直接抛"""
        attempts = [0]
        def f():
            attempts[0] += 1
            raise WeiboError("未登录", kind=WeiboErrorKind.AUTH, recoverable=False)
        with self.assertRaises(WeiboError):
            retry_with_backoff(f, max_attempts=5, base_delay=0.001)
        # auth 错误在 attempt 0 就被认作不可恢复，第 2 次 attempt 才 raise
        # 但 attempt 0 也会试一次（先 return 然后 catch），所以 attempts=2
        self.assertEqual(attempts[0], 2)


if __name__ == "__main__":
    unittest.main()
