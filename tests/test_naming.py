from datetime import UTC, datetime
from pathlib import Path

from documentcrawler.models import DocStatus, DocumentRow
from documentcrawler.storage.naming import render_destination


def _doc() -> DocumentRow:
    now = datetime.now(UTC)
    return DocumentRow(
        id=42,
        doi="10.1000/xyz",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
        status=DocStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


def test_render_destination_default_template(tmp_path: Path):
    dest = render_destination(
        tmp_path,
        _doc(),
        filename_template="{first_author_last}_{year}_{title_slug}.{ext}",
        folder_template="{first_author_initial}",
        ext="pdf",
    )
    assert dest.parent == tmp_path / "V"
    assert dest.name.startswith("Vaswani_2017_")
    assert dest.suffix == ".pdf"


def test_render_destination_no_folder(tmp_path: Path):
    dest = render_destination(
        tmp_path,
        _doc(),
        filename_template="{doi_slug}.{ext}",
        folder_template=None,
        ext="pdf",
    )
    assert dest.parent == tmp_path
    assert "10.1000_xyz" in dest.name


def test_render_destination_journal_and_publisher_placeholders(tmp_path: Path):
    doc = _doc()
    doc.extra = {"journal": "Nature Communications", "publisher": "Springer Nature"}
    dest = render_destination(
        tmp_path,
        doc,
        filename_template="{first_author_last}_{year}.{ext}",
        folder_template="{publisher_slug}/{journal_slug}",
        ext="pdf",
    )
    assert dest.parent == tmp_path / "springer-nature" / "nature-communications"
    assert dest.name == "Vaswani_2017.pdf"
