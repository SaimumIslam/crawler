from crawler.fetch.blockdetect import is_blocked


def test_status_based_block():
    assert is_blocked(403, "<html>ok</html>")
    assert is_blocked(429, "")
    assert not is_blocked(200, "<html><body>lots of real content here</body></html>")


def test_cloudflare_challenge_detected():
    html = "<html><head><title>Just a moment...</title></head>" \
           "<body>Checking your browser before accessing.</body></html>"
    assert is_blocked(200, html)


def test_datadome_detected():
    assert is_blocked(200, "<html>datadome captcha please verify you are human</html>")


def test_normal_page_not_blocked():
    html = "<html><body>" + ("content " * 500) + "</body></html>"
    assert not is_blocked(200, html)
