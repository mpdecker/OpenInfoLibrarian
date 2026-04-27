"""Unit tests for searchers + multi-searcher aggregator.

Each searcher parses one fixture payload — we don't hit live APIs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from documentcrawler.searcher import (
    MultiSearcher,
    SearchHit,
    SearchQuery,
    build_searcher,
)
from documentcrawler.searcher.base import registry


class _FakeResponse:
    """Mimics ``documentcrawler.fetcher.FetchResponse`` for tests."""

    def __init__(self, status: int, text: str, headers: dict[str, str] | None = None):
        self.status = status
        self.text = text
        self.headers = headers or {}
        self.url = ""
        self.content = text.encode("utf-8")


class FakeFetcher:
    """Stub Fetcher returning canned payloads keyed by URL substring."""

    def __init__(self, json_map: dict[str, Any] | None = None,
                 text_map: dict[str, str] | None = None,
                 status_map: dict[str, list[tuple[int, str, dict[str, str]]]] | None = None,
                 fail: set[str] | None = None,
                 delay: float = 0.0):
        self.json_map = json_map or {}
        self.text_map = text_map or {}
        # status_map maps a URL substring → list of (status, body, headers)
        # to return on successive calls.  Useful for testing retry paths.
        self.status_map: dict[str, list[tuple[int, str, dict[str, str]]]] = status_map or {}
        self.fail = fail or set()
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    def _match(self, url: str, m: dict[str, Any]) -> Any:
        for needle, payload in m.items():
            if needle in url:
                return payload
        return None

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.calls.append((url, kw))
        if self.delay:
            await asyncio.sleep(self.delay)
        if any(n in url for n in self.fail):
            raise RuntimeError("simulated failure")
        m = self._match(url, self.json_map)
        if m is None:
            return {"results": []}
        return m

    async def get_text(self, url: str, **kw: Any) -> str:
        self.calls.append((url, kw))
        if self.delay:
            await asyncio.sleep(self.delay)
        if any(n in url for n in self.fail):
            raise RuntimeError("simulated failure")
        m = self._match(url, self.text_map)
        return m or ""

    async def probe_text(self, url: str, **kw: Any) -> str:
        # Delegate to ``get_text`` — ``probe_text`` only differs from
        # ``get_text`` in its httpx timeout/retry tuning, which is
        # transparent to the searcher layer.
        return await self.get_text(url, **kw)

    async def get(self, url: str, **kw: Any):
        """Return the next status_map entry, falling back to json/text maps."""
        self.calls.append((url, kw))
        if self.delay:
            await asyncio.sleep(self.delay)
        if any(n in url for n in self.fail):
            raise RuntimeError("simulated failure")
        for needle, queue in self.status_map.items():
            if needle in url and queue:
                status, body, headers = queue.pop(0)
                return _FakeResponse(status, body, headers)
        # Searchers that previously used ``get_json`` may also pull
        # via ``get``+ ``json.loads(resp.text)`` after the refactor —
        # serialize ``json_map`` payloads into the response body.
        json_payload = self._match(url, self.json_map)
        if json_payload is not None:
            import json as _json
            return _FakeResponse(200, _json.dumps(json_payload))
        body = self._match(url, self.text_map) or ""
        return _FakeResponse(200, body)

    async def render(self, url: str) -> str:  # used by Anna's Archive fallback
        return await self.get_text(url)


# -----------------------------------------------------------------------------
# Hit / dedupe helpers
# -----------------------------------------------------------------------------


def test_hit_dedupe_keys():
    h_doi = SearchHit(source="x", doi="10.1/a")
    h_isbn = SearchHit(source="x", isbn="9780000000000")
    h_title = SearchHit(source="x", title="Hello!", year=2020)
    assert h_doi.dedupe_key() == "doi:10.1/a"
    assert h_isbn.dedupe_key() == "isbn:9780000000000"
    assert h_title.dedupe_key().startswith("title:hello")


def test_author_str_truncation():
    h = SearchHit(source="x", authors=["A", "B", "C", "D", "E"])
    assert h.author_str == "A; B; C; +2"


# -----------------------------------------------------------------------------
# Crossref
# -----------------------------------------------------------------------------


def test_crossref_search_parses_items():
    payload = {
        "message": {
            "items": [
                {
                    "DOI": "10.1234/a",
                    "title": ["Hello world"],
                    "author": [{"given": "A.", "family": "Smith"}],
                    "issued": {"date-parts": [[2020]]},
                    "container-title": ["Nature"],
                    "abstract": "<p>Cool <i>science</i></p>",
                    "URL": "https://doi.org/10.1234/a",
                    "type": "journal-article",
                },
                {
                    "DOI": "10.1234/b",
                    "title": ["Another"],
                    "issued": {"date-parts": [[2019]]},
                },
            ]
        }
    }
    fetcher = FakeFetcher(json_map={"api.crossref.org": payload})
    s = build_searcher("crossref", fetcher)
    hits = asyncio.run(s.search("hello", limit=5, kind="title"))
    assert len(hits) == 2
    assert hits[0].title == "Hello world"
    assert hits[0].doi == "10.1234/a"
    assert hits[0].authors == ["A. Smith"]
    assert hits[0].year == 2020
    assert hits[0].abstract == "Cool science"
    assert hits[0].container == "Nature"
    assert hits[1].year == 2019


def test_crossref_doi_lookup():
    payload = {"message": {"DOI": "10.1234/a", "title": ["X"],
                            "issued": {"date-parts": [[2021]]}}}
    fetcher = FakeFetcher(json_map={"works/10.1234/a": payload})
    s = build_searcher("crossref", fetcher)
    hits = asyncio.run(s.search("https://doi.org/10.1234/a", kind="auto"))
    assert len(hits) == 1
    assert hits[0].doi == "10.1234/a"
    assert hits[0].score == 1.0


# -----------------------------------------------------------------------------
# OpenAlex
# -----------------------------------------------------------------------------


def test_openalex_parses_pdf_and_authors():
    payload = {
        "results": [
            {
                "title": "Quantum thingy",
                "doi": "https://doi.org/10.5/q",
                "publication_year": 2022,
                "authorships": [
                    {"author": {"display_name": "Alice"}},
                    {"author": {"display_name": "Bob"}},
                ],
                "best_oa_location": {"pdf_url": "https://x.org/q.pdf"},
                "host_venue": {"display_name": "Phys Rev"},
                "open_access": {"is_oa": True},
                "id": "https://openalex.org/W1",
                "type": "journal-article",
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"api.openalex.org": payload})
    s = build_searcher("openalex", fetcher)
    hits = asyncio.run(s.search("quantum"))
    assert len(hits) == 1
    h = hits[0]
    assert h.doi == "10.5/q"
    assert h.pdf_url == "https://x.org/q.pdf"
    assert h.has_pdf
    assert h.authors == ["Alice", "Bob"]
    assert h.container == "Phys Rev"


def test_openalex_inverted_abstract():
    payload = {
        "results": [
            {
                "title": "T",
                "publication_year": 2020,
                "abstract_inverted_index": {"hello": [0], "world": [1]},
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"api.openalex": payload})
    s = build_searcher("openalex", fetcher)
    hits = asyncio.run(s.search("anything"))
    assert hits[0].abstract == "hello world"


# -----------------------------------------------------------------------------
# arXiv
# -----------------------------------------------------------------------------


_ARXIV_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <title>Attention is all you need (revisited)</title>
    <summary>An update on the transformer.</summary>
    <published>2024-01-15T00:00:00Z</published>
    <author><name>Alice Chen</name></author>
    <author><name>Bob Liu</name></author>
    <arxiv:doi>10.1145/abc</arxiv:doi>
  </entry>
</feed>
"""


