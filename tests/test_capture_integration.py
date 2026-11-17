"""Playwright capture test: __zoomAt / F5 / ring / sidecar.

Skipped when the Playwright browsers are not installed.
"""

import time
from urllib.parse import quote

import pytest

pytestmark = pytest.mark.integration

sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = sync_api.sync_playwright

import qscript  # noqa: E402  (imported after the Playwright availability check)
from zoom_sidecar import load_sidecar  # noqa: E402

HTML = (
    "<!doctype html><html><body style='margin:0;height:100vh;background:#ddd'>"
    "<input id=x value='keep me'></body></html>"
)


def test_zoom_hook_f5_and_sidecar(tmp_path):
    qscript._reset_zoom_state()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True, channel="chromium")
            except Exception as exc:  # pragma: no cover - browser not installed
                pytest.skip(f"Playwright Chromium is not available: {exc}")
            try:
                context = browser.new_context(viewport=dict(qscript.VIDEO_SIZE))
                context.add_init_script(
                    "window.__PW_ZOOM_FACTOR = 2.0;\n" + qscript.OVERLAY_JS
                )
                page = context.new_page()
                qscript._zoom_t0 = time.monotonic()
                qscript._active_page = page
                page.expose_binding("__zoomEvent", qscript._record_zoom_event)
                page.goto("data:text/html," + quote(HTML))
                page.wait_for_timeout(150)

                assert page.evaluate("typeof window.__zoomAt") == "function"

                page.evaluate("window.__zoomAt(100, 120)")
                page.wait_for_timeout(120)
                rings = page.evaluate(
                    "[...document.querySelectorAll('div')].filter("
                    "d => d.style.borderRadius === '50%' && d.style.border).length"
                )
                assert rings >= 1  # the colored on-screen indicator
                assert [event["type"] for event in qscript._zoom_events] == ["in"]

                page.keyboard.press("F5")
                page.wait_for_timeout(150)
                assert [event["type"] for event in qscript._zoom_events] == ["in", "out"]
                assert qscript._zoom_events[0]["x"] == 100.0
                assert qscript._zoom_events[0]["y"] == 120.0
                # F5 must not reload the page (the input is still there).
                assert page.locator("#x").count() == 1
            finally:
                qscript._active_page = None
                browser.close()
    finally:
        qscript._reset_zoom_state()

    video = tmp_path / "clip.webm"
    video.write_bytes(b"not-a-real-video")
    # The helper only needs the events + the destination path.
    qscript._zoom_events[:] = [
        {"t": 1.0, "x": 100.0, "y": 120.0, "type": "in"},
        {"t": 2.0, "x": 100.0, "y": 120.0, "type": "out"},
    ]
    sidecar_path = qscript._write_zoom_sidecar(video)
    assert sidecar_path is not None
    sidecar = load_sidecar(sidecar_path)
    assert [event.type for event in sidecar.events] == ["in", "out"]
    assert sidecar.viewport == (1280.0, 720.0)
