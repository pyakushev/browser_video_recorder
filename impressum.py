"""impressum.py - Impressum discovery, visible-text extraction and token pruning.

Three independent layers, so each one can be tested without network access:

* :func:`normalize_url` - ``www.example.de`` -> ``https://www.example.de``;
* :func:`find_impressum_link` - the Impressum/Imprint link of the home page;
* :func:`extract_visible_text` - BeautifulSoup text without scripts/nav/footer;
* :func:`optimize_text` - the token-budget cut (the core of Internship Task 3).

The DeepSeek client lives in :mod:`deepseek_client`; the orchestration in
:mod:`enrich_companies`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

#: Pages longer than this (visible characters) are pruned before the LLM call.
MAX_TEXT_CHARS = 8000
#: Rough heuristic used by the task: tokens ~= characters // 4.
CHARS_PER_TOKEN = 4
#: How far after the last tail anchor the text is kept by default.
TAIL_MARGIN = 400
#: Default maximum how far the tail margin may grow (capped by max_chars).
MAX_TAIL_MARGIN = 4000
#: Characters kept by the last-resort head+tail window.
WINDOW_CHARS = 3000
#: A single line longer than this without any legal data is filler.
MAX_LINE_CHARS = 600

IMPRESSUM_WORDS = ("impressum", "imprint")
#: Visible-text labels that open the legal section (head anchors).  Strong
#: anchors are preferably searched first so a menu item "Impressum" at the
#: top of the page cannot move the cut into the navigation.
STRONG_HEAD_ANCHORS: Tuple[str, ...] = (
    "angaben gemäß § 5 tmg",
    "angaben gemäß § 5 ddg",
    "angaben gemäß §5 tmg",
    "angaben gemäß §5 ddg",
    "diensteanbieter",
)
HEAD_ANCHORS: Tuple[str, ...] = STRONG_HEAD_ANCHORS + ("anbieter", "impressum")

#: Strong signals of legally required data (tail anchors).  Each entry is a
#: named pattern so the log can say which anchors were found.
TAIL_ANCHORS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("phone", re.compile(r"(?:\+49|0049)[\s\-/()]*\d[\d\s\-/()]{5,}", re.IGNORECASE)),
    ("vat", re.compile(r"\bDE\s?\d{9}\b", re.IGNORECASE)),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE)),
    (
        "handelsregister",
        re.compile(
            r"(?:HR\s?[AB]\s*\d+|Registergericht|Amtsgericht|Handelsregister\s+[^\n]{1,60})",
            re.IGNORECASE,
        ),
    ),
    (
        "responsible",
        re.compile(
            r"(?:Verantwortlich|Vertreten\s+durch|Gesch[äa]ftsf[üu]hrer(?:in)?|"
            r"Inhaltlich\s+verantwortlich)",
            re.IGNORECASE,
        ),
    ),
)

#: Lines that are boilerplate even inside the legal section.
BOILERPLATE_PATTERNS: Tuple["re.Pattern[str]", ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(?:©|\(c\)|copyright)\s*\d{4}",
        r"^\s*©",
        r"cookie",
        r"datenschutz",
        r"datenschutzerklärung",
        r"\bAGB\b",
        r"^\s*(?:nach\s+oben|zur[üu]ck\s+nach\s+oben|back\s+to\s+top)\s*$",
        r"^\s*footer\b",
        r"^\s*(?:startseite|home|homepage)\s*$",
        r"^\s*men[üu]\s*$",
        r"^\s*(?:deutsch|english|fran[çc]ais|español|italiano|nederlands|polski|russian|русский)\s*$",
        r"^\s*(?:language|sprache)\s*$",
        r"^\s*(?:skip to|zum inhalt|back to top)\b",
        r"^\s*(?:newsletter|kontaktanfrage|kontaktformular|zur[üu]ck)\s*$",
    )
)
#: Short navigational labels that are pure menu entries.
BOILERPLATE_LABELS = frozenset(
    {
        "impressum",
        "imprint",
        "kontakt",
        "contact",
        "datenschutz",
        "agb",
        "login",
        "anmelden",
        "home",
        "startseite",
        "blog",
        "karriere",
        "über uns",
        "about us",
        "nach oben",
        "zurück nach oben",
    }
)


class ImpressumError(RuntimeError):
    """The page could not be parsed (invalid HTML / nothing to extract)."""


@dataclass(frozen=True)
class OptimizationResult:
    """Outcome of :func:`optimize_text`, including the numbers to log."""

    text: str
    original: str
    cutoff: str
    head_anchor: Optional[str] = None
    tail_anchor: Optional[str] = None
    tail_hits: Tuple[str, ...] = ()
    margin: int = 0
    start: int = 0
    end: int = 0
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def chars_before(self) -> int:
        return len(self.original)

    @property
    def chars_after(self) -> int:
        return len(self.text)

    @property
    def tokens_before(self) -> int:
        return estimate_tokens(self.chars_before)

    @property
    def tokens_after(self) -> int:
        return estimate_tokens(self.chars_after)

    @property
    def saved_percent(self) -> float:
        if not self.chars_before:
            return 0.0
        return 100.0 * (self.chars_before - self.chars_after) / self.chars_before


def estimate_tokens(text_or_length: "int | str") -> int:
    length = text_or_length if isinstance(text_or_length, int) else len(text_or_length)
    return length // CHARS_PER_TOKEN


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Return an absolute http(s) URL, or None when there is no usable URL.

    ``www.example.de`` -> ``https://www.example.de``; an existing scheme is
    kept (``http://`` included).  Non-web schemes are rejected.
    """
    if url is None:
        return None
    text = url.strip()
    if not text:
        return None
    if "://" in text:
        scheme = text.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            return None
    elif ":" in text.split("/", 1)[0]:
        # "mailto:info@x.de" and friends are not web pages.
        return None
    else:
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    host = parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    if not host or host in (".", "..") or "." not in host and host != "localhost":
        return None
    return text


