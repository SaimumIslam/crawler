"""Human-like behavior helpers for the browser tiers.

Randomized dwell, scrolling, and small mouse movements make automated page loads
look less mechanical. Kept intentionally light so it does not dominate crawl time.
"""
from __future__ import annotations

import random


def jitter(base_ms: int, spread: float = 0.4) -> float:
    """Return a randomized delay in seconds around base_ms."""
    low = base_ms * (1 - spread)
    high = base_ms * (1 + spread)
    return random.uniform(low, high) / 1000.0


def human_page_actions(page, render_wait_ms: int = 0) -> None:
    """Perform a few human-ish interactions on a Playwright/Patchright page."""
    import time

    try:
        # small initial dwell
        time.sleep(jitter(400))
        # gentle scroll in a couple of steps to trigger lazy content
        for frac in (0.25, 0.6, 1.0):
            page.mouse.wheel(0, random.randint(300, 700))
            time.sleep(jitter(250))
        # a stray mouse move
        page.mouse.move(random.randint(50, 400), random.randint(50, 400))
        if render_wait_ms:
            time.sleep(render_wait_ms / 1000.0)
    except Exception:
        # behavior is best-effort; never fail a fetch because of it
        pass


def _try_click_turnstile(page) -> bool:
    """Best-effort click of a Cloudflare Turnstile / managed-challenge checkbox.

    The widget renders inside a cross-origin iframe (challenges.cloudflare.com),
    so we look through the page's frames for it and click the checkbox. This is the
    same single interaction a human performs; no CAPTCHA is solved or outsourced.
    Returns True if a click was attempted.
    """
    clicked = False
    try:
        for frame in page.frames:
            url = (getattr(frame, "url", "") or "").lower()
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                for sel in ("input[type=checkbox]", "label", "body"):
                    try:
                        frame.click(sel, timeout=1500)
                        clicked = True
                        break
                    except Exception:
                        continue
        if not clicked:
            # Some managed challenges expose the checkbox in the main frame.
            try:
                page.click("input[type=checkbox]", timeout=1000)
                clicked = True
            except Exception:
                pass
    except Exception:
        pass
    return clicked


def _challenge_token_present(page) -> bool:
    """True if a solved-CAPTCHA response token is populated (human completed it).

    Checkbox reCAPTCHA/hCaptcha leave their widget in the DOM after solving, so the
    widget-disappearance heuristic alone would miss a solve on a standalone page.
    A non-empty response token is the reliable "the human finished" signal.
    """
    js = """() => {
        const sels = ['textarea#g-recaptcha-response',
                      'textarea[name="g-recaptcha-response"]',
                      'textarea[name="h-captcha-response"]'];
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el && el.value && el.value.length > 0) return true;
        }
        return false;
    }"""
    try:
        return bool(page.evaluate(js))
    except Exception:
        return False


def wait_for_manual_solve(page, timeout_ms: int, url: str = "") -> str:
    """Human-in-the-loop: pause while a person solves the CAPTCHA in a headed browser.

    We do NOT touch the CAPTCHA ourselves — we only poll until either the widget is
    gone or a solved response token appears (i.e. the human completed it), or the
    timeout expires. Returns the final HTML. Requires a headed browser so the person
    can see and interact with the page.
    """
    import time

    from .blockdetect import is_captcha

    secs = int(timeout_ms / 1000)
    print(
        "\n" + "=" * 68 +
        f"\n  CAPTCHA detected{(' at ' + url) if url else ''}."
        f"\n  A browser window is open - please solve the CAPTCHA there."
        f"\n  Waiting up to {secs}s for you to complete it...\n" + "=" * 68,
        flush=True,
    )
    deadline = time.time() + timeout_ms / 1000.0
    content = ""
    while time.time() < deadline:
        try:
            content = page.content()
        except Exception:
            break
        if not is_captcha(content) or _challenge_token_present(page):
            print("  CAPTCHA solved - continuing.\n", flush=True)
            return content
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)
    print("  Manual solve window expired - treating as blocked.\n", flush=True)
    return content


def wait_for_clearance(page, timeout_ms: int = 20000) -> str:
    """Wait for a challenge interstitial ("Just a moment...") to clear.

    Polls the live DOM until it no longer looks like a challenge page, attempting a
    Turnstile checkbox click along the way. Returns the final page HTML (cleared if
    it succeeded, otherwise the last interstitial so callers can still detect block).
    """
    import time

    from .blockdetect import is_challenge_interstitial

    deadline = time.time() + timeout_ms / 1000.0
    content = ""
    attempts = 0
    while time.time() < deadline:
        try:
            content = page.content()
        except Exception:
            break
        if not is_challenge_interstitial(content):
            return content  # cleared
        if attempts == 1:  # give the auto-solve one cycle, then try the checkbox
            _try_click_turnstile(page)
        attempts += 1
        try:
            page.wait_for_timeout(1500)
        except Exception:
            time.sleep(1.5)
    return content
