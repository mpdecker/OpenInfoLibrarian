"""PubMed Central OA full text via NCBI E-utilities."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from documentcrawler.fetcher import FetchError
from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.storage.writer import looks_like_pdf

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ELINK = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
_PMC_PDF = "https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
_EPMC_PDF = "https://europepmc.org/articles/{pmcid}?pdf=render"

# NCBI's PDF endpoint increasingly sits behind bot checks that no amount of
# link parsing satisfies — detecting them here keeps the attempt log honest
# instead of burning a Playwright fallback that cannot pass them either.
_POW_MARKERS = ("cloudpmc-viewer-pow", "POW_CHALLENGE")
_RECAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/challengepage")


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
        candidates = [
            Candidate(
                source=self.name,
                url=_PMC_PDF.format(pmcid=pmcid_norm),
                confidence=0.9,
                note=f"pmc:{pmcid_norm}",
            )
        ]
        # Europe PMC mirrors the PMC OA corpus behind a friendlier front end;
        # it often serves the same PDF where NCBI's bot checks won't.
        candidates.append(
            Candidate(
                source=self.name,
                url=_EPMC_PDF.format(pmcid=pmcid_norm),
                confidence=0.75,
                note=f"europepmc:{pmcid_norm}",
            )
        )
        return candidates

    async def fetch(self, candidate: Candidate, ctx: SourceContext) -> bytes | None:
        if "europepmc.org" in candidate.url:
            return await super().fetch(candidate, ctx)

        resp = await ctx.fetcher.get(candidate.url)
        if looks_like_pdf(resp.content):
            return resp.content
        self._raise_if_challenge(resp.text, candidate.url)
        # Not a PDF and not a bot check — let the generic resolver deal with
        # interstitials / embedded links / Playwright as usual.
        return await super().fetch(candidate, ctx)

    @staticmethod
    def _raise_if_challenge(html: str, url: str) -> None:
        page = html[:4000]  # markers all live in the <head>/early scripts
        if any(m in page for m in _POW_MARKERS):
            raise FetchError(
                "PMC served a proof-of-work challenge page "
                "(cloudpmc-viewer-pow); automated PDF download is blocked "
                "for this client — retry later or open the article in a browser",
                url=url,
            )
        if any(m in page for m in _RECAPTCHA_MARKERS):
            raise FetchError(
                "PMC served a reCAPTCHA challenge page; automated PDF "
                "download is blocked from this network",
                url=url,
            )

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