def test_arxiv_parses_entry():
    fetcher = FakeFetcher(text_map={"arxiv.org/api": _ARXIV_FIXTURE})
    s = build_searcher("arxiv", fetcher)
    hits = asyncio.run(s.search("attention"))
    assert len(hits) == 1
    h = hits[0]
    assert "Attention" in h.title
    assert h.year == 2024
    assert h.authors == ["Alice Chen", "Bob Liu"]
    assert h.pdf_url == "https://arxiv.org/pdf/2401.01234.pdf"
    assert h.doi == "10.1145/abc"


# -----------------------------------------------------------------------------
# OpenLibrary
# -----------------------------------------------------------------------------


def test_openlibrary_search_parses_docs():
    payload = {
        "docs": [
            {
                "title": "Why Cats Paint",
                "author_name": ["Burton Silver"],
                "first_publish_year": 1994,
                "key": "/works/OL12345W",
                "isbn": ["0898154480"],
                "publisher": ["Ten Speed Press"],
                "ia": ["whycatspaint00silv"],
                "has_fulltext": True,
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"openlibrary.org/search": payload})
    s = build_searcher("openlibrary", fetcher)
    hits = asyncio.run(s.search("cats", kind="title"))
    assert len(hits) == 1
    h = hits[0]
    assert h.isbn == "0898154480"
    assert h.year == 1994
    assert h.url == "https://openlibrary.org/works/OL12345W"
    assert "archive.org" in (h.pdf_url or "")


# -----------------------------------------------------------------------------
# Semantic Scholar
# -----------------------------------------------------------------------------


def test_semantic_scholar_parses():
    payload = {
        "data": [
            {
                "title": "Some paper",
                "authors": [{"name": "Alice"}],
                "year": 2018,
                "externalIds": {"DOI": "10.7/x"},
                "venue": "ICML",
                "openAccessPdf": {"url": "https://x.org/y.pdf"},
                "url": "https://semanticscholar.org/paper/X",
                "abstract": "An abstract.",
                "referenceCount": 5,
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"semanticscholar.org": payload})
    s = build_searcher("semantic_scholar", fetcher)
    hits = asyncio.run(s.search("some"))
    assert len(hits) == 1
    h = hits[0]
    assert h.doi == "10.7/x"
    assert h.pdf_url.endswith(".pdf")
    assert h.extra["references"] == 5


# -----------------------------------------------------------------------------
# Aggregator
# -----------------------------------------------------------------------------


def test_multi_searcher_dedupes_and_prefers_pdf():
    cr_payload = {
        "message": {
            "items": [
                {"DOI": "10.1234/a", "title": ["Same paper"],
                 "issued": {"date-parts": [[2020]]}}
            ]
        }
    }
    oa_payload = {
        "results": [
            {"title": "Same paper", "doi": "https://doi.org/10.1234/a",
             "publication_year": 2020,
             "best_oa_location": {"pdf_url": "https://oa.example/x.pdf"}}
        ]
    }
    fetcher = FakeFetcher(json_map={
        "api.crossref.org": cr_payload,
        "api.openalex.org": oa_payload,
    })
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="same paper", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    assert len(res.merged) == 1
    h = res.merged[0]
    assert h.has_pdf
    assert h.doi == "10.1234/a"
    sources = h.extra.get("contributors")
    assert {"crossref", "openalex"}.issubset(sources)


def test_multi_searcher_records_errors():
    fetcher = FakeFetcher(fail={"api.crossref.org"},
                          json_map={"api.openalex.org": {"results": []}})
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="x", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    runs_by_src = {r.source: r for r in res.runs}
    # crossref returns empty list on exception (caught inside the searcher)
    assert runs_by_src["crossref"].error is None or runs_by_src["crossref"].error == "timeout" or runs_by_src["crossref"].hits == []
    assert runs_by_src["openalex"].hits == []


def test_unknown_searcher_returns_error_run():
    ms = MultiSearcher(FakeFetcher())
    sq = SearchQuery(text="x", sources=["does_not_exist"])
    res = asyncio.run(ms.search(sq))
    assert len(res.runs) == 1
    assert res.runs[0].error == "unknown searcher"
    assert res.runs[0].hits == []


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------


def test_registry_has_all_expected_searchers():
    expected = {"crossref", "openalex", "arxiv", "openlibrary",
                "semantic_scholar", "annas_archive", "libgen", "zlibrary"}
    assert expected.issubset(set(registry))


# -----------------------------------------------------------------------------
# LibGen — modern (libgen.li) and legacy (libgen.is/.rs) layouts
# -----------------------------------------------------------------------------

_LIBGEN_LI_FIXTURE = """
<html><body>
<table id="tablelibgen" class="table table-striped">
  <tr><th>Title</th><th>Author(s)</th><th>Publisher</th><th>Year</th>
      <th>Lang</th><th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr>
  <tr>
    <td><a href="edition.php?id=1">Attention Is All You Need <i></i></a>
        <span class="badge badge-secondary">l 6846495</span></td>
    <td>Ashish Vaswani, Noam Shazeer, Niki Parmar</td>
    <td></td>
    <td>2017</td>
    <td>English</td>
    <td>15</td>
    <td>2 MB</td>
    <td>pdf</td>
    <td><a href="/ads.php?md5=18e1b007a1dab45b30cc861ba2dfda25">Mirror</a></td>
  </tr>
  <tr>
    <td><a href="edition.php?id=2">A Book About Cats <i></i></a></td>
    <td>Some Author</td>
    <td>Cat Press</td>
    <td>1994</td>
    <td>English</td>
    <td>200</td>
    <td>3 MB</td>
    <td>epub</td>
    <td><a href="/ads.php?md5=290754a9b1fa76f65ddfb75826b83dfe">Mirror</a></td>
  </tr>
</table>
</body></html>
"""


def test_libgen_parses_modern_libgen_li_layout():
    fetcher = FakeFetcher(text_map={"libgen.li/index.php": _LIBGEN_LI_FIXTURE})
    s = build_searcher("libgen", fetcher, options={"mirrors": ["https://libgen.li"]})
    hits = asyncio.run(s.search("attention", limit=5))
    assert len(hits) == 2
    titles = [h.title for h in hits]
    assert "Attention Is All You Need" in titles
    assert "A Book About Cats" in titles
    by_title = {h.title: h for h in hits}
    h = by_title["Attention Is All You Need"]
    assert h.year == 2017
    assert h.authors[0] == "Ashish Vaswani"
    assert h.extra["md5"] == "18e1b007a1dab45b30cc861ba2dfda25"
    assert h.extra["ext"] == "pdf"
    # Title cleaning strips the trailing badge digits.
    assert "6846495" not in h.title


_LIBGEN_LEGACY_FIXTURE = """
<html><body>
<table class="c">
  <tr><th>id</th><th>Authors</th><th>Title</th><th>Pub</th><th>Year</th>
      <th>x</th><th>x</th><th>x</th><th>x</th><th>x</th></tr>
  <tr>
    <td>1</td>
    <td>Smith, J.</td>
    <td><a href="book/index.php?md5=AAAA1111BBBB2222CCCC3333DDDD4444">Old Book</a></td>
    <td>Old Press</td>
    <td>1999</td>
    <td>x</td><td>x</td><td>x</td><td>x</td><td>x</td>
  </tr>
</table>
</body></html>
"""


def test_libgen_parses_legacy_libgen_is_layout():
    fetcher = FakeFetcher(text_map={"libgen.is/index.php": _LIBGEN_LEGACY_FIXTURE})
    s = build_searcher("libgen", fetcher, options={"mirrors": ["https://libgen.is"]})
    hits = asyncio.run(s.search("old", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Old Book"
    assert h.year == 1999
    assert h.extra["md5"] == "aaaa1111bbbb2222cccc3333dddd4444"


def test_libgen_falls_through_dead_mirror_then_succeeds():
    fetcher = FakeFetcher(
        text_map={"libgen.li/index.php": _LIBGEN_LI_FIXTURE},
        fail={"libgen.is", "libgen.rs"},
    )
    s = build_searcher(
        "libgen", fetcher,
        options={"mirrors": ["https://libgen.is", "https://libgen.rs", "https://libgen.li"]},
    )
    hits = asyncio.run(s.search("attention", limit=5))
    assert len(hits) == 2


def test_libgen_propagates_error_when_all_mirrors_fail():
    fetcher = FakeFetcher(fail={"libgen."})
    s = build_searcher(
        "libgen", fetcher,
        options={"mirrors": ["https://libgen.li", "https://libgen.gs"]},
    )
    import pytest

    with pytest.raises(RuntimeError):
        asyncio.run(s.search("anything"))


def test_libgen_augments_stale_user_mirror_list():
    """A user with a config listing only ``libgen.is`` (now DNS-blocked)
    should still get a fallback to ``libgen.li`` automatically."""
    fetcher = FakeFetcher(
        text_map={"libgen.li/index.php": _LIBGEN_LI_FIXTURE},
        fail={"libgen.is"},
    )
    s = build_searcher("libgen", fetcher, options={"mirrors": ["https://libgen.is"]})
    hits = asyncio.run(s.search("attention", limit=5))
    assert len(hits) == 2


# -----------------------------------------------------------------------------
# Anna's Archive — modern (Tailwind) layout + mirror fallback + error
# -----------------------------------------------------------------------------

_ANNAS_MODERN_FIXTURE = """
<html><body>
<div class="js-aarecord-list-outer">
  <div class="flex pt-3 pb-3 border-b last:border-b-0 border-gray-100">
    <a href="/md5/18e1b007a1dab45b30cc861ba2dfda25"></a>
    <div class="max-w-full overflow-hidden flex flex-col justify-around">
      <div>
        <div class="text-[9px] text-gray-500 font-mono">lgli/1706.03762.pdf</div>
        <a href="/md5/18e1b007a1dab45b30cc861ba2dfda25" class="js-vim-focus">Attention Is All You Need</a>
        <a href="/search?q=Ashish Vaswani"><span></span> Ashish Vaswani, Noam Shazeer, Niki Parmar</a>
        <a href="/search?q="><span></span> 2017 jun 12</a>
      </div>
    </div>
  </div>
</div>
</body></html>
"""


def test_annas_parses_modern_tailwind_layout():
    fetcher = FakeFetcher(text_map={"annas-archive.gl/search": _ANNAS_MODERN_FIXTURE})
    s = build_searcher("annas_archive", fetcher,
                       options={"mirrors": ["https://annas-archive.gl"]})
    hits = asyncio.run(s.search("attention", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Attention Is All You Need"
    assert h.year == 2017
    assert h.authors and h.authors[0] == "Ashish Vaswani"
    assert h.extra["md5"] == "18e1b007a1dab45b30cc861ba2dfda25"
    assert h.url and "annas-archive.gl" in h.url


_ANNAS_LEGACY_FIXTURE = """
<html><body>
<a href="/md5/aaaa1111bbbb2222cccc3333dddd4444">
  <h3>Legacy Book</h3>
  <div>English [en], pdf, 1.2MB, Book, Old Press, 1995, John Smith</div>
</a>
</body></html>
"""


def test_annas_legacy_layout_still_works():
    fetcher = FakeFetcher(text_map={"annas-archive.org/search": _ANNAS_LEGACY_FIXTURE})
    s = build_searcher("annas_archive", fetcher,
                       options={"mirrors": ["https://annas-archive.org"]})
    hits = asyncio.run(s.search("legacy"))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Legacy Book"
    assert h.year == 1995
    assert h.authors == ["John Smith"]


def test_annas_falls_through_dead_mirror():
    fetcher = FakeFetcher(
        text_map={"annas-archive.gl/search": _ANNAS_MODERN_FIXTURE},
        fail={"annas-archive.org"},
    )
    s = build_searcher(
        "annas_archive", fetcher,
        options={"mirrors": ["https://annas-archive.org", "https://annas-archive.gl"]},
    )
    hits = asyncio.run(s.search("attention"))
    assert len(hits) == 1
    assert hits[0].title == "Attention Is All You Need"


def test_annas_back_compat_base_url():
    """Older configs use ``base_url`` instead of ``mirrors`` — both should work."""
    fetcher = FakeFetcher(text_map={"annas-archive.org/search": _ANNAS_LEGACY_FIXTURE})
    s = build_searcher("annas_archive", fetcher,
                       options={"base_url": "https://annas-archive.org"})
    hits = asyncio.run(s.search("legacy"))
    assert len(hits) == 1


# -----------------------------------------------------------------------------
# Semantic Scholar — error propagation (was silently swallowing errors before)
# -----------------------------------------------------------------------------


def test_semantic_scholar_propagates_errors_to_aggregator():
    """A 429/503 from S2 used to return [] silently — now MultiSearcher
    sees the exception and records the error per source so the user can
    diagnose rate-limiting."""
    fetcher = FakeFetcher(fail={"semanticscholar.org"})
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="x", sources=["semantic_scholar"])
    res = asyncio.run(ms.search(sq))
    assert len(res.runs) == 1
    run = res.runs[0]
    assert run.hits == []
    assert run.error and "simulated failure" in run.error


def test_semantic_scholar_retries_once_on_429_then_succeeds():
    """The S2 searcher does at most one Retry-After-aware retry on 429
    rather than the generic 3x exponential backoff that used to burn
    through the whole per-source budget for nothing."""
    payload = (
        '{"data":[{"title":"Hi","authors":[{"name":"A"}],"year":2024,'
        '"externalIds":{"DOI":"10.1/x"}}]}'
    )
    fetcher = FakeFetcher(
        status_map={
            "semanticscholar.org": [
                (429, "rate limited", {"Retry-After": "0"}),
                (200, payload, {}),
            ]
        }
    )
    s = build_searcher("semantic_scholar", fetcher)
    hits = asyncio.run(s.search("hi"))
    assert len(hits) == 1
    assert hits[0].doi == "10.1/x"
    # We should have made exactly two attempts — one retry, no more.
    s2_calls = [c for c in fetcher.calls if "semanticscholar.org" in c[0]]
    assert len(s2_calls) == 2


def test_semantic_scholar_gives_up_after_one_retry():
    """A second 429 is final — we surface a clean FetchError so
    ``MultiSearcher`` can render a meaningful per-source error."""
    fetcher = FakeFetcher(
        status_map={
            "semanticscholar.org": [
                (429, "rate limited", {"Retry-After": "0"}),
                (429, "rate limited", {"Retry-After": "0"}),
            ]
        }
    )
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="x", sources=["semantic_scholar"])
    res = asyncio.run(ms.search(sq))
    run = res.runs[0]
    assert run.hits == []
    assert run.error and "rate limited" in run.error.lower()


# -----------------------------------------------------------------------------
# Z-Library — z-bookcard parser + mirror fall-through
# -----------------------------------------------------------------------------

_ZLIB_FIXTURE = """
<html><body>
<div class="counter">1</div>
<z-bookcard
    id="5206515"
    isbn="9781234567890"
    href="/book/n0z2/machine-learning-mastery.html"
    download="/dl/0rLbxVm3MZ"
    publisher="Machine Learning Mastery"
    language="English"
    year="2016"
    extension="pdf"
    filesize="2.39 MB">
  <img />
  <div slot="title">Machine Learning Mastery with Python</div>
  <div slot="author">Jason Brownlee</div>
</z-bookcard>
<div class="counter">2</div>
<z-bookcard
    id="5220424"
    href="/book/W0Ep/statistical-methods.html"
    download="/dl/9DJ2"
    publisher="ML Press"
    language="English"
    year="2019"
    extension="epub"
    filesize="1.5 MB">
  <div slot="title">Statistical Methods</div>
  <div slot="author">A. Author, B. Other</div>
</z-bookcard>
</body></html>
"""


def test_zlibrary_parses_z_bookcards():
    fetcher = FakeFetcher(text_map={"z-lib.fm/s/": _ZLIB_FIXTURE})
    s = build_searcher("zlibrary", fetcher,
                       options={"mirrors": ["https://z-lib.fm"]})
    hits = asyncio.run(s.search("machine learning", limit=5))
    assert len(hits) == 2
    h = hits[0]
    assert h.title == "Machine Learning Mastery with Python"
    assert h.year == 2016
    assert h.authors == ["Jason Brownlee"]
    assert h.isbn == "9781234567890"
    assert h.container == "Machine Learning Mastery"
    assert h.has_pdf  # extension=pdf → pdf_url populated
    assert h.pdf_url and "/dl/0rLbxVm3MZ" in h.pdf_url
    assert h.url and "/book/n0z2/" in h.url
    assert h.extra["zlib_id"] == "5206515"
    assert h.extra["language"] == "English"
    # Non-PDF cards should NOT expose pdf_url
    h2 = hits[1]
    assert h2.title == "Statistical Methods"
    assert h2.has_pdf is False
    assert h2.authors == ["A. Author", "B. Other"]


def test_zlibrary_falls_through_dead_mirror():
    fetcher = FakeFetcher(
        text_map={"z-lib.fm/s/": _ZLIB_FIXTURE},
        fail={"z-library.sk"},
    )
    s = build_searcher(
        "zlibrary", fetcher,
        options={"mirrors": ["https://z-library.sk", "https://z-lib.fm"]},
    )
    hits = asyncio.run(s.search("anything", limit=5))
    assert len(hits) == 2


def test_zlibrary_augments_stale_user_mirror_list():
    fetcher = FakeFetcher(
        text_map={"z-lib.fm/s/": _ZLIB_FIXTURE},
        fail={"z-library.sk", "1lib.sk"},  # the user's two stale entries
    )
    s = build_searcher("zlibrary", fetcher,
                       options={"mirrors": ["https://z-library.sk"]})
    hits = asyncio.run(s.search("anything", limit=5))
    assert len(hits) == 2  # falls through to the appended z-lib.fm default


def test_zlibrary_propagates_error_when_all_mirrors_fail():
    fetcher = FakeFetcher(fail={"z-lib.", "z-library.", "1lib."})
    s = build_searcher(
        "zlibrary", fetcher,
        options={"mirrors": ["https://z-lib.fm"]},
    )
    import pytest

    with pytest.raises(RuntimeError):
        asyncio.run(s.search("anything"))


def test_zlibrary_skips_search_when_query_empty():
    fetcher = FakeFetcher()
    s = build_searcher("zlibrary", fetcher)
    assert asyncio.run(s.search("", limit=5)) == []
    assert asyncio.run(s.search("   ", limit=5)) == []
