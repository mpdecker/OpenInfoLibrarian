"""Unit tests for arXiv metadata resolver."""

import pytest

from documentcrawler.config import FetcherConfig
from documentcrawler.fetcher import Fetcher
from documentcrawler.metadata.arxiv import arxiv_lookup, extract_arxiv_id


def test_extract_arxiv_id():
    assert extract_arxiv_id("https://arxiv.org/abs/1706.03762") == "1706.03762"
    assert extract_arxiv_id("https://arxiv.org/pdf/1706.03762v1.pdf") == "1706.03762v1"
    assert extract_arxiv_id("arxiv:1706.03762") == "1706.03762"
    assert extract_arxiv_id("1706.03762") == "1706.03762"


@pytest.mark.asyncio
async def test_arxiv_lookup_mocked(httpx_mock):
    httpx_mock.add_response(
        url="http://export.arxiv.org/api/query?id_list=1706.03762",
        text="""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Attention Is All You Need</title>
            <summary>The dominant sequence transduction models are based on complex recurrent...</summary>
            <published>2017-06-12T17:57:34Z</published>
            <author><name>Ashish Vaswani</name></author>
            <author><name>Noam Shazeer</name></author>
          </entry>
        </feed>""",
    )

    fetcher = Fetcher(FetcherConfig())
    async with fetcher:
        meta = await arxiv_lookup(fetcher, "1706.03762")
        assert meta is not None
        assert meta["title"] == "Attention Is All You Need"
        assert meta["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
        assert meta["year"] == 2017
        assert meta["pdf_url"] == "https://arxiv.org/pdf/1706.03762.pdf"
