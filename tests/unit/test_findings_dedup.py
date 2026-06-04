from zero.findings.dedup import dedup_hash, normalize_endpoint
from zero.findings.schema import Finding


def _f(**kw) -> Finding:
    base = dict(
        title="t", summary="s", endpoint="/orders/1041", method="GET",
        cwe="CWE-639", evidence="e", payload_shape="http_replay/tampered-id",
    )
    base.update(kw)
    return Finding(**base)


def test_normalize_endpoint_int_segment():
    assert normalize_endpoint("/orders/1041") == "/orders/{id}"
    assert normalize_endpoint("/v2/orders/9999/items/1") == "/v2/orders/{id}/items/{id}"


def test_normalize_endpoint_uuid():
    e = normalize_endpoint("/users/3b9da70a-5b89-4b85-9c5c-9e7d24e57e4b/orders/9")
    assert e == "/users/{uuid}/orders/{id}"


def test_normalize_endpoint_strips_method_prefix():
    assert normalize_endpoint("GET /orders/1") == "/orders/{id}"


def test_normalize_endpoint_query_string():
    assert normalize_endpoint("/search?q=abc&page=1") == "/search?page={v}&q={v}"


def test_same_endpoint_same_cwe_same_shape_dedups():
    a = _f(endpoint="/orders/1041")
    b = _f(endpoint="/orders/2018")  # different numeric ID
    assert dedup_hash(a) == dedup_hash(b)


def test_different_cwe_does_not_dedup():
    a = _f(cwe="CWE-639")
    b = _f(cwe="CWE-89")
    assert dedup_hash(a) != dedup_hash(b)


def test_different_method_does_not_dedup():
    a = _f(method="GET")
    b = _f(method="PUT")
    assert dedup_hash(a) != dedup_hash(b)


def test_hash_is_deterministic():
    a = _f()
    assert dedup_hash(a) == dedup_hash(a)
