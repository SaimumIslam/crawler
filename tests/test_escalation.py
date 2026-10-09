"""Verify the tiered fetcher escalates on block and stops on success,
using fake per-tier fetchers injected in place of the real ones.
"""
from crawler.config import FetchSpec
from crawler.fetch.base import FetchResult
from crawler.fetch.escalation import TieredFetcher


class FakeFetcher:
    def __init__(self, tier, result):
        self.tier_name = tier
        self._result = result
        self.calls = 0

    def fetch(self, url, proxy=None):
        self.calls += 1
        return self._result

    def close(self):
        pass


def _blocked(tier):
    return FetchResult(url="u", status_code=403, text="denied", tier=tier,
                       ok=False, blocked=True)


def _ok(tier):
    return FetchResult(url="u", status_code=200, text="hi", tier=tier, ok=True)


def _make(spec, tiers):
    tf = TieredFetcher(spec, site="t")
    tf._fetchers = tiers  # inject fakes; bypass lazy construction
    return tf


def test_escalates_past_blocked_tier():
    spec = FetchSpec(max_retries=1)
    tiers = {
        "http": FakeFetcher("http", _blocked("http")),
        "patchright": FakeFetcher("patchright", _ok("patchright")),
    }
    tf = _make(spec, tiers)
    res = tf.fetch("http://x")
    assert res.ok and res.tier == "patchright"
    assert tiers["http"].calls == 1  # block => no wasted retries at tier 1


def test_stops_at_first_success():
    spec = FetchSpec(max_retries=2)
    tiers = {"http": FakeFetcher("http", _ok("http"))}
    tf = _make(spec, tiers)
    res = tf.fetch("http://x")
    assert res.ok and res.tier == "http"
    assert tiers["http"].calls == 1


def test_min_tier_pins_start():
    spec = FetchSpec(min_tier="patchright", max_retries=1)
    tiers = {"patchright": FakeFetcher("patchright", _ok("patchright"))}
    tf = _make(spec, tiers)
    res = tf.fetch("http://x")
    assert res.tier == "patchright"