def is_same_host(base_url: str, candidate_url: str) -> bool:
    base = urlparse(base_url).netloc.lower()
    other = urlparse(candidate_url).netloc.lower()
    return base == other


def _link_label(anchor) -> str:
    return " ".join(anchor.get_text(" ", strip=True).split()).lower()


def find_impressum_link(html: str, base_url: str) -> Optional[str]:
    """Find the Impressum/Imprint URL of a home page.

    First pass: an ``<a>`` whose visible text is exactly ``impressum`` or
    ``imprint`` (case-insensitive).  Second pass: an ``<a>`` whose ``href``
    contains ``impressum``/``imprint``.  Relative links are resolved against
    ``base_url`` with :func:`urllib.parse.urljoin`.
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # noqa: BLE001 - malformed markup must not crash the batch
        raise ImpressumError(f"cannot parse home page: {exc}") from exc

    anchors = [a for a in soup.find_all("a", href=True) if a.get("href")]
    for anchor in anchors:
        if _link_label(anchor) in IMPRESSUM_WORDS:
            candidate = urljoin(base_url, anchor["href"].strip())
            if urlparse(candidate).scheme in ("http", "https"):
                return candidate
    for anchor in anchors:
        href = anchor["href"].strip().lower()
        if any(word in href for word in IMPRESSUM_WORDS):
            candidate = urljoin(base_url, anchor["href"].strip())
            if urlparse(candidate).scheme in ("http", "https"):
                return candidate
    return None


REMOVE_TAGS: Tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "header",
    "footer",
    "form",
)


def _normalize_whitespace(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_visible_text(html: str) -> str:
    """BeautifulSoup text of the page without scripts, navigation and footer.

    Per §4.3: remove ``script, style, noscript, svg, nav, header, footer,
    form``, then ``get_text()`` with whitespace normalized to one line per
    block.
    """
    if not html or not html.strip():
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # noqa: BLE001
        raise ImpressumError(f"cannot parse Impressum page: {exc}") from exc
    for tag in soup(REMOVE_TAGS):
        tag.decompose()
    return _normalize_whitespace(soup.get_text("\n"))


def _fold(text: str) -> str:
    """Lower-case and strip combining marks so 'gemäß' == 'gemass' etc."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _find_head_anchor(lowered: str) -> Tuple[Optional[str], int]:
    """Earliest strong head anchor; weak ones ("Impressum", "Anbieter") only as fallback."""
    for anchors in (STRONG_HEAD_ANCHORS, HEAD_ANCHORS):
        best: Tuple[Optional[str], int] = (None, -1)
        for anchor in anchors:
            offset = lowered.find(_fold(anchor))
            if offset != -1 and (best[1] == -1 or offset < best[1]):
                best = (anchor, offset)
        if best[1] != -1:
            return best
    return None, -1


def _tail_hits(text: str) -> List[Tuple[str, int, int]]:
    hits: List[Tuple[str, int, int]] = []
    for name, pattern in TAIL_ANCHORS:
        for match in pattern.finditer(text):
            hits.append((name, match.start(), match.end()))
    return hits


