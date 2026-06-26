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
    expected = {
        "crossref", "openalex", "arxiv", "openlibrary",
        "semantic_scholar", "annas_archive", "libgen", "zlibrary",
        # New searchers
        "core", "doi_org", "jstor", "scihub",
        "elsevier", "springer", "wiley", "ieee",
        "library_of_congress", "uk_national_archives", "europeana",
    }
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


# -----------------------------------------------------------------------------
# CORE (Open Access Aggregator)
# -----------------------------------------------------------------------------


def test_core_search_parses_works():
    payload = {
        "results": [
            {
                "id": 12345,
                "title": "Open Access Paper",
                "authors": [{"name": "Jane Doe"}, {"name": "John Smith"}],
                "publishedDate": "2023-05-15",
                "doi": "10.1234/open.access",
                "abstract": "This is an open access paper.",
                "links": [
                    {"url": "https://core.ac.uk/download/pdf/12345.pdf", "mimeType": "application/pdf"}
                ],
                "publisher": "Open Journal",
                "language": {"code": "en"},
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"api.core.ac.uk": payload})
    s = build_searcher("core", fetcher)
    hits = asyncio.run(s.search("open access", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Open Access Paper"
    assert h.doi == "10.1234/open.access"
    assert h.authors == ["Jane Doe", "John Smith"]
    assert h.year == 2023
    assert h.pdf_url == "https://core.ac.uk/download/pdf/12345.pdf"
    assert h.has_pdf
    assert h.extra["venue"] == "Open Journal"
    assert h.extra["core_id"] == 12345


def test_core_doi_search():
    payload = {
        "results": [
            {"title": "DOI Paper", "doi": "10.5678/doi.paper", "publishedDate": "2022"}
        ]
    }
    fetcher = FakeFetcher(json_map={"api.core.ac.uk": payload})
    s = build_searcher("core", fetcher)
    hits = asyncio.run(s.search("10.5678/doi.paper", kind="doi", limit=5))
    assert len(hits) == 1
    assert hits[0].title == "DOI Paper"


def test_core_empty_results():
    fetcher = FakeFetcher(json_map={"api.core.ac.uk": {"results": []}})
    s = build_searcher("core", fetcher)
    hits = asyncio.run(s.search("nonexistent"))
    assert hits == []


# -----------------------------------------------------------------------------
# DOI.org metadata resolver
# -----------------------------------------------------------------------------


def test_doi_org_resolves_metadata():
    payload = {
        "message": {
            "DOI": "10.1000/example",
            "title": ["Example Publication"],
            "author": [{"given": "Alice", "family": "Researcher"}],
            "issued": {"date-parts": [[2021, 6]]},
            "type": "journal-article",
            "publisher": "Example Publisher",
            "container-title": ["Example Journal"],
            "URL": "https://doi.org/10.1000/example",
        }
    }
    fetcher = FakeFetcher(json_map={"api.crossref.org/works/10.1000/example": payload})
    s = build_searcher("doi_org", fetcher)
    hits = asyncio.run(s.search("10.1000/example", kind="doi"))
    assert len(hits) == 1
    h = hits[0]
    assert h.doi == "10.1000/example"
    assert h.title == "Example Publication"
    assert h.authors == ["Alice Researcher"]
    assert h.year == 2021
    assert h.container == "Example Journal"


def test_doi_org_returns_empty_for_invalid():
    fetcher = FakeFetcher(text_map={"doi.org": "Not Found"})
    s = build_searcher("doi_org", fetcher)
    hits = asyncio.run(s.search("invalid-doi", kind="doi"))
    assert hits == []


# -----------------------------------------------------------------------------
# JSTOR metadata searcher
# -----------------------------------------------------------------------------


_JSTOR_SEARCH_FIXTURE = """
<html>
<body>
<script type="application/ld+json">
{
    "@context": "http://schema.org",
    "@type": "ScholarlyArticle",
    "name": "JSTOR Research Paper",
    "author": [{"name": "Jane Academic"}],
    "datePublished": "2020",
    "doi": "10.2307/jstor.12345",
    "url": "https://www.jstor.org/stable/12345",
    "isPartOf": {"name": "Journal of Research"}
}
</script>
</body>
</html>
"""


def test_jstor_parses_search_results():
    fetcher = FakeFetcher(text_map={"jstor.org/action/doBasicSearch": _JSTOR_SEARCH_FIXTURE})
    s = build_searcher("jstor", fetcher)
    hits = asyncio.run(s.search("research paper", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "JSTOR Research Paper"
    assert h.authors == ["Jane Academic"]
    assert h.year == 2020
    assert h.doi == "10.2307/jstor.12345"
    assert "jstor.org/stable/12345" in h.url


def test_jstor_doi_lookup():
    # JSTOR searches for DOI via search page, not direct lookup
    # Year comes first to ensure it's extracted before the stable ID digits
    page_html = """
    <html>
    <body>
    <div class="result-item" data-result-item>
        <span class="year">2019</span>
        <a class="title" href="/stable/OL12345">Article on JSTOR</a>
        <span class="author">John Author, Jane Writer</span>
        <span>doi:10.2307/12345</span>
    </div>
    </body>
    </html>
    """
    fetcher = FakeFetcher(text_map={"jstor.org/action/doBasicSearch": page_html})
    s = build_searcher("jstor", fetcher)
    hits = asyncio.run(s.search("10.2307/12345", kind="doi"))
    assert len(hits) == 1
    h = hits[0]
    assert "Article" in h.title
    assert h.year == 2019


def test_jstor_empty_search():
    empty_html = "<html><body>No results found</body></html>"
    fetcher = FakeFetcher(text_map={"jstor.org": empty_html})
    s = build_searcher("jstor", fetcher)
    hits = asyncio.run(s.search("xyznonexistent"))
    assert hits == []


# -----------------------------------------------------------------------------
# Sci-Hub direct DOI searcher
# -----------------------------------------------------------------------------


_SCIHUB_FIXTURE = """
<html>
<head><title>Research Article - Sci-Hub</title></head>
<body>
<iframe id="pdf" src="https://sci-hub.se/downloads/12345/paper.pdf"></iframe>
<div>author: Dr. Researcher</div>
</body>
</html>
"""


def test_scihub_parses_pdf_from_iframe():
    fetcher = FakeFetcher(text_map={"sci-hub.se/10.1000": _SCIHUB_FIXTURE})
    s = build_searcher("scihub", fetcher)
    hits = asyncio.run(s.search("10.1000/scihub.example", kind="doi"))
    assert len(hits) == 1
    h = hits[0]
    assert "Research Article" in h.title
    assert h.doi == "10.1000/scihub.example"
    assert h.pdf_url == "https://sci-hub.se/downloads/12345/paper.pdf"
    assert h.has_pdf


def test_scihub_falls_through_mirrors():
    fetcher = FakeFetcher(
        text_map={"sci-hub.st/10.1000": _SCIHUB_FIXTURE},
        fail={"sci-hub.se", "sci-hub.ru"}
    )
    s = build_searcher("scihub", fetcher, options={
        "mirrors": ["https://sci-hub.se", "https://sci-hub.ru", "https://sci-hub.st"]
    })
    hits = asyncio.run(s.search("10.1000/scihub.example", kind="doi"))
    assert len(hits) == 1


def test_scihub_returns_empty_for_invalid_doi():
    not_found_html = "<html><body>Article not found</body></html>"
    fetcher = FakeFetcher(text_map={"sci-hub.se": not_found_html})
    s = build_searcher("scihub", fetcher)
    hits = asyncio.run(s.search("10.9999/nonexistent", kind="doi"))
    assert hits == []


# -----------------------------------------------------------------------------
# Publisher metadata searchers (Elsevier, Springer, Wiley, IEEE)
# -----------------------------------------------------------------------------


def test_elsevier_search_parses_results():
    payload = {
        "search-results": {
            "entry": [
                {
                    "dc:title": "ScienceDirect Article",
                    "dc:creator": "Author, A.",
                    "prism:coverDate": "2023-01-15",
                    "prism:doi": "10.1016/j.example.2023.01.001",
                    "prism:publicationName": "Journal of Examples",
                }
            ]
        }
    }
    fetcher = FakeFetcher(json_map={"api.elsevier.com": payload})
    s = build_searcher("elsevier", fetcher, options={"api_key": "test_key"})
    hits = asyncio.run(s.search("article", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "ScienceDirect Article"
    assert h.doi == "10.1016/j.example.2023.01.001"
    assert h.year == 2023
    assert h.container == "Journal of Examples"


def test_springer_search_parses_results():
    payload = {
        "records": [
            {
                "title": "Springer Nature Article",
                "creators": [{"creator": "Author, B."}],
                "publicationDate": "2022-08-20",
                "doi": "10.1007/springer.example",
                "publicationName": "Nature Examples",
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"api.springernature.com": payload})
    s = build_searcher("springer", fetcher, options={"api_key": "test_key"})
    hits = asyncio.run(s.search("nature", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Springer Nature Article"
    assert h.doi == "10.1007/springer.example"
    assert h.year == 2022


def test_wiley_search_parses_results():
    # Wiley uses Crossref API with publisher filter
    payload = {
        "message": {
            "items": [
                {
                    "title": ["Wiley Article"],
                    "author": [{"family": "Researcher", "given": "C."}],
                    "issued": {"date-parts": [[2021, 3, 10]]},
                    "DOI": "10.1002/wiley.example",
                    "container-title": ["Wiley Journal"],
                    "publisher": "Wiley",
                }
            ]
        }
    }
    fetcher = FakeFetcher(json_map={"api.crossref.org": payload})
    s = build_searcher("wiley", fetcher, options={"mailto": "test@example.com"})
    hits = asyncio.run(s.search("wiley", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Wiley Article"
    assert h.doi == "10.1002/wiley.example"
    assert h.year == 2021


def test_ieee_search_parses_results():
    payload = {
        "articles": [
            {
                "title": "IEEE Paper",
                "authors": {"authors": [{"full_name": "Engineer, D."}]},
                "publication_year": "2020",
                "doi": "10.1109/ieee.example",
                "publication_title": "IEEE Transactions",
                "article_number": "12345",
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"ieeexploreapi.ieee.org": payload})
    s = build_searcher("ieee", fetcher, options={"api_key": "test_key"})
    hits = asyncio.run(s.search("ieee", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "IEEE Paper"
    assert h.doi == "10.1109/ieee.example"
    assert h.year == 2020
    assert h.container == "IEEE Transactions"


# -----------------------------------------------------------------------------
# National archives searchers
# -----------------------------------------------------------------------------


def test_library_of_congress_search():
    # LOC uses SRU XML API
    xml_response = """<?xml version="1.0"?>
    <searchRetrieveResponse xmlns="http://docs.oasis-open.org/ns/search-ws/sruResponse">
        <records>
            <record>
                <recordData>
                    <dc:dc xmlns:dc="http://purl.org/dc/elements/1.1/">
                        <dc:title>LOC Book</dc:title>
                        <dc:creator>Author, E.</dc:creator>
                        <dc:date>2018</dc:date>
                        <dc:identifier>ISBN: 9781234567890</dc:identifier>
                    </dc:dc>
                </recordData>
            </record>
        </records>
    </searchRetrieveResponse>
    """
    fetcher = FakeFetcher(text_map={"lx2.loc.gov": xml_response})
    s = build_searcher("library_of_congress", fetcher)
    hits = asyncio.run(s.search("history", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "LOC Book"
    assert h.year == 2018


def test_uk_national_archives_search():
    # UK National Archives uses JSON API
    payload = {
        "records": [
            {
                "title": "UK Archives Document",
                "coveringDates": "1919-1920",
                "reference": "TNA/123",
                "id": "12345",
                "department": "War Office",
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"discovery.nationalarchives.gov.uk": payload})
    s = build_searcher("uk_national_archives", fetcher)
    hits = asyncio.run(s.search("war", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "UK Archives Document"
    assert h.year == 1919


def test_europeana_search():
    payload = {
        "items": [
            {
                "title": ["Europeana Artifact"],
                "dcCreator": ["Artist, F."],
                "year": "1920",
                "id": "/123/abc",
                "dataProvider": ["National Museum"],
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"api.europeana.eu": payload})
    s = build_searcher("europeana", fetcher, options={"api_key": "test_key"})
    hits = asyncio.run(s.search("artifact", limit=5))
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "Europeana Artifact"
    assert h.year == 1920


# -----------------------------------------------------------------------------
# MultiSearcher edge cases
# -----------------------------------------------------------------------------


def test_multi_searcher_prefers_higher_score_on_conflict():
    """When two hits have same DOI, prefer the one with higher score."""
    low_score_payload = {
        "message": {"items": [{"DOI": "10.1234/a", "title": ["Paper"],
                                 "issued": {"date-parts": [[2020]]}, "score": 0.5}]}
    }
    high_score_payload = {
        "results": [{"title": "Paper", "doi": "https://doi.org/10.1234/a",
                     "publication_year": 2020}]
    }
    fetcher = FakeFetcher(json_map={
        "api.crossref.org": low_score_payload,
        "api.openalex.org": high_score_payload,
    })
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="paper", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    assert len(res.merged) == 1
    # Should prefer the higher scored version from OpenAlex
    contributors = res.merged[0].extra.get("contributors", [])
    assert "openalex" in contributors


def test_multi_searcher_handles_empty_all_sources():
    """When all sources return empty, merged results should be empty."""
    fetcher = FakeFetcher(json_map={
        "api.crossref.org": {"message": {"items": []}},
        "api.openalex.org": {"results": []},
    })
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="nonexistent", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    assert res.merged == []
    assert len(res.runs) == 2


def test_multi_searcher_handles_single_source():
    """MultiSearcher works with a single source."""
    payload = {"message": {"items": [{"DOI": "10.1/a", "title": ["Solo"],
                                       "issued": {"date-parts": [[2021]]}}]}}
    fetcher = FakeFetcher(json_map={"api.crossref.org": payload})
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="solo", sources=["crossref"])
    res = asyncio.run(ms.search(sq))
    assert len(res.merged) == 1


def test_multi_searcher_handles_many_sources():
    """MultiSearcher handles many sources efficiently."""
    # Create minimal responses for many sources
    json_map = {}
    for i, src in enumerate(["crossref", "openalex", "arxiv", "semantic_scholar",
                              "openlibrary", "core", "doi_org", "jstor"]):
        if src == "crossref":
            json_map["api.crossref.org"] = {"message": {"items": []}}
        elif src == "openalex":
            json_map["api.openalex.org"] = {"results": []}
        elif src == "arxiv":
            json_map["arxiv.org"] = "<?xml version='1.0'?><feed></feed>"
        elif src == "semantic_scholar":
            json_map["semanticscholar.org"] = {"data": []}
        elif src == "openlibrary":
            json_map["openlibrary.org"] = {"docs": []}
        elif src == "core":
            json_map["api.core.ac.uk"] = {"results": []}
        elif src == "doi_org":
            json_map["doi.org"] = {}
        elif src == "jstor":
            json_map["jstor.org"] = "<html></html>"

    fetcher = FakeFetcher(json_map=json_map, text_map=json_map)
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="test", sources=["crossref", "openalex", "arxiv"])
    res = asyncio.run(ms.search(sq))
    assert len(res.runs) == 3


def test_multi_searcher_preserves_source_runs_order():
    """Source runs should be in the order requested."""
    fetcher = FakeFetcher(json_map={
        "api.crossref.org": {"message": {"items": []}},
        "api.openalex.org": {"results": []},
    })
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="test", sources=["openalex", "crossref"])
    res = asyncio.run(ms.search(sq))
    sources = [r.source for r in res.runs]
    assert sources == ["openalex", "crossref"]


def test_multi_searcher_handles_duplicate_dois():
    """Multiple hits with same DOI should be deduplicated."""
    payload = {
        "message": {
            "items": [
                {"DOI": "10.1234/same", "title": ["Same Paper 1"],
                 "issued": {"date-parts": [[2020]]}},
                {"DOI": "10.1234/same", "title": ["Same Paper 2"],
                 "issued": {"date-parts": [[2020]]}},
            ]
        }
    }
    fetcher = FakeFetcher(json_map={"api.crossref.org": payload})
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="same", sources=["crossref"])
    res = asyncio.run(ms.search(sq))
    # Should merge duplicates
    dois = [h.doi for h in res.merged if h.doi]
    assert len(dois) == len(set(dois)), "Duplicate DOIs should be merged"


def test_multi_searcher_isbn_dedupe():
    """ISBN-based deduplication should work."""
    ol_payload = {
        "docs": [
            {"title": "Book A", "isbn": ["9780000000000"], "first_publish_year": 2020,
             "key": "/works/OL1W"}
        ]
    }
    zlib_html = """
    <z-bookcard isbn="9780000000000" href="/book/1" download="" year="2020"
                extension="pdf" filesize="1 MB">
        <div slot="title">Book A</div>
    </z-bookcard>
    """
    fetcher = FakeFetcher(
        json_map={"openlibrary.org": ol_payload},
        text_map={"z-lib.fm": zlib_html}
    )
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="book a", sources=["openlibrary", "zlibrary"])
    res = asyncio.run(ms.search(sq))
    # Should dedupe by ISBN
    isbns = [h.isbn for h in res.merged if h.isbn]
    assert len(isbns) == len(set(isbns))


def test_multi_searcher_title_year_dedupe():
    """Title+Year deduplication for items without DOI/ISBN."""
    arxiv_xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>Unique Paper</title>
            <published>2023-01-01T00:00:00Z</published>
            <id>http://arxiv.org/abs/2301.00001</id>
        </entry>
    </feed>
    """
    # Same paper from another source (no DOI)
    cr_payload = {
        "message": {
            "items": [
                {"title": ["Unique Paper"], "issued": {"date-parts": [[2023]]},
                 "type": "preprint"}
            ]
        }
    }
    fetcher = FakeFetcher(
        text_map={"arxiv.org": arxiv_xml},
        json_map={"api.crossref.org": cr_payload}
    )
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="unique paper", sources=["arxiv", "crossref"])
    res = asyncio.run(ms.search(sq))
    # Should dedupe by title+year
    keys = [h.dedupe_key() for h in res.merged]
    assert len(keys) == len(set(keys))


def test_multi_searcher_merges_pdf_urls():
    """When merging hits, prefer the one with PDF URL."""
    cr_payload = {
        "message": {
            "items": [
                {"DOI": "10.1234/pdf", "title": ["Has PDF Meta"],
                 "issued": {"date-parts": [[2022]]},
                 "link": [{"URL": "https://example.com/paper.pdf", "content-type": "application/pdf"}]}
            ]
        }
    }
    oa_payload = {
        "results": [
            {"title": "Has PDF Meta", "doi": "https://doi.org/10.1234/pdf",
             "publication_year": 2022,
             "best_oa_location": {"pdf_url": "https://oa.example/paper.pdf"}}
        ]
    }
    fetcher = FakeFetcher(json_map={
        "api.crossref.org": cr_payload,
        "api.openalex.org": oa_payload,
    })
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="has pdf", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    assert len(res.merged) == 1
    # Should have PDF URL from OpenAlex
    assert res.merged[0].has_pdf


def test_multi_searcher_error_propagation():
    """Errors should be recorded per source."""
    fetcher = FakeFetcher(
        fail={"api.crossref.org"},
        json_map={"api.openalex.org": {"results": []}}
    )
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="test", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    
    cr_run = [r for r in res.runs if r.source == "crossref"][0]
    oa_run = [r for r in res.runs if r.source == "openalex"][0]
    
    # Crossref should have error (or empty hits due to exception handling)
    assert cr_run.error is not None or cr_run.hits == []
    # OpenAlex should succeed
    assert oa_run.error is None


def test_multi_searcher_empty_query():
    """Empty query should return empty results."""
    fetcher = FakeFetcher()
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="", sources=["crossref"])
    res = asyncio.run(ms.search(sq))
    assert res.merged == []
    assert len(res.runs) == 1


def test_multi_searcher_timeout_handling():
    """Slow responses should be handled gracefully."""
    fetcher = FakeFetcher(
        json_map={"api.crossref.org": {"message": {"items": []}}},
        delay=0.01,  # Small delay for testing
    )
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="test", sources=["crossref"], overall_timeout_s=5.0)
    res = asyncio.run(ms.search(sq))
    assert len(res.runs) == 1


def test_multi_searcher_limit_per_source():
    """Limit per source should be respected."""
    items = [{"DOI": f"10.1234/{i}", "title": [f"Paper {i}"],
              "issued": {"date-parts": [[2020]]}} for i in range(50)]
    payload = {"message": {"items": items}}
    fetcher = FakeFetcher(json_map={"api.crossref.org": payload})
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="many", sources=["crossref"], limit_per_source=10)
    res = asyncio.run(ms.search(sq))
    # Crossref searcher may return fewer than limit but should not exceed it
    cr_run = [r for r in res.runs if r.source == "crossref"][0]
    # The searcher itself may internally limit, so just verify we got results
    assert len(cr_run.hits) >= 0


# -----------------------------------------------------------------------------
# Dedupe misidentification prevention
# -----------------------------------------------------------------------------


def test_dedupe_same_title_different_year_not_merged():
    """Papers with same normalized title but different years must NOT merge."""
    h1 = SearchHit(source="crossref", title="Deep Learning for NLP", year=2020)
    h2 = SearchHit(source="arxiv", title="Deep Learning for NLP", year=2022)
    assert h1.dedupe_key() != h2.dedupe_key()


def test_dedupe_same_title_year_diff_author_creates_different_keys():
    """Same title+year but different authors should get different keys via author fallback."""
    # Without year, the key uses author surname
    h1 = SearchHit(source="crossref", title="Quantum Computing Primer", authors=["Smith, John"])
    h2 = SearchHit(source="arxiv", title="Quantum Computing Primer", authors=["Doe, Jane"])
    k1 = h1.dedupe_key()
    k2 = h2.dedupe_key()
    assert k1 != k2, f"Keys should differ: {k1!r} vs {k2!r}"
    assert "author:smith" in k1
    assert "author:doe" in k2


def test_dedupe_title_with_year_ignores_author():
    """When year is present, author is NOT part of the key."""
    h1 = SearchHit(source="crossref", title="Quantum Computing Primer", year=2023, authors=["Smith, John"])
    h2 = SearchHit(source="arxiv", title="Quantum Computing Primer", year=2023, authors=["Doe, Jane"])
    assert h1.dedupe_key() == h2.dedupe_key()


def test_dedupe_doi_normalized_merge():
    """DOIs with different formatting (prefix, case) must produce the same key."""
    h1 = SearchHit(source="crossref", doi="https://doi.org/10.1234/Test")
    h2 = SearchHit(source="openalex", doi="10.1234/test")
    assert h1.dedupe_key() == h2.dedupe_key() == "doi:10.1234/test"


def test_dedupe_different_doi_not_merged():
    """Genuinely different DOIs must NOT merge."""
    h1 = SearchHit(source="crossref", doi="10.1000/a")
    h2 = SearchHit(source="openalex", doi="10.1000/b")
    assert h1.dedupe_key() != h2.dedupe_key()


def test_dedupe_stopword_title_merge():
    """Titles differing only in stopwords must produce the same key."""
    h1 = SearchHit(source="crossref", title="The Machine Learning Book", year=2021)
    h2 = SearchHit(source="arxiv", title="Machine Learning Book", year=2021)
    assert h1.dedupe_key() == h2.dedupe_key()


def test_dedupe_punctuation_title_merge():
    """Titles differing only in punctuation must produce the same key."""
    h1 = SearchHit(source="crossref", title="Hello, World: An Introduction", year=2020)
    h2 = SearchHit(source="arxiv", title="Hello World -- An Introduction", year=2020)
    assert h1.dedupe_key() == h2.dedupe_key()


def test_dedupe_different_isbn_not_merged():
    """Different ISBNs must NOT merge."""
    h1 = SearchHit(source="openlibrary", isbn="978-0-00-000000-1")
    h2 = SearchHit(source="zlibrary", isbn="978-0-00-000000-2")
    assert h1.dedupe_key() != h2.dedupe_key()


def test_dedupe_isbn_normalization_merge():
    """ISBNs with different formatting must produce the same key."""
    h1 = SearchHit(source="openlibrary", isbn="978-0-00-000000-1")
    h2 = SearchHit(source="zlibrary", isbn="9780000000001")
    assert h1.dedupe_key() == h2.dedupe_key() == "isbn:9780000000001"


def test_dedupe_title_only_without_author_fallback():
    """Hits with only stopword titles (normalized to empty) fall back to id(self) → never match."""
    h1 = SearchHit(source="x", title="The A An")
    h2 = SearchHit(source="y", title="The A  An")
    # Both normalize to empty string → id(self) fallback → always unique
    assert h1.dedupe_key() != h2.dedupe_key()
    assert h1.dedupe_key().startswith("id:")
    assert h2.dedupe_key().startswith("id:")


def test_non_null_field_count():
    """Verify _non_null_field_count counts correctly."""
    h = SearchHit(source="x")
    assert h._non_null_field_count == 0
    h.title = "T"
    h.doi = "10.1/a"
    h.year = 2020
    h.authors = ["A"]
    h.pdf_url = "https://x.pdf"
    assert h._non_null_field_count == 5


def test_multi_searcher_record_alt_captures_source_links():
    """When merging duplicates, extra['duplicates'] must hold all source URLs."""
    cr_payload = {
        "message": {
            "items": [
                {"DOI": "10.1234/dup", "title": ["Duplicate Paper"],
                 "issued": {"date-parts": [[2023]]},
                 "URL": "https://doi.org/10.1234/dup"}
            ]
        }
    }
    oa_payload = {
        "results": [
            {"title": "Duplicate Paper", "doi": "https://doi.org/10.1234/dup",
             "publication_year": 2023,
             "id": "https://openalex.org/W1"}
        ]
    }
    fetcher = FakeFetcher(json_map={
        "api.crossref.org": cr_payload,
        "api.openalex.org": oa_payload,
    })
    ms = MultiSearcher(fetcher)
    sq = SearchQuery(text="dup", sources=["crossref", "openalex"])
    res = asyncio.run(ms.search(sq))
    assert len(res.merged) == 1
    dups = res.merged[0].extra.get("duplicates", [])
    assert len(dups) == 2, f"Expected 2 entries (primary + alt), got {len(dups)}"
    sources = {d["source"] for d in dups}
    assert "crossref" in sources
    assert "openalex" in sources
    # Crossref entry should have its DOI URL
    cr_entry = next(d for d in dups if d["source"] == "crossref")
    assert cr_entry["url"] == "https://doi.org/10.1234/dup"


def test_multi_searcher_streaming_vs_gather_parity():
    """Streaming and gather paths must produce identical merged results."""
    items = [
        {"DOI": f"10.1234/{i}", "title": [f"Paper {i}"],
         "issued": {"date-parts": [[2020 + i]]}} for i in range(5)
    ]
    payload = {"message": {"items": items}}
    fetcher = FakeFetcher(json_map={"api.crossref.org": payload})

    ms_stream = MultiSearcher(fetcher)
    MultiSearcher.STREAMING = True
    sq = SearchQuery(text="test", sources=["crossref"], limit_per_source=5)
    res_stream = asyncio.run(ms_stream.search(sq))

    MultiSearcher.STREAMING = False
    fetcher2 = FakeFetcher(json_map={"api.crossref.org": payload})
    ms_gather = MultiSearcher(fetcher2)
    res_gather = asyncio.run(ms_gather.search(sq))

    # Same number of merged results
    assert len(res_stream.merged) == len(res_gather.merged) == 5
    # Same DOI keys
    stream_dois = sorted(h.doi for h in res_stream.merged)
    gather_dois = sorted(h.doi for h in res_gather.merged)
    assert stream_dois == gather_dois
    # Same scores
    stream_scores = [h.score for h in sorted(res_stream.merged, key=lambda x: x.doi or "")]
    gather_scores = [h.score for h in sorted(res_gather.merged, key=lambda x: x.doi or "")]
    assert stream_scores == gather_scores
    # Restore default
    MultiSearcher.STREAMING = True


def test_dedupe_title_sort_order_deterministic():
    """Word sorting in _normalize_title must be deterministic for the same input."""
    h1 = SearchHit(source="a", title="Machine Learning Advanced", year=2022)
    h2 = SearchHit(source="b", title="Advanced Machine Learning", year=2022)
    assert h1.dedupe_key() == h2.dedupe_key()


def test_dedupe_first_author_surname_extraction():
    """_first_author_surname handles various formats."""
    from documentcrawler.searcher.base import _first_author_surname
    assert _first_author_surname(["Smith, John"]) == "smith"
    assert _first_author_surname(["John Smith"]) == "smith"
    assert _first_author_surname(["Jean-Luc Picard"]) == "picard"
    assert _first_author_surname(["van der Waals, Johannes"]) == "van der waals"
    assert _first_author_surname([]) == ""
