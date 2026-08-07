"""Prometheus metrics exporter module."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.db import Database


def generate_prometheus_metrics(db: Database) -> str:
    """Generate operational metrics in standard Prometheus text format."""
    stats = db.get_db_stats()
    doc_counts = stats.get("documents", {})
    source_stats = db.get_source_diagnostics()

    lines: list[str] = [
        "# HELP document_crawler_documents_total Total documents in queue by status.",
        "# TYPE document_crawler_documents_total gauge",
    ]

    for status_name in ("pending", "in_progress", "done", "failed"):
        count = doc_counts.get(status_name, 0)
        lines.append(f'document_crawler_documents_total{{status="{status_name}"}} {count}')

    lines.extend(
        [
            "# HELP document_crawler_source_attempts_total Total source attempt count.",
            "# TYPE document_crawler_source_attempts_total counter",
        ]
    )
    for s in source_stats:
        src = s["source"]
        tot = s["total_attempts"]
        succ = s["successful_attempts"]
        lines.append(f'document_crawler_source_attempts_total{{source="{src}",result="total"}} {tot}')
        lines.append(f'document_crawler_source_attempts_total{{source="{src}",result="success"}} {succ}')

    lines.extend(
        [
            "# HELP document_crawler_downloaded_bytes_total Total downloaded bandwidth in bytes.",
            "# TYPE document_crawler_downloaded_bytes_total counter",
        ]
    )
    for s in source_stats:
        src = s["source"]
        bytes_count = s["total_bytes"]
        lines.append(f'document_crawler_downloaded_bytes_total{{source="{src}"}} {bytes_count}')

    lines.extend(
        [
            "# HELP document_crawler_webhooks_registered Total registered webhooks.",
            "# TYPE document_crawler_webhooks_registered gauge",
            f'document_crawler_webhooks_registered {stats.get("total_webhooks", 0)}',
        ]
    )

    return "\n".join(lines) + "\n"