def _has_strong_signal(text: str) -> bool:
    return any(pattern.search(text) for _, pattern in TAIL_ANCHORS)


def _strip_boilerplate(lines: Sequence[str]) -> Tuple[str, List[str]]:
    """Drop obvious non-legal and repetitive lines; returns text and what was dropped."""
    kept: List[str] = []
    removed: List[str] = []
    seen: set = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.search(stripped) for pattern in BOILERPLATE_PATTERNS):
            removed.append(line)
            continue
        label = stripped.lower().strip(":. ")
        if label in BOILERPLATE_LABELS and label not in HEAD_ANCHORS:
            removed.append(line)
            continue
        # Legal data is short and unique; repeated long lines are feature
        # blocks, testimonials or lists of links.
        folded = stripped.lower()
        if len(stripped) > 80 and folded in seen:
            removed.append(line)
            continue
        seen.add(folded)
        if len(stripped) > MAX_LINE_CHARS and not _has_strong_signal(stripped):
            removed.append(line)  # endless filler paragraph without any legal datum
            continue
        kept.append(line)
    return "\n".join(kept), removed


def _contains_anchor(text: str, anchor: str) -> bool:
    return _fold(anchor) in _fold(text)


def _relaxed_sandwich(
    cleaned: str, max_chars: int, window_chars: int
) -> Optional[Tuple[int, int, int]]:
    """Largest verified block: head anchor (or start) + a strong signal, capped.

    Used when the default sandwich fails verification.  Tries the head anchor
    with growing windows around the strongest signal, then the whole text
    when it already fits the budget.
    """
    if len(cleaned) <= max_chars:
        return 0, len(cleaned), 0
    folded = _fold(cleaned)
    head_anchor, head_pos = _find_head_anchor(folded)
    hits = _tail_hits(cleaned)
    if not hits:
        return None
    start = head_pos if head_pos != -1 else 0
    half = max(1, window_chars // 2)
    for name, hit_start, hit_end in sorted(hits, key=lambda hit: hit[2]):
        candidate_start = max(start, hit_end - half)
        candidate_end = min(len(cleaned), max(hit_end, candidate_start + window_chars))
        candidate = cleaned[candidate_start:candidate_end]
        if (head_anchor is None or _contains_anchor(candidate, head_anchor)) and len(
            candidate
        ) <= max_chars:
            return candidate_start, candidate_end, 0
    # Last resort: the head anchor plus the first strong signal right after it.
    for name, hit_start, hit_end in sorted(hits, key=lambda hit: hit[2]):
        if hit_end > start:
            return start, min(len(cleaned), start + window_chars), 0
    return None


def _signals_window(
    cleaned: str, start: int, end: int, window_chars: int
) -> Tuple[int, int]:
    """Bounded window: the head anchor plus as many strong signals as fit."""
    hits = [hit for hit in _tail_hits(cleaned) if start <= hit[1] < max(end, len(cleaned))]
    if not hits:
        # Nothing to anchor on: leave only a bounded start of the text.
        return start, min(len(cleaned), start + window_chars)
    # The head anchor stays at the window start; the strong signal marks the
    # end.  If head..signal is wider than the window, something sits between
    # them that is not legal data: keep the part around the last signal.
    last_end = max(hit[2] for hit in hits)
    if last_end - start <= window_chars:
        return start, last_end
    return max(0, last_end - window_chars), last_end


def optimize_text(
    text: str,
    max_chars: int = MAX_TEXT_CHARS,
    tail_margin: int = TAIL_MARGIN,
    max_tail_margin: int = MAX_TAIL_MARGIN,
    window_chars: int = WINDOW_CHARS,
) -> OptimizationResult:
    """Cut an over-long Impressum text down to the legally relevant block.

    Strategy, in order:

    1. Lines under ``max_chars`` are returned unchanged.
    2. Obvious boilerplate (cookie banners, Datenschutz/AGB menu entries,
       "© 2024", language switchers, short menu labels) is dropped.
    3. *Head anchor*: the first Impressum heading ("Impressum", "Angaben
       gemäß § 5 TMG/DDG", "Anbieter", "Diensteanbieter") opens the legal
       section; the menu/hero text before it is cut.
    4. *Tail anchors*: the last occurrence of a legally required datum
       (phone starting with +49/0049, VAT id ``DE`` + 9 digits, e-mail,
       HRB/HRA/Registergericht, "Vertreten durch"/Geschäftsführer/…)
       marks the end of the legal data; ``tail_margin`` characters after it
       are kept, the footer is cut.
    5. The sandwich ``[head_anchor : tail_match_end + margin]`` is
       verified: it must still contain the head anchor and at least one
       strong signal; otherwise the margin grows (up to
       ``max_tail_margin``), then the cut is relaxed to the whole cleaned
       text.
    6. If the result is still longer than ``max_chars`` (rare), a head+tail
       window (first and last ``window_chars / 2`` characters) is used.
    """
    original = text
    if len(text) <= max_chars:
        return OptimizationResult(
            text=text,
            original=original,
            cutoff="none (under threshold)",
            notes=("text is under the threshold; sent unchanged",),
        )

    notes: List[str] = []
    cleaned, removed = _strip_boilerplate(text.splitlines())
    if removed:
        notes.append(f"stripped {len(removed)} boilerplate line(s)")

    folded = _fold(cleaned)
    head_anchor, head_pos = _find_head_anchor(folded)
    start = head_pos if head_pos != -1 else 0
    if head_anchor is not None:
        notes.append(f"head anchor {head_anchor!r} at char {head_pos}")
    else:
        notes.append("no head anchor: kept from the beginning")

    tail = cleaned[start:]
    tail_offsets = _tail_hits(tail)
    margin = 0
    end = len(cleaned)
    tail_anchor = None
    limit = min(max_tail_margin, max(0, max_chars - 1))
    if tail_offsets:
        # The last legal datum marks the end of the legal block; keep the
        # default margin after it, growing step by step while that is still
        # under the threshold (e.g. "Details & Preise", "Kontaktformular").
        tail_anchor, _, last_end = max(tail_offsets, key=lambda hit: hit[2])
        margin = max(0, tail_margin)
        while True:
            end = min(start + last_end + margin, len(cleaned))
            candidate = cleaned[start:end]
            if len(candidate) <= max_chars or margin >= limit:
                break
            margin = min(limit, int(margin * 1.5) + tail_margin)
        end = min(end, len(cleaned))
        notes.append(
            f"last tail anchor {tail_anchor!r}; kept {margin} chars after it"
        )
    else:
        notes.append("no tail anchor: kept everything after the head anchor")

    candidate = cleaned[start:end]
    has_head = head_anchor is None or _contains_anchor(candidate, head_anchor)
    if not (_has_strong_signal(candidate) and has_head):
        # Verification failed: relax to the largest block that still passes
        # the "head anchor + one strong signal" check, then use the window.
        relaxed = _relaxed_sandwich(cleaned, max_chars, window_chars)
        if relaxed is not None:
            start, end, margin = relaxed
            candidate = cleaned[start:end]
            notes.append("verification failed: relaxed to the largest verified block")
        else:
            start, end = _signals_window(cleaned, start, end, window_chars)
            margin = 0
            candidate = cleaned[start:end]
            notes.append("verification failed: used the window around the strongest signals")

    if len(candidate) > max_chars > 0:
        # Rare: the verified block itself is still longer than the budget.
        window_start, window_end = _signals_window(cleaned, start, end, window_chars)
        candidate = cleaned[window_start:window_end]
        start, end = window_start, window_end
        notes.append(f"result still long: window of at most {window_chars} chars")

    if len(candidate) > max_chars > 0:
        # Safety net: a signed slice, so the caller never sends more than the
        # budget even for pathological pages without any strong signal.
        candidate = candidate[:max_chars]
        notes.append(f"document truncated to the {max_chars}-character budget")

    hits = _tail_hits(candidate)
    return OptimizationResult(
        text=candidate,
        original=original,
        cutoff="sandwich" if margin else ("head" if head_anchor else "window"),
        head_anchor=head_anchor,
        tail_anchor=tail_anchor,
        tail_hits=tuple(sorted({name for name, _, _ in hits})),
        margin=margin,
        start=start,
        end=end,
        notes=tuple(notes),
    )


def log_summary(result: OptimizationResult) -> str:
    """One-line summary for the per-company log."""
    return (
        "chars %d -> %d (tokens ~%d -> ~%d, saved %.0f%%); cutoff=%s; anchors=%s"
        % (
            result.chars_before,
            result.chars_after,
            result.tokens_before,
            result.tokens_after,
            result.saved_percent,
            result.cutoff,
            ", ".join(result.tail_hits) or "none",
        )
    )


def iter_anchor_names(text: str) -> Iterable[str]:
    """Names of all tail anchors present in a text (used by tests/logs)."""
    return (name for name, _, _ in _tail_hits(text))
