from app.urls import normalize_base_url, page_identity, sanitize_url


def test_identity_keeps_meaningful_query_and_drops_noise():
    a = page_identity("HTTP://Shop.Example.com:80/orders/1001/?utm_source=x&tab=billing&token=abc#top")
    b = page_identity("http://shop.example.com/orders/2002?tab=billing")
    assert a == b == "http://shop.example.com/orders/{id}?tab=billing"


def test_identity_distinguishes_meaningful_params_and_hash_routes():
    assert page_identity("http://a.test/p?tab=1") != page_identity("http://a.test/p?tab=2")
    assert page_identity("http://a.test/p?b=2&a=1") == page_identity("http://a.test/p?a=1&b=2")
    assert page_identity("http://a.test/#/users/42") == "http://a.test/#/users/{id}"
    assert page_identity("http://a.test/page#section") == "http://a.test/page"


def test_identity_templates_uuids_and_keeps_slugs():
    assert page_identity("http://a.test/u/3f2b8a8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b/edit") == "http://a.test/u/{id}/edit"
    assert page_identity("http://a.test/blog/introducing-our-new-feature") == "http://a.test/blog/introducing-our-new-feature"


def test_sanitize_redacts_credentials_but_keeps_context():
    s = sanitize_url("https://user:pw@a.test/cb?code=xyz&session_id=1&access_token=t&page=2")
    assert "pw@" not in s and "xyz" not in s and "access_token=REDACTED" in s and "page=2" in s


def test_normalize_base_url():
    assert normalize_base_url("localhost:3000") == "http://localhost:3000/"
    assert normalize_base_url("ftp://x.test") is None
    assert normalize_base_url("http://") is None
    assert normalize_base_url("  ") is None


def test_identity_templates_id_params_but_not_other_numbers():
    assert page_identity("http://a.test/product?id=17") == page_identity("http://a.test/product?id=99")
    assert page_identity("http://a.test/o?order_id=1") == "http://a.test/o?order_id={id}"
    assert page_identity("http://a.test/wizard?step=1") != page_identity("http://a.test/wizard?step=2")
