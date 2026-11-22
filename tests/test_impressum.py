"""impressum.py: URL helpers, link discovery, visible text and token pruning."""

import pytest

import impressum as imp

HOME_HTML = """
<html><body>
  <nav><a href="/">Home</a><a href="/impressum">Impressum</a></nav>
  <main>
    <h1>ACME GmbH</h1>
    <a href="/about">About us</a>
    <a href="/imprint-legal">Our legal notice</a>
  </main>
  <footer></footer>
</body></html>
"""

IMPRESSUM_HTML = """
<html><head><title>Impressum</title><style>.x{}</style></head><body>
<script>var tracking = 1;</script>
<nav><a href="/">Home</a></nav>
<header>Big hero</header>
<main>
  <h2>Impressum</h2>
  <p>Angaben gemäß § 5 TMG</p>
  <p>ACME GmbH</p>
  <p>Main Street 12</p>
  <p>10115 Berlin</p>
  <p>Telefon: +49 30 1234567</p>
  <p>E-Mail: info@acme-gmbh.de</p>
  <p>USt-IdNr.: DE123456789</p>
</main>
<noscript>enable js</noscript>
<footer>© 2024 ACME</footer>
</body></html>
"""


# --- URL helpers -----------------------------------------------------------


def test_normalize_url_adds_scheme():
    assert imp.normalize_url("www.example.de") == "https://www.example.de"
    assert imp.normalize_url("example.de/about") == "https://example.de/about"


def test_normalize_url_keeps_existing_scheme():
    assert imp.normalize_url("http://example.de") == "http://example.de"
    assert imp.normalize_url("  https://example.de  ") == "https://example.de"


@pytest.mark.parametrize("value", [None, "", "   ", "mailto:info@x.de", "ftp://x.de", "///x"])
def test_normalize_url_rejects_unusable_values(value):
    assert imp.normalize_url(value) is None


def test_is_same_host():
    assert imp.is_same_host("https://www.x.de/a", "https://WWW.x.de/b")
    assert not imp.is_same_host("https://x.de", "https://other.de")


# --- link discovery ----------------------------------------------------------


def test_find_link_by_visible_text():
    assert imp.find_impressum_link(HOME_HTML, "https://acme.de/") == "https://acme.de/impressum"


def test_find_link_by_href_fallback():
    html = '<a href="/impressum.html">Rechtliches</a><a href="/about">About</a>'
    assert (
        imp.find_impressum_link(html, "https://acme.de/") == "https://acme.de/impressum.html"
    )


def test_find_link_case_insensitive_and_stripped():
    html = '<a href="/x">  IMPRINT  </a>'
    assert imp.find_impressum_link(html, "https://acme.de/") == "https://acme.de/x"


def test_find_link_resolves_relative_paths():
    html = '<a href="../impressum.html">Impressum</a>'
    assert (
        imp.find_impressum_link(html, "https://acme.de/de/info/")
        == "https://acme.de/de/impressum.html"
    )


def test_visible_text_label_wins_over_href():
    html = '<a href="/impressum">Kontakt</a><a href="/legal">Impressum</a>'
    assert imp.find_impressum_link(html, "https://acme.de/") == "https://acme.de/legal"


def test_no_link_returns_none():
    assert imp.find_impressum_link("<html><body><a href='/about'>About</a></body></html>",
                                   "https://acme.de/") is None


def test_find_link_ignores_javascript_hrefs():
    html = '<a href="javascript:void(0)">Impressum</a>'
    assert imp.find_impressum_link(html, "https://acme.de/") is None


def test_empty_home_page_returns_none():
    assert imp.find_impressum_link("", "https://acme.de/") is None


# --- visible text ------------------------------------------------------------


def test_extract_visible_text_removes_noise():
    text = imp.extract_visible_text(IMPRESSUM_HTML)
    assert "Angaben gemäß § 5 TMG" in text
    assert "info@acme-gmbh.de" in text
    # script/style/nav/header/footer/noscript content is gone
    assert "tracking" not in text
    assert ".x{}" not in text
    assert "Big hero" not in text
    assert "© 2024 ACME" not in text
    assert "enable js" not in text


def test_extract_visible_text_normalizes_whitespace():
    html = "<html><body><p>  Achtung   jetzt </p>\n\n<p>\n\nNeue Zeile</p></body></html>"
    assert imp.extract_visible_text(html) == "Achtung jetzt\nNeue Zeile"


def test_extract_visible_text_of_empty_page():
    assert imp.extract_visible_text("") == ""
    assert imp.extract_visible_text("<html><body></body></html>") == ""


# --- token pruning -----------------------------------------------------------

LEGAL_BLOCK = """
Angaben gemäß § 5 TMG
ACME GmbH
Main Street 12
10115 Berlin
Telefon: +49 30 1234567
E-Mail: info@acme-gmbh.de
USt-IdNr.: DE123456789
Handelsregister: HRB 12345
Vertreten durch: Max Muster
"""


