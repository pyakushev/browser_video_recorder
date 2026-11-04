"""qscript.py - browser automation scenarios that are recorded to video.

Public API
----------
PATH_TO_VIDEO_FOLDER   global: where the .webm files are written
HEADLESS               global: run whether with window or not
CaptureVideo_Magento   runs the sample scenario and returns the video path
ShowCallout            shows a text callout on the page being recorded
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

#: Videos are saved here (created if missing). The main script may override it.
PATH_TO_VIDEO_FOLDER = "./video"

#: Run Chromium without a window. capture_video.py can switch it off (--headed).
HEADLESS = True

VIDEO_SIZE = {"width": 1280, "height": 720}

SEARCH_BOX_SELECTOR = 'textarea[name="q"]'
SEARCH_BUTTON_SELECTOR = 'form button[type="submit"]:visible'
FIRST_RESULT_SELECTOR = 'a[data-testid="result-title-a"]'

NAV_TIMEOUT_MS = 45_000
ELEMENT_TIMEOUT_MS = 30_000
RESULTS_TIMEOUT_MS = 60_000  # results are rendered client-side and can be slow
FINAL_HOLD_MS = 3_000  # keep recording the page that opened after the click

# Injected overlay: SVG cursor + yellow highlight + click ripple + callout
OVERLAY_JS = r"""
(() => {
  "use strict";
  if (window.top !== window || window.__pwOverlay) return;

  const Z = 2147483647;            // topmost
  const KEY = "__pw_cursor_pos";   // survives same-origin navigations
  const SVG_NS = "http://www.w3.org/2000/svg";
  const HL_R = 32;                 // highlight radius ~ cursor width

  const root = document.createElement("div");
  root.id = "__pw_overlay";
  root.style.cssText = "position:fixed;inset:0;margin:0;padding:0;border:0;" +
    "background:none;pointer-events:none;overflow:hidden;z-index:" + Z + ";";

  // Yellow semi-transparent circle, centered on the pointer, behind the arrow.
  const hl = document.createElement("div");
  hl.style.cssText = "position:absolute;left:0;top:0;width:" + (HL_R * 2) +
    "px;height:" + (HL_R * 2) + "px;border-radius:50%;" +
    "background:rgba(255,235,59,0.40);pointer-events:none;display:none;" +
    "will-change:transform;";

  // White arrow with a black outline, ~32x32, tip at (1,1) of its box.
  const arrow = document.createElement("div");
  arrow.style.cssText = "position:absolute;left:0;top:0;width:32px;height:32px;" +
    "pointer-events:none;display:none;will-change:transform;";
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", "32");
  svg.setAttribute("height", "32");
  svg.setAttribute("viewBox", "0 0 32 32");
  svg.style.cssText = "overflow:visible;display:block;";
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M1 1 L1 26 L7.2 20.6 L11.4 30.4 L15.6 28.6 L11.5 19 L19.8 19 Z");
  path.setAttribute("fill", "#ffffff");
  path.setAttribute("stroke", "#000000");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  arrow.appendChild(svg);

  root.appendChild(hl);
  root.appendChild(arrow);

  let shown = false;

  const place = (x, y) => {
    hl.style.transform = "translate(" + (x - HL_R) + "px," + (y - HL_R) + "px)";
    arrow.style.transform = "translate(" + (x - 1) + "px," + (y - 1) + "px)";
    if (!shown) { hl.style.display = "block"; arrow.style.display = "block"; shown = true; }
  };

  const set = (x, y) => {
    place(x, y);
    try { sessionStorage.setItem(KEY, JSON.stringify({ x: x, y: y })); } catch (e) {}
  };

  // Click ripple: a wave grows from the click point to ~cursor width and fades.
  // Size is animated (not scale) so the 4px ring keeps its thickness.
  const ripple = (x, y) => {
    const r = document.createElement("div");
    r.style.cssText = "position:absolute;box-sizing:border-box;border-radius:50%;" +
      "border:4px solid rgba(220,38,18,1);background:rgba(220,38,18,0.25);" +
      "pointer-events:none;";
    root.insertBefore(r, arrow);   // above the highlight, below the arrow
    const d0 = 8, d1 = HL_R * 2;
    const a = r.animate([
      { left: (x - d0 / 2) + "px", top: (y - d0 / 2) + "px", width: d0 + "px", height: d0 + "px", opacity: 1 },
      { left: (x - d1 / 2) + "px", top: (y - d1 / 2) + "px", width: d1 + "px", height: d1 + "px", opacity: 0.85, offset: 0.55 },
      { left: (x - d1 / 2) + "px", top: (y - d1 / 2) + "px", width: d1 + "px", height: d1 + "px", opacity: 0 }
    ], { duration: 700, easing: "ease-out" });
    a.onfinish = () => r.remove();
  };

  // Callout: yellow box, black 16pt text, auto-sized, fade in / hold / fade out.
  const callout = (text, bg, fg, fadeMs, holdMs) => {
    attach();
    const old = root.querySelector("#__pw_callout");
    if (old) old.remove();
    const el = document.createElement("div");
    el.id = "__pw_callout";
    el.textContent = text;
    el.style.cssText = "position:absolute;top:100px;left:50%;transform:translateX(-50%);" +
      "width:max-content;max-width:88%;box-sizing:border-box;padding:12px 22px;" +
      "border:2px solid #000;border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,0.35);" +
      "font:16pt/1.3 Arial,Helvetica,sans-serif;text-align:center;" +
      "pointer-events:none;opacity:0;";
    el.style.background = bg;
    el.style.color = fg;
    root.appendChild(el);
    let anim;
    if (fadeMs > 0) {
      const total = 2 * fadeMs + holdMs;
      anim = el.animate([
        { opacity: 0, offset: 0 },
        { opacity: 1, offset: fadeMs / total },
        { opacity: 1, offset: (fadeMs + holdMs) / total },
        { opacity: 0, offset: 1 }
      ], { duration: total, fill: "forwards" });
    } else {
      anim = el.animate([{ opacity: 1 }, { opacity: 1 }], { duration: holdMs, fill: "forwards" });
    }
    anim.onfinish = () => el.remove();
  };

  // The init script may run before <html> exists: keep (re)attaching the overlay.
  const attach = () => {
    const p = document.documentElement;
    if (p && root.parentNode !== p) p.appendChild(root);
  };
  attach();
  try { new MutationObserver(attach).observe(document, { childList: true }); } catch (e) {}
  document.addEventListener("DOMContentLoaded", attach);
  window.addEventListener("load", attach);
  setInterval(attach, 250);

  // Real mouse events drive the overlay.
  window.addEventListener("mousemove", (e) => set(e.clientX, e.clientY),
    { capture: true, passive: true });
  window.addEventListener("mousedown", (e) => { set(e.clientX, e.clientY); ripple(e.clientX, e.clientY); },
    { capture: true, passive: true });

  // After a navigation show the cursor where it was before.
  try {
    const s = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (s && isFinite(s.x) && isFinite(s.y)) place(s.x, s.y);
  } catch (e) {}

  window.__pwOverlay = { set: set, callout: callout };
})();
"""

_active_page: Optional[Page] = None
_cursor: Dict[str, float] = {"x": 0.0, "y": 0.0}


def _evaluate_retry(page: Page, expression: str, arg: object = None, attempts: int = 6) -> object:
    """page.evaluate that survives a navigation happening at the same moment."""
    last: Optional[PlaywrightError] = None
    for _ in range(attempts):
        try:
            return page.evaluate(expression, arg)
        except PlaywrightError as exc:
            message = str(exc).lower()
            if "context was destroyed" in message or "navigat" in message:
                last = exc
                page.wait_for_timeout(300)
                continue
            raise
    assert last is not None
    raise last


def _sync_cursor(page: Page) -> None:
    """Put the overlay cursor at the real mouse position (new page / new origin)."""
    try:
        _evaluate_retry(
            page,
            "([x, y]) => { if (window.__pwOverlay) window.__pwOverlay.set(x, y); }",
            [_cursor["x"], _cursor["y"]],
        )
    except PlaywrightError as exc:
        logger.debug("cursor sync failed: %s", exc)


def ShowCallout(
    text: str,
    bgColor: str = "#FFEB3B",
    textColor: str = "#000000",
    fade: Union[bool, float] = True,
    hold: float = 3.0,
) -> None:
    """Show a callout on the page that is being recorded (non-blocking).

    fade: True -> fade in/out 2 s each, False -> no fade, a number -> that many
    seconds for each of fade-in and fade-out. hold: seconds fully visible.
    """
    page = _active_page
    if page is None:
        return
    if isinstance(fade, bool):
        fade_s = 2.0 if fade else 0.0
    else:
        fade_s = max(0.0, float(fade))
    try:
        _evaluate_retry(
            page,
            "([t, bg, fg, f, h]) => { if (window.__pwOverlay) window.__pwOverlay.callout(t, bg, fg, f, h); }",
            [text, bgColor, textColor, int(fade_s * 1000), int(hold * 1000)],
        )
    except PlaywrightError as exc:  # a callout is decoration, never fatal
        logger.warning("callout failed: %s", exc)


def _glide_to(page: Page, tx: float, ty: float) -> None:
    """Move the mouse along a slightly curved path with ease-in-out (no teleporting)."""
    sx, sy = _cursor["x"], _cursor["y"]
    dist = math.hypot(tx - sx, ty - sy)
    if dist < 1.0:
        return
    duration = min(1.6, max(0.5, 0.35 + dist / 1400.0))  # seconds
    # Quadratic Bezier with a control point pushed sideways a little.
    nx, ny = -(ty - sy) / dist, (tx - sx) / dist
    offset = random.uniform(-0.12, 0.12) * dist
    cx, cy = (sx + tx) / 2 + nx * offset, (sy + ty) / 2 + ny * offset
    steps = max(15, int(duration * 60))
    pause_ms = duration * 1000.0 / steps
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)  # smoothstep
        x = (1 - e) ** 2 * sx + 2 * (1 - e) * e * cx + e ** 2 * tx
        y = (1 - e) ** 2 * sy + 2 * (1 - e) * e * cy + e ** 2 * ty
        page.mouse.move(x, y)
        _cursor["x"], _cursor["y"] = x, y
        page.wait_for_timeout(pause_ms)


def _target_point(box: Dict[str, float], aim: str) -> Tuple[float, float]:
    if aim == "start":  # inside the first line of a (possibly wrapped) link
        return box["x"] + min(box["width"] * 0.4, 160.0), box["y"] + min(box["height"] / 2, 16.0)
    return (
        box["x"] + box["width"] * random.uniform(0.4, 0.6),
        box["y"] + box["height"] * random.uniform(0.4, 0.6),
    )


def _click_element(page: Page, locator: Locator, aim: str = "center") -> None:
    """Glide to the element and click it with the real mouse."""
    locator.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    locator.scroll_into_view_if_needed(timeout=ELEMENT_TIMEOUT_MS)
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError("element has no bounding box: %s" % locator)
    tx, ty = _target_point(box, aim)
    _glide_to(page, tx, ty)
    page.wait_for_timeout(random.uniform(180, 380))
    page.mouse.click(tx, ty, delay=random.uniform(60, 110))


def _type_humanlike(page: Page, text: str) -> None:
    """Type symbol by symbol with irregular delays."""
    for ch in text:
        page.keyboard.type(ch)
        delay = random.uniform(70, 180)
        if ch == " ":
            delay += random.uniform(30, 90)
        if random.random() < 0.06:  # occasional hesitation
            delay += random.uniform(150, 350)
        page.wait_for_timeout(delay)


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {
    "LPT%d" % i for i in range(1, 10)
}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")[:150].strip()
    if not cleaned:
        return "video"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def _user_agent(browser_version: str) -> str:
    """A regular Chrome UA (the default headless one contains 'HeadlessChrome')."""
    major = browser_version.split(".")[0]
    if sys.platform.startswith("win"):
        platform = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform = "X11; Linux x86_64"
    return (
        "Mozilla/5.0 (%s) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/%s.0.0.0 Safari/537.36" % (platform, major)
    )

def _run_scenario(page: Page, url: str, city: str) -> None:
    page.set_default_timeout(ELEMENT_TIMEOUT_MS)

    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    search_box = page.locator(SEARCH_BOX_SELECTOR).first
    search_box.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)

    # Show the custom cursor (the first real mouse move makes it appear).
    start_x, start_y = random.uniform(120, 260), random.uniform(480, 600)
    page.mouse.move(start_x, start_y)
    _cursor["x"], _cursor["y"] = start_x, start_y
    page.wait_for_timeout(700)

    city = " ".join((city or "").split())
    query = (f"Magento developer in {city}") if city else "Magento developer"
    ShowCallout(f"Searching for: {query}")

    _click_element(page, search_box)

    page.wait_for_timeout(300)
    _type_humanlike(page, query)
    page.wait_for_timeout(500)

    _click_element(page, page.locator(SEARCH_BUTTON_SELECTOR).first)

    try:
        page.wait_for_selector(FIRST_RESULT_SELECTOR, state="visible", timeout=RESULTS_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        raise RuntimeError(
            "no search results within %d s (url=%s); the site may be showing a bot challenge"
            % (RESULTS_TIMEOUT_MS // 1000, page.url)
        )
    page.wait_for_timeout(800)
    _sync_cursor(page)
    
    ShowCallout("Opening the first result")
    link = page.locator(FIRST_RESULT_SELECTOR).first
    link.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    # Keep a single tab (and therefore a single video file).
    link.evaluate("el => el.removeAttribute('target')")
    results_url = page.url
    _click_element(page, link, aim="start")

    def left_results(u: str) -> bool:
        return u != results_url

    try:
        page.wait_for_url(left_results, timeout=10_000, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        logger.warning("mouse click did not navigate, falling back to locator.click()")
        link.click(timeout=5_000)
        page.wait_for_url(left_results, timeout=30_000, wait_until="domcontentloaded")

    _sync_cursor(page)
    page.wait_for_timeout(FINAL_HOLD_MS)


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    """Record the sample scenario and return the absolute path to the .webm file.

    Returns None on failure. ``language``, ``country``, ``postcode``, ``street``
    and ``house_no`` are accepted for the common interface but unused by this
    sample behavior; ``city`` goes into the query, ``company_name`` names the file.
    """
    global _active_page

    try:
        video_dir = Path(PATH_TO_VIDEO_FOLDER).expanduser().resolve()
        video_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix=".rec_", dir=str(video_dir)))
    except OSError as exc:
        logger.error("cannot prepare video folder %r: %s", PATH_TO_VIDEO_FOLDER, exc)
        return None

    final_path: Optional[Path] = None
    try:
        with sync_playwright() as pw:
            launch_kwargs: Dict[str, object] = {
                "headless": HEADLESS,
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            if HEADLESS:
                launch_kwargs["channel"] = "chromium"
            browser = pw.chromium.launch(**launch_kwargs)  # type: ignore[arg-type]
            try:
                context = browser.new_context(
                    viewport=dict(VIDEO_SIZE),
                    record_video_dir=str(tmp_dir),
                    record_video_size=dict(VIDEO_SIZE),
                    user_agent=_user_agent(browser.version),
                    locale="en-US",
                )
                context.add_init_script(OVERLAY_JS)
                page = context.new_page()
                video = page.video
                _cursor["x"], _cursor["y"] = 0.0, 0.0
                _active_page = page
                succeeded = False
                try:
                    _run_scenario(page, url, city)
                    succeeded = True
                finally:
                    _active_page = None
                    context.close()

                if succeeded and video is not None:
                    raw_path = Path(video.path())
                    final_path = video_dir / (_safe_filename(company_name) + ".webm")
                    os.replace(str(raw_path), str(final_path))
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - the contract is "path or None"
        logger.error("capture failed for %r (%s): %s", company_name, url, exc)
        final_path = None
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

    return str(final_path) if final_path is not None else None
