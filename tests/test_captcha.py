"""CAPTCHA detection + config policy tests (no auto-solving is implemented)."""
import pytest
from pydantic import ValidationError

from crawler.config import CaptchaSpec, SiteTemplate
from crawler.fetch.blockdetect import captcha_vendor, is_captcha


def test_detect_recaptcha():
    html = '<div class="g-recaptcha" data-sitekey="x"></div>'
    assert captcha_vendor(html) == "recaptcha"
    assert is_captcha(html)


def test_detect_hcaptcha():
    assert captcha_vendor('<script src="https://js.hcaptcha.com/1/api.js">') == "hcaptcha"


def test_detect_turnstile():
    assert captcha_vendor('<div class="cf-turnstile" data-sitekey="x">') == "turnstile"


def test_detect_arkose():
    assert captcha_vendor("<script>funcaptcha init arkoselabs</script>") == "arkose"


def test_detect_image_text_captcha():
    assert captcha_vendor('<img src="/captcha.php"><input name="captcha">') == "image-text"


def test_plain_page_has_no_captcha():
    assert captcha_vendor("<html><body>just content</body></html>") is None
    assert not is_captcha("")


def test_captcha_spec_defaults_to_skip():
    t = SiteTemplate.model_validate(
        {"name": "t", "start_urls": ["https://e.com"],
         "extract": {"mode": "selectors", "fields": {"x": {"css": "a::text"}}}}
    )
    assert t.captcha.on_detect == "skip"
    assert t.captcha.manual_timeout_sec == 180.0


def test_captcha_mode_validated():
    with pytest.raises(ValidationError):
        CaptchaSpec(on_detect="solve")  # only skip/manual/stop allowed


def test_manual_mode_forces_headed_browser():
    from crawler.config import FetchSpec
    from crawler.fetch.escalation import TieredFetcher

    tf = TieredFetcher(FetchSpec(headless=True), site="t",
                       captcha=CaptchaSpec(on_detect="manual"))
    assert tf._browser_headless is False  # manual needs a visible window

    tf2 = TieredFetcher(FetchSpec(headless=True), site="t",
                        captcha=CaptchaSpec(on_detect="skip"))
    assert tf2._browser_headless is True