def _noisy_page(filler_lines=200):
    head = "\n".join(
        ["Startseite", "Über uns", "Leistungen", "Impressum", "Kontakt",
         "Cookie-Hinweis: Wir verwenden Cookies", "Datenschutz", "AGB"]
    )
    filler = "\n".join(
        f"Lorem ipsum dolor sit amet consectetur adipiscing elit {i}" for i in range(filler_lines)
    )
    footer = "\n".join(
        ["Zurück nach oben", "© 2024 ACME GmbH", "Deutsch", "English", "Kontaktformular"]
        + [f"Footer link {i}" for i in range(30)]
    )
    return head + "\n" + filler + "\n" + LEGAL_BLOCK + "\n" + footer


def test_short_text_is_sent_unchanged():
    text = LEGAL_BLOCK * 3
    assert len(text) <= imp.MAX_TEXT_CHARS
    result = imp.optimize_text(text)
    assert result.text == text
    assert result.cutoff == "none (under threshold)"
    assert result.chars_after == result.chars_before


def test_long_text_is_cut_to_the_legal_block():
    page = _noisy_page()
    assert len(page) > imp.MAX_TEXT_CHARS
    result = imp.optimize_text(page)

    assert result.chars_after < result.chars_before
    assert result.chars_after <= imp.MAX_TEXT_CHARS
    assert result.text.startswith("Angaben gemäß § 5 TMG")
    assert "info@acme-gmbh.de" in result.text
    assert "DE123456789" in result.text
    assert "HRB 12345" in result.text
    assert "Vertreten durch: Max Muster" in result.text
    # boilerplate and footer are gone
    assert "Cookie-Hinweis" not in result.text
    assert "Zurück nach oben" not in result.text
    assert "© 2024" not in result.text
    assert "Footer link" not in result.text


def test_optimization_logs_before_and_after_counts():
    page = _noisy_page()
    result = imp.optimize_text(page)
    summary = imp.log_summary(result)
    assert f"chars {result.chars_before} -> {result.chars_after}" in summary
    assert "tokens ~" in summary
    assert result.tokens_before == result.chars_before // 4
    assert result.tokens_after == result.chars_after // 4
    assert 0 < result.saved_percent < 100


def test_head_anchor_selects_the_legal_section():
    page = _noisy_page()
    result = imp.optimize_text(page)
    assert result.head_anchor in (
        "angaben gemäß § 5 tmg",
        "angaben gemäß § 5 ddg",
        "impressum",
        "anbieter",
        "diensteanbieter",
    )
    assert "Startseite" not in result.text


def test_tail_margin_keeps_context_after_the_last_datum():
    page = _noisy_page()
    result = imp.optimize_text(page)
    assert result.tail_anchor in {"responsible", "handelsregister", "vat", "email", "phone"}
    assert "Vertreten durch: Max Muster" in result.text


def test_no_anchors_falls_back_to_window_with_signals():
    # Over the threshold, but without any head/tail anchor: the strongest
    # signal (e-mail) must survive the fallback.
    noise = "\n".join(f"Filler line number {i} without legal data" for i in range(400))
    text = noise + "\nKontakt: info@nomatch.de\n" + noise
    result = imp.optimize_text(text)
    assert "info@nomatch.de" in result.text
    assert result.chars_after <= imp.MAX_TEXT_CHARS


def test_text_over_threshold_without_any_signal_is_still_bounded():
    text = "\n".join(f"Filler line number {i} without legal data" for i in range(500))
    result = imp.optimize_text(text)
    assert result.chars_after <= imp.MAX_TEXT_CHARS


def test_repeated_long_lines_are_dropped():
    block = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 4
    text = "\n".join([block] * 100) + "\nImpressum\nTelefon: +49 30 1234567\n"
    result = imp.optimize_text(text)
    assert result.chars_after < result.chars_before
    assert "Impressum" in result.text
    assert "+49 30 1234567" in result.text


@pytest.mark.parametrize(
    "signal",
    [
        "Telefon: +49 30 1234567",
        "Telefax 0049 30 7654321",
        "USt-IdNr. DE123456789",
        "E-Mail: info@example.de",
        "Handelsregister HRB 12345",
        "Amtsgericht Berlin-Charlottenburg",
        "Geschäftsführer: Max Muster",
        "Vertreten durch: Erika Muster",
        "Inhaltlich verantwortlich: Max Muster",
    ],
)
def test_tail_anchor_patterns(signal):
    assert imp._has_strong_signal(signal)


def test_anchor_names_includes_all_detected_signals():
    names = set(imp.iter_anchor_names(LEGAL_BLOCK))
    assert names == {"phone", "email", "vat", "handelsregister", "responsible"}


def test_chars_per_token_heuristic():
    assert imp.estimate_tokens("a" * 400) == 100
    assert imp.CHARS_PER_TOKEN == 4
