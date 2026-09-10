"""Download a Scribd embed page and save it as a PDF.

Usage:
    python scribdefiler.py <document_id>

Example:
    python scribdefiler.py 582827860
    -> saves 582827860.pdf in the current directory
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

URL_TEMPLATE = "https://www.scribd.com/embeds/{doc_id}/content?view_mode=scroll"

# Osano (Scribd's cookie consent vendor) plus a few generic fallbacks.
CONSENT_SELECTORS = [
    ".osano-cm-accept-all",
    ".osano-cm-accept",
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("I Accept")',
    'button:has-text("Agree")',
]

# Scroll the inner .document_scroller container (the real scroller - body
# scrollHeight is 0) from top to bottom so every page lazy-loads its content.
# scrollHeight grows while we scroll, so re-read it every iteration and only
# stop once the bottom has been reached and the height stops growing.
SCROLL_SCRIPT = r"""
async ({ stepPause, maxIterations, stableRounds }) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const scroller =
        document.querySelector('.document_scroller') ||
        document.scrollingElement ||
        document.documentElement;

    let iterations = 0;
    let stable = 0;
    while (iterations < maxIterations) {
        iterations += 1;
        const heightBefore = scroller.scrollHeight;
        const step = Math.max(400, Math.floor(scroller.clientHeight * 0.9));
        scroller.scrollTop = Math.min(scroller.scrollTop + step, heightBefore);
        await sleep(stepPause);

        const atBottom =
            scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
        if (!atBottom) {
            stable = 0;
            continue;
        }
        // At the bottom: give lazy-loading a chance to append more pages.
        await sleep(stepPause * 3);
        stable = scroller.scrollHeight > heightBefore ? 0 : stable + 1;
        if (stable >= stableRounds) break;
    }

    scroller.scrollTop = 0;
    await sleep(400);
    return {
        iterations,
        exhausted: iterations >= maxIterations && stable < stableRounds,
        height: scroller.scrollHeight,
        pages: document.querySelectorAll('.outer_page').length,
    };
};
"""

# Wait until every .outer_page has actually rendered: all of its images decoded
# (complete + naturalWidth > 0) and no page left visually empty. Polling this is
# what keeps half-rendered pages out of the PDF.
WAIT_RENDERED_SCRIPT = r"""
async ({ timeoutMs, pollMs }) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const imageReady = (img) => img.complete && img.naturalWidth > 0;
    const pageBlank = (page) => {
        if (page.querySelector('canvas')) return false;
        const imgs = Array.from(page.querySelectorAll('img'));
        if (imgs.some(imageReady)) return false;
        return page.textContent.trim().length === 0;
    };
    const snapshot = () => {
        const pages = Array.from(document.querySelectorAll('.outer_page'));
        const imgs = Array.from(document.querySelectorAll('.outer_page img'));
        return {
            pages: pages.length,
            images: imgs.length,
            pendingImages: imgs.filter((i) => !imageReady(i)).length,
            blankPages: pages.filter(pageBlank).length,
        };
    };

    const deadline = Date.now() + timeoutMs;
    let last = snapshot();
    while (Date.now() < deadline) {
        last = snapshot();
        if (last.pages && !last.pendingImages && !last.blankPages) {
            return { ...last, ok: true };
        }
        await sleep(pollMs);
    }
    return { ...last, ok: false };
};
"""

# Tuning knobs for the two scripts above.
SCROLL_STEP_PAUSE_MS = 250
SCROLL_MAX_ITERATIONS = 2000
SCROLL_STABLE_ROUNDS = 3
RENDER_TIMEOUT_MS = 45_000
RENDER_POLL_MS = 500
MAX_SCROLL_PASSES = 3

# Flatten the inner-scroll layout so all pages flow into the document body,
# hide Scribd / Osano chrome, and force one Scribd page per PDF page.
FLATTEN_CSS = r"""
html, body {
    height: auto !important;
    max-height: none !important;
    overflow: visible !important;
    margin: 0 !important;
    padding: 0 !important;
    background: white !important;
}
.document_scroller,
[class*="document_scroller"],
[class*="doc_container"] {
    height: auto !important;
    max-height: none !important;
    min-height: 0 !important;
    overflow: visible !important;
    position: static !important;
}
/* Hide cookie consent + any Scribd chrome (header/footer/download bar) */
.osano-cm-window, .osano-cm-dialog, .osano-cm-info-dialog,
[class*="osano-cm"],
.global_header, .embed_header, .embed_footer,
[class*="toolbar"], [class*="banner"],
[id*="download"], [class*="download_button"], [class*="download-button"],
a[href*="download"], button[class*="download" i] {
    display: none !important;
}
/* Each Scribd page becomes exactly one PDF page */
.outer_page {
    page-break-after: always !important;
    break-after: page !important;
    margin: 0 auto !important;
    box-shadow: none !important;
}
.outer_page:last-child {
    page-break-after: auto !important;
    break-after: auto !important;
}
"""


async def fetch_pdf(doc_id: str, out_path: Path) -> None:
    url = URL_TEMPLATE.format(doc_id=doc_id)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1200, "height": 1600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            print(f"[1/5] Loading {url}", file=sys.stderr)
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                pass

            print("[2/5] Dismissing cookie consent if present...", file=sys.stderr)
            for sel in CONSENT_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() and await btn.is_visible():
                        await btn.click(timeout=2_000)
                        await page.wait_for_timeout(400)
                        break
                except Exception:
                    continue

            print("[3/5] Forcing all pages to lazy-load (scrolling inner container)...", file=sys.stderr)
            render = None
            for attempt in range(1, MAX_SCROLL_PASSES + 1):
                scroll = await page.evaluate(
                    SCROLL_SCRIPT,
                    {
                        "stepPause": SCROLL_STEP_PAUSE_MS,
                        "maxIterations": SCROLL_MAX_ITERATIONS,
                        "stableRounds": SCROLL_STABLE_ROUNDS,
                    },
                )
                if scroll["exhausted"]:
                    print(
                        f"        Warning: scroll pass {attempt} hit the "
                        f"{SCROLL_MAX_ITERATIONS}-iteration cap; the document may be truncated.",
                        file=sys.stderr,
                    )
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except PWTimeout:
                    pass

                render = await page.evaluate(
                    WAIT_RENDERED_SCRIPT,
                    {"timeoutMs": RENDER_TIMEOUT_MS, "pollMs": RENDER_POLL_MS},
                )
                print(
                    f"        Pass {attempt}: {render['pages']} pages, "
                    f"{render['images'] - render['pendingImages']}/{render['images']} images loaded, "
                    f"{render['blankPages']} blank.",
                    file=sys.stderr,
                )
                if render["ok"]:
                    break
            else:
                print(
                    f"        Warning: after {MAX_SCROLL_PASSES} passes "
                    f"{render['pendingImages']} images are still loading and "
                    f"{render['blankPages']} pages look blank - exporting anyway.",
                    file=sys.stderr,
                )

            print("[4/5] Measuring page dimensions and flattening layout...", file=sys.stderr)
            dims = await page.evaluate(
                """
                () => {
                    const ps = document.querySelectorAll('.outer_page');
                    if (!ps.length) return null;
                    let maxW = 0, maxH = 0;
                    ps.forEach((p) => {
                        const r = p.getBoundingClientRect();
                        if (r.width  > maxW) maxW = r.width;
                        if (r.height > maxH) maxH = r.height;
                    });
                    // Tiny buffer so a sub-pixel rounding error doesn't push the
                    // last few pixels of a page onto a second PDF page.
                    return { w: Math.ceil(maxW), h: Math.ceil(maxH) + 4, count: ps.length };
                }
                """
            )
            if not dims:
                raise RuntimeError("No .outer_page elements found - Scribd page structure has changed.")
            print(f"        Found {dims['count']} pages at {dims['w']}x{dims['h']}px each.", file=sys.stderr)

            await page.add_style_tag(content=FLATTEN_CSS)
            await page.wait_for_timeout(600)

            print(f"[5/5] Writing PDF -> {out_path}", file=sys.stderr)
            await page.emulate_media(media="screen")
            await page.pdf(
                path=str(out_path),
                print_background=True,
                width=f"{dims['w']}px",
                height=f"{dims['h']}px",
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
        finally:
            await browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save a Scribd embed as a PDF.")
    parser.add_argument("doc_id", help="Scribd document ID, e.g. 582827860")
    args = parser.parse_args(argv)

    out_path = Path.cwd() / f"{args.doc_id}.pdf"
    asyncio.run(fetch_pdf(args.doc_id, out_path))
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
