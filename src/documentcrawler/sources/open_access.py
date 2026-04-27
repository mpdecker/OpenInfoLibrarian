"""Direct open-access PDF candidates from Unpaywall / OpenAlex enrichment."""

from __future__ import annotations

from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register


@register("open_access")
class OpenAccessSource(Source):
    """Use OA URLs gathered during metadata enrichment."""

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        return [
            Candidate(source=self.name, url=u, confidence=0.95, note="oa-direct")
            for u in ctx.metadata.oa_urls
        ]
