from pathlib import Path

from zero.traffic.store import CapturedRequest, TrafficStore, new_request_id


def _r(**kw) -> CapturedRequest:
    base = dict(
        id=new_request_id(),
        host="example.com",
        method="GET",
        url="https://example.com/x",
        status=200,
    )
    base.update(kw)
    return CapturedRequest(**base)


def test_add_dedupes_by_id():
    s = TrafficStore()
    a = _r()
    s.add(a)
    s.add(a)  # same id, ignored
    assert len(s) == 1


def test_search_by_host():
    s = TrafficStore()
    s.add(_r(host="a.com"))
    s.add(_r(host="b.com"))
    out = s.search(host="a.com")
    assert len(out) == 1 and out[0].host == "a.com"


def test_search_combined_filters():
    s = TrafficStore()
    s.add(_r(host="a.com", url="https://a.com/orders/1", status=200, method="GET"))
    s.add(_r(host="a.com", url="https://a.com/orders/2", status=403, method="PUT"))
    s.add(_r(host="a.com", url="https://a.com/items/1", status=200, method="GET"))
    out = s.search(host="a.com", url_substring="orders", status=200)
    assert len(out) == 1
    assert "orders/1" in out[0].url


def test_search_by_text_matches_headers_and_body():
    s = TrafficStore()
    s.add(_r(response_body='{"flag":"value"}'))
    s.add(_r(response_body="nothing here"))
    out = s.search(text="flag")
    assert len(out) == 1


def test_search_by_content_type():
    s = TrafficStore()
    s.add(_r(response_content_type="application/json"))
    s.add(_r(response_content_type="text/html"))
    out = s.search(content_type="json")
    assert len(out) == 1


def test_hosts_dedup():
    s = TrafficStore()
    s.add(_r(host="a.com"))
    s.add(_r(host="a.com"))
    s.add(_r(host="b.com"))
    assert s.hosts() == ["a.com", "b.com"]


def test_persist_and_load(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    s = TrafficStore(persist_path=p)
    s.add(_r(url="https://example.com/a"))
    s.add(_r(url="https://example.com/b"))
    loaded = TrafficStore.load_jsonl(p)
    assert len(loaded) == 2
    assert {r.url for r in loaded.all()} == {"https://example.com/a", "https://example.com/b"}
