from documentcrawler.sources.scihub import _extract_pdf_url


def test_iframe_pdf():
    html = """
    <html><body>
      <iframe id="pdf" src="//sci-hub.se/downloads/2020-01-01/00/foo.pdf#navpanes=0"></iframe>
    </body></html>
    """
    url = _extract_pdf_url(html, "https://sci-hub.se")
    assert url == "https://sci-hub.se/downloads/2020-01-01/00/foo.pdf#navpanes=0"


def test_embed_pdf():
    html = """
    <html><body>
      <embed type="application/pdf" src="/dl/foo.pdf"></embed>
    </body></html>
    """
    url = _extract_pdf_url(html, "https://sci-hub.ru")
    assert url == "https://sci-hub.ru/dl/foo.pdf"


def test_button_onclick():
    html = """
    <html><body>
      <button onclick="location.href='//sci-hub.se/dl/abcd.pdf?download=true'">save</button>
    </body></html>
    """
    url = _extract_pdf_url(html, "https://sci-hub.se")
    assert url == "https://sci-hub.se/dl/abcd.pdf?download=true"


def test_no_pdf_returns_none():
    assert _extract_pdf_url("<html></html>", "https://sci-hub.se") is None
