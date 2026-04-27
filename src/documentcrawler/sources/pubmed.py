"""PubMed Central OA full text via NCBI E-utilities."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ELINK = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
_PMC_PDF = "https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"


@register("pubmed")
class PubMedSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        pmcid = ctx.metadata.pmcid
        pmid = ctx.metadata.pmid
        doi = ctx.metadata.doi

        if not pmcid and not pmid and doi:
            pmid = await self._pmid_from_doi(ctx, doi)

        if not pmcid and pmid:
            pmcid = await self._pmcid_from_pmid(ctx, pmid)

        if not pmcid:
            return []

        pmcid_norm = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
        return [
            Candidate(
                source=self.name,
                url=_PMC_PDF.format(pmcid=pmcid_norm),
                confidence=0.9,
                note=f"pmc:{pmcid_norm}",
            )
        ]

    async def _pmid_from_doi(self, ctx: SourceContext, doi: str) -> str | None:
        try:
            xml = await ctx.fetcher.get_text(
                _ESEARCH, params={"db": "pubmed", "term": f"{doi}[doi]"}
            )
            root = ET.fromstring(xml)
            id_el = root.find(".//IdList/Id")
            return id_el.text if id_el is not None else None
        except Exception:
            return None

    async def _pmcid_from_pmid(self, ctx: SourceContext, pmid: str) -> str | None:
        try:
            xml = await ctx.fetcher.get_text(
                _ELINK,
                params={
                    "dbfrom": "pubmed",
                    "db": "pmc",
                    "id": pmid,
                    "linkname": "pubmed_pmc",
                },
            )
            root = ET.fromstring(xml)
            id_el = root.find(".//LinkSet/LinkSetDb/Link/Id")
            return id_el.text if id_el is not None else None
        except Exception:
            return None
