"""Fixture web pages for tests/test_docker_companies.py (Task 3).

Served by a small HTTP server inside a ``--network host`` container so the
enricher can crawl them like a real company website.
"""

FILLER = "<br>".join(
    f"Lorem ipsum dolor sit amet consectetur adipiscing elit {index}" for index in range(400)
)
LEGAL = (
    "Angaben gem&#228;&#223; &#167; 5 TMG<br>ACME GmbH<br>Main Street 12<br>10115 Berlin<br>"
    "Telefon: +49 30 1234567<br>E-Mail: info@acme-gmbh.de<br>USt-IdNr.: DE123456789<br>"
    "Vertreten durch: Max Muster"
)

PAGES = {
    "/": (
        "<html><body><nav><a href='/de/impressum.html'>Impressum</a>"
        "<a href='/datenschutz'>Datenschutz</a></nav><main>Welcome</main>"
        "<footer>&#169; 2024 ACME</footer></body></html>"
    ),
    "/de/impressum.html": (
        f"<html><body><main><h2>Impressum</h2>{FILLER}<br>{LEGAL}</main></body></html>"
    ),
    "/no-impressum": "<html><body><a href='/about'>About</a></body></html>",
}
