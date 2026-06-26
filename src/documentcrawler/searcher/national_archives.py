"""National and international archive/collection searchers.

These searchers query national libraries, archives, and cultural heritage
aggregators for publication metadata. They return identifiers and catalog
information without attempting direct document access.

Supported archives:
- Library of Congress (US)
- UK National Archives
- Europeana (EU cultural heritage aggregator)
- Open Library (Internet Archive books)
"""

from __future__ import annotations

from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_isbn

log = get_logger(__name__)


@register("library_of_congress")
class LibraryOfCongressSearcher(Searcher):
    """Library of Congress catalog searcher.

    Uses the Library of Congress SRU (Search/Retrieve via URL) API.
    Returns catalog metadata including LCCN, OCLC numbers, and ISBNs.
    """

    _SRU_URL = "http://lx2.loc.gov:210/lcdb"

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        # Build CQL query
        if kind == "title":
            cql = f'dc.title="{query}"'
        elif kind == "author":
            cql = f'dc.creator="{query}"'
        else:
            cql = f'dc.title any "{query}" or dc.creator any "{query}"'

        params: dict[str, Any] = {
            "operation": "searchRetrieve",
            "query": cql,
            "maximumRecords": min(limit, 50),
            "recordSchema": "dc",
        }

        try:
            xml = await self.fetcher.get_text(self._SRU_URL, params=params)
        except Exception as e:
            log.debug("LOC search failed: %s", e)
            return []

        return self._parse_sru_response(xml, limit)

    def _parse_sru_response(self, xml: str, limit: int) -> list[SearchHit]:
        """Parse SRU XML response into SearchHits."""
        from xml.etree import ElementTree as ET

        hits: list[SearchHit] = []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            log.debug("LOC response parsing failed: %s", e)
            return hits

        # Define namespaces
        ns = {
            "zs": "http://docs.oasis-open.org/ns/search-ws/sruResponse",
            "dc": "http://purl.org/dc/elements/1.1/",
            "oclcdcs": "http://purl.org/dc/elements/1.1/",
        }

        records = root.findall(".//zs:record", ns) or root.findall(".//record", ns)
        for i, record in enumerate(records[:limit]):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_record(record, ns, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_record(
        self, record: Any, ns: dict[str, str], score: float
    ) -> SearchHit | None:
        # Extract recordData
        data = record.find(".//zs:recordData", ns)
        if data is None:
            data = record.find(".//recordData", ns)
        if data is None:
            return None

        dc = data.find(".//dc:dc", ns)
        if dc is None:
            dc = data.find(".//oclcdcs:dc", ns)
        if dc is None and len(data) > 0:
            dc = data[0]
        if dc is None:
            return None

        def get_dc(tag: str) -> str | None:
            el = dc.find(f"dc:{tag}", ns) if dc is not None else None
            if el is not None and el.text:
                return el.text.strip()
            return None

        title = get_dc("title")
        if not title:
            return None

        authors: list[str] = []
        for creator in dc.findall("dc:creator", ns) if dc is not None else []:
            if creator.text:
                authors.append(creator.text.strip())

        year: int | None = None
        date = get_dc("date")
        if date and len(date) >= 4 and date[:4].isdigit():
            year = int(date[:4])

        # Get identifiers
        isbn: str | None = None
        lccn: str | None = None
        for identifier in dc.findall("dc:identifier", ns) if dc is not None else []:
            if identifier.text:
                id_text = identifier.text.strip()
                if "ISBN" in id_text.upper():
                    isbn = normalize_isbn(id_text.replace("ISBN", "").replace(":", "").strip())
                elif id_text.startswith("http://") or id_text.startswith("https://"):
                    lccn = id_text

        # Build catalog URL
        url = lccn or f"https://catalog.loc.gov/vwebv/search?searchArg={title[:50]}&searchCode=GKEY%5E*&searchType=0"

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            isbn=isbn,
            url=url,
            score=score,
            extra={
                "archive": "Library of Congress",
                "catalog_type": "LOC",
                "lccn": lccn,
            },
        )


