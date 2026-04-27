from pathlib import Path

from documentcrawler.importers import parse_file
from documentcrawler.importers.bibtex_importer import parse_bibtex
from documentcrawler.importers.csv_importer import parse_csv
from documentcrawler.importers.doi_list import parse_doi_list


def test_parse_csv(tmp_path: Path):
    f = tmp_path / "in.csv"
    f.write_text(
        "DOI,Title,Authors,Year,ISBN,Journal,Editor,Keywords\n"
        "10.1038/s41586-020-2649-2,Numpy paper,Harris; van der Walt,2020,,Nature,,arrays;python\n"
        ",Attention Is All You Need,Vaswani and Shazeer,2017,,NeurIPS,Guyon,transformer\n"
        ",,,,\n",
        encoding="utf-8",
    )
    qs = list(parse_csv(f))
    assert len(qs) == 2
    assert qs[0].doi == "10.1038/s41586-020-2649-2"
    assert qs[0].authors == ["Harris", "van der Walt"]
    assert qs[0].extra["journal"] == "Nature"
    assert qs[1].title == "Attention Is All You Need"
    assert qs[1].authors == ["Vaswani", "Shazeer"]
    assert qs[1].year == 2017
    assert qs[1].extra["journal"] == "NeurIPS"
    assert qs[1].extra["editor"] == "Guyon"
    assert qs[1].keywords == ["transformer"]


def test_parse_doi_list(tmp_path: Path):
    f = tmp_path / "list.txt"
    f.write_text(
        "# header\n10.1000/a\nhttps://doi.org/10.2000/b\n\n",
        encoding="utf-8",
    )
    qs = list(parse_doi_list(f))
    assert [q.doi for q in qs] == ["10.1000/a", "10.2000/b"]


def test_parse_bibtex(tmp_path: Path):
    f = tmp_path / "refs.bib"
    f.write_text(
        """
        @article{vaswani2017attention,
            title   = {{Attention Is All You Need}},
            author  = {Vaswani, Ashish and Shazeer, Noam},
            year    = {2017},
            doi     = {10.48550/arXiv.1706.03762},
            journal = {NeurIPS},
            editor  = {Guyon}
        }
        """,
        encoding="utf-8",
    )
    qs = list(parse_bibtex(f))
    assert len(qs) == 1
    q = qs[0]
    assert q.title == "Attention Is All You Need"
    assert q.year == 2017
    assert q.doi == "10.48550/arXiv.1706.03762"
    assert q.authors == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert q.extra["journal"] == "NeurIPS"
    assert q.extra["editor"] == "Guyon"


def test_parse_file_dispatch(tmp_path: Path):
    f = tmp_path / "list.txt"
    f.write_text("10.1234/a\n", encoding="utf-8")
    qs = list(parse_file(f))
    assert qs[0].doi == "10.1234/a"
