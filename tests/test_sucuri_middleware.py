from __future__ import annotations

import base64

from scrapy import Request
from scrapy.http import HtmlResponse

from lgd_scraper.middlewares import (
    SucuriCookieChallengeMiddleware,
    parse_sucuri_cookie,
)


def challenge_html() -> str:
    decoded = (
        "f=String.fromCharCode(48)+'0'+'9'+'c'+'b'+'8'+'7'+'2'+'0'+'9'+'3'+'1'"
        "+'1'+'e'+'3'+'9'+'d'+'b'+'b'+'7'+'5'+'b'+'1'+'f'+'a'+'5'+'e'+'f'+'d'"
        "+'6'+'e'+'1';"
        "document.cookie='s'+'u'+'c'+'u'+'r'+'i'+'_'+'c'+'l'+'o'+'u'+'d'+'p'"
        "+'r'+'o'+'x'+'y'+'_'+'u'+'u'+'i'+'d'+'_'+'3'+'3'+'3'+'3'+'0'+'c'"
        "+'9'+'2'+'a'+'='+f+';path=/;max-age=86400;SameSite=Lax;Secure';"
        "location.reload();"
    )
    payload = base64.b64encode(decoded.encode()).decode()
    return (
        "<html><script>var sucuri_cloudproxy_js='',"
        f"S='{payload}';eval('challenge');</script></html>"
    )


def test_parse_sucuri_cookie_decodes_only_restricted_expression():
    assert parse_sucuri_cookie(challenge_html()) == (
        "sucuri_cloudproxy_uuid_33330c92a",
        "009cb87209311e39dbb75b1fa5efd6e1",
    )
    assert parse_sucuri_cookie("<html>ordinary redirect</html>") is None


def test_middleware_retries_challenge_with_cookie_and_without_cache():
    request = Request("https://example.test/product/ring/")
    response = HtmlResponse(
        request.url,
        status=307,
        body=challenge_html().encode(),
        encoding="utf-8",
        request=request,
    )
    retry = SucuriCookieChallengeMiddleware().process_response(request, response)

    assert isinstance(retry, Request)
    assert retry.dont_filter is True
    assert retry.meta["dont_cache"] is True
    assert retry.meta["sucuri_challenge_retries"] == 1
    assert retry.cookies == {
        "sucuri_cloudproxy_uuid_33330c92a": "009cb87209311e39dbb75b1fa5efd6e1"
    }
