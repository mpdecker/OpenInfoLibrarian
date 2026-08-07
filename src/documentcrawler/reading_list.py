"""Automated HTML and Markdown reading list report generator."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.models import DocumentRow


def generate_reading_list(docs: list[DocumentRow], fmt: str = "markdown") -> str:
    """Generate a formatted Markdown or HTML reading list report for a collection of documents."""
    if fmt.lower() in ("html", "htm"):
        return _generate_html_reading_list(docs)
    return _generate_markdown_reading_list(docs)


def _generate_markdown_reading_list(docs: list[DocumentRow]) -> str:
    lines: list[str] = [
        "# Research Library Reading List",
        "",
        f"Total Documents: {len(docs)}",
        "",
        "---",
        "",
    ]

    for idx, doc in enumerate(docs, 1):
        title = doc.title or "Untitled Document"
        authors_str = ", ".join(doc.authors) if doc.authors else "Unknown Author"
        year_str = f" ({doc.year})" if doc.year else ""
        doi_str = f" **DOI:** `{doc.doi}`" if doc.doi else ""
        file_str = f" **File:** [{doc.file_path}]({doc.file_path})" if doc.file_path else ""

        lines.append(f"### {idx}. {title}{year_str}")
        lines.append(f"- **Authors:** {authors_str}")
        if doi_str or file_str:
            lines.append(f"-{doi_str}{file_str}")
        if doc.keywords:
            lines.append(f"- **Tags:** {', '.join(doc.keywords)}")
        lines.append("")

    return "\n".join(lines)


def _generate_html_reading_list(docs: list[DocumentRow]) -> str:
    items_html: list[str] = []

    for idx, doc in enumerate(docs, 1):
        title = html.escape(doc.title or "Untitled Document")
        authors = html.escape(", ".join(doc.authors)) if doc.authors else "Unknown Author"
        year = f" ({doc.year})" if doc.year else ""

        doi_html = f'<span>DOI: <a href="https://doi.org/{html.escape(doc.doi)}">{html.escape(doc.doi)}</a></span>' if doc.doi else ""
        file_html = f'<span>File: <code>{html.escape(doc.file_path)}</code></span>' if doc.file_path else ""

        tags_html = ""
        if doc.keywords:
            badges = "".join(f'<span style="background:#e2e8f0;padding:2px 6px;border-radius:4px;font-size:12px;margin-right:4px;">{html.escape(k)}</span>' for k in doc.keywords)
            tags_html = f'<div style="margin-top:6px;">{badges}</div>'

        items_html.append(f"""
        <div style="border-bottom:1px solid #cbd5e1;padding:12px 0;">
            <h3 style="margin:0 0 4px 0;color:#1e293b;">{idx}. {title}{year}</h3>
            <div style="color:#475569;font-size:14px;"><strong>Authors:</strong> {authors}</div>
            <div style="color:#64748b;font-size:13px;margin-top:4px;">{doi_html} {file_html}</div>
            {tags_html}
        </div>
        """)

    body_content = "\n".join(items_html) if items_html else "<p>No documents found.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Research Library Reading List</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; color: #0f172a; }}
        h1 {{ border-bottom: 2px solid #0284c7; padding-bottom: 8px; color: #0369a1; }}
    </style>
</head>
<body>
    <h1>Research Library Reading List</h1>
    <p style="color:#64748b;">Total Documents: {len(docs)}</p>
    {body_content}
</body>
</html>
"""
