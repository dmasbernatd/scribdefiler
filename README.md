# scribdefiler

A small CLI that takes a Scribd document ID, fetches the embed page with headless Chromium, and saves the full document as a multi-page PDF — preserving real text, links, and visuals where Scribd renders them as HTML.

## Requirements

- Python 3.10 or newer
- Roughly 500 MB free disk space for the bundled Chromium that Playwright installs
- An internet connection (and a Scribd document that's actually viewable in the public embed — paywalled previews will only export their preview portion)

## Install

```
python -m pip install -r requirements.txt
python -m playwright install chromium
```

The second command downloads a Chromium build that Playwright drives. You only need to run it once.

## Usage

```
python scribdefiler.py <document_id>
```

The document ID is the number in a Scribd embed URL. For example, given
`https://www.scribd.com/embeds/582827860/content`, the ID is `582827860`.

Example:

```
python scribdefiler.py 582827860
```

This writes `582827860.pdf` into the current working directory. Expect roughly one minute per ~80 pages on a typical machine.

## How it works

1. Loads `https://www.scribd.com/embeds/<id>/content?view_mode=scroll` in headless Chromium.
2. Dismisses the Osano cookie-consent dialog if present.
3. Scrolls Scribd's inner `.document_scroller` container top-to-bottom so every page lazy-loads its content into the DOM. The container grows as pages arrive, so the scroll loop re-measures it on every step and only stops once the bottom is reached and the height has stopped changing. It then waits until every page has actually rendered (all images decoded, no blank pages), retrying the whole pass up to three times and warning on stderr if anything is still missing.
4. Injects CSS that flattens the inner-scroll layout, hides Scribd chrome (header, footer, download button), and forces one Scribd page per PDF page.
5. Measures the rendered `.outer_page` dimensions and calls Chromium's built-in PDF export at exactly those dimensions, with zero margins.

## Known quirks

- **Two blank pages.** The PDF typically has one blank page bracketing each end of the content. Cosmetic; trim with any PDF tool if it bothers you.
- **Large files.** Scribd embeds rasterized page backgrounds at high DPI, so a 160-page document is around 125 MB. Running it through `ghostscript` or `pikepdf` after export will compress it substantially if size matters.
- **Paywalled documents.** If the embed only shows a preview in a regular browser, that's all the script will capture too — it doesn't bypass access controls.

## Use responsibly

This tool only retrieves what Scribd's public embed endpoint already serves to any browser. Respect copyright and Scribd's terms of service for whatever you do with the output.