@register("uk_national_archives")
class UKNationalArchivesSearcher(Searcher):
    """UK National Archives Discovery API searcher.

    Searches the UK National Archives catalog for records.
    Returns archival reference numbers and metadata.
    """

    _API = "https://discovery.nationalarchives.gov.uk/API/search/v1/records"

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        params: dict[str, Any] = {
            "s": query,
            "rpp": min(limit, 50),
        }

        try:
            data = await self.fetcher.get_json(self._API, params=params)
        except Exception as e:
            log.debug("UK National Archives search failed: %s", e)
            return []

        records = ((data or {}).get("records")) or []
        hits: list[SearchHit] = []
        for i, rec in enumerate(records):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_record(rec, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_record(self, rec: dict[str, Any], score: float) -> SearchHit | None:
        title = rec.get("title")
        if not title:
            return None

        # Extract year from coverage dates
        year: int | None = None
        cover_dates = rec.get("coveringDates", "")
        if cover_dates:
            import re
            years = [int(y) for y in re.findall(r"\b(\d{4})\b", cover_dates) if 1000 < int(y) < 2100]
            if years:
                year = min(years)  # Use earliest year

        # Reference number
        reference = rec.get("reference", "")
        url = f"https://discovery.nationalarchives.gov.uk/details/r/{rec.get('id', '')}" if rec.get("id") else None

        return SearchHit(
            source=self.name,
            title=title,
            year=year,
            url=url,
            score=score,
            extra={
                "archive": "UK National Archives",
                "reference": reference,
                "department": rec.get("department"),
                "covering_dates": cover_dates,
            },
        )


@register("europeana")
class EuropeanaSearcher(Searcher):
    """Europeana cultural heritage aggregator searcher.

    Searches Europeana for books, manuscripts, and publications.
    Requires a free API key from https://pro.europeana.eu/api
    """

    _API = "https://api.europeana.eu/record/v2/search.json"

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        api_key = self.options.get("api_key")
        if not api_key:
            log.debug("Europeana search skipped: no api_key configured")
            return []

        params: dict[str, Any] = {
            "query": query,
            "rows": min(limit, 100),
            "wskey": api_key,
            "qf": "TYPE:TEXT",  # Focus on text documents
        }

        try:
            data = await self.fetcher.get_json(self._API, params=params)
        except Exception as e:
            log.debug("Europeana search failed: %s", e)
            return []

        items = ((data or {}).get("items")) or []
        hits: list[SearchHit] = []
        for i, item in enumerate(items):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_item(item, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_item(self, item: dict[str, Any], score: float) -> SearchHit | None:
        # Get title
        title_list = item.get("title") or []
        title = title_list[0] if isinstance(title_list, list) and title_list else str(title_list) if title_list else None
        if not title:
            return None

        # Get authors/creators
        authors: list[str] = []
        dc_creator = item.get("dcCreator") or []
        if isinstance(dc_creator, list):
            authors = [c for c in dc_creator if c]
        elif isinstance(dc_creator, str):
            authors = [dc_creator]

        # Get year from various date fields
        year: int | None = None
        for date_field in ["year", "timestamp", "date"]:
            date_val = item.get(date_field)
            if date_val:
                import re
                years = [int(y) for y in re.findall(r"\b(\d{4})\b", str(date_val)) if 1000 < int(y) < 2100]
                if years:
                    year = years[0]
                    break

        # Europeana identifier and link
        europeana_id = item.get("id")
        url = f"https://europeana.eu/item/{europeana_id}" if europeana_id else None

        # Get data provider
        provider = item.get("dataProvider", ["Unknown"])[0] if isinstance(item.get("dataProvider"), list) else item.get("dataProvider", "Unknown")

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            url=url,
            score=score,
            extra={
                "archive": "Europeana",
                "data_provider": provider,
                "europeana_id": europeana_id,
                "rights": item.get("rights"),
            },
        )
