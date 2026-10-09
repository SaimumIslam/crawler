"""RateLimiter: per-domain spacing that does not serialize unrelated domains."""
import threading
import time

from crawler.crawl.robots import RateLimiter


def test_first_request_no_wait():
    rl = RateLimiter(per_sec=2)  # 0.5s interval
    t0 = time.monotonic()
    rl.wait("https://a.com/1")
    assert time.monotonic() - t0 < 0.1


def test_same_domain_is_spaced():
    rl = RateLimiter(per_sec=10)  # 0.1s interval
    t0 = time.monotonic()
    rl.wait("https://a.com/1")
    rl.wait("https://a.com/2")
    assert time.monotonic() - t0 >= 0.09


def test_different_domains_are_independent():
    rl = RateLimiter(per_sec=2)  # 0.5s interval
    t0 = time.monotonic()
    rl.wait("https://a.com/1")
    rl.wait("https://b.com/1")  # different domain -> should not wait
    assert time.monotonic() - t0 < 0.1


def test_sleep_does_not_hold_lock_across_domains():
    """A pending wait on domain A must not block a wait on domain B (the lock
    is not held during sleep)."""
    rl = RateLimiter(per_sec=2)  # 0.5s interval
    rl.wait("https://a.com/0")  # prime A so the next A wait must sleep ~0.5s
    results: dict[str, float] = {}

    def wait_a():
        t = time.monotonic()
        rl.wait("https://a.com/1")
        results["a"] = time.monotonic() - t

    def wait_b():
        t = time.monotonic()
        rl.wait("https://b.com/1")
        results["b"] = time.monotonic() - t

    ta = threading.Thread(target=wait_a)
    ta.start()
    time.sleep(0.05)  # ensure A is mid-sleep
    tb = threading.Thread(target=wait_b)
    tb.start()
    ta.join()
    tb.join()

    assert results["a"] >= 0.3   # A genuinely waited
    assert results["b"] < 0.1    # B was not blocked by A's sleep
