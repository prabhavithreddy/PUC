"""
DOM-Extract Engine — Pure JS Table Extraction
==============================================
Strategy: Playwright navigates to page → locates the table → extracts ALL
rows directly via JavaScript (no LLM involved in data capture).

Links are resolved to absolute URLs in the browser so no domain guessing
is needed.  Every column is preserved as-is in the metadata dict.

Registered as engine="sonnet" in __init__.py.
"""

import os
import json
import asyncio
from typing import List, Optional

from playwright.async_api import Page
from .base import BaseExtractor


# ─────────────────────────────────────────────────────────────────
# JS that extracts every table row with all columns + resolved links
# ─────────────────────────────────────────────────────────────────

_EXTRACT_JS = """
(selector) => {
    const table = document.querySelector(selector);
    if (!table) return [];

    // Resolve all <a href> to absolute using browser's own href property
    Array.from(table.querySelectorAll('a[href]')).forEach(a => a.href = a.href);

    // Collect header labels
    const headers = Array.from(
        table.querySelectorAll('thead th, thead td, tr:first-child th')
    ).map(h => h.innerText.trim());

    const results = [];

    table.querySelectorAll('tbody tr, tr').forEach(row => {
        const cells = row.querySelectorAll('td, th');
        if (cells.length === 0) return;

        const cellData = Array.from(cells).map((td, i) => ({
            header: headers[i] || `col_${i}`,
            text:   td.innerText.trim().replace(/\\n/g, ' '),
            urls:   Array.from(td.querySelectorAll('a[href]')).map(a => a.href)
        }));

        // Skip pure header rows (all cells match their header label)
        if (cellData.every(c => !c.text || c.text === c.header)) return;

        // Most text-rich cell → title
        const titleCell = cellData.reduce(
            (best, c) => c.text.length > best.text.length ? c : best,
            cellData[0]
        );

        // First date-like cell → date
        const dateCell = cellData.find(c =>
            /date|filed|received|submitted/i.test(c.header) ||
            /\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}/.test(c.text) ||
            /\\d{4}-\\d{2}-\\d{2}/.test(c.text)
        );

        // First URL across the whole row
        const firstUrl = cellData.flatMap(c => c.urls)[0] || '';

        // All remaining cells → metadata dict
        const metadata = {};
        cellData.forEach(c => {
            if (c.text && c.text !== titleCell.text)
                metadata[c.header] = c.text;
        });

        results.push({
            title:    titleCell.text || 'Document',
            date:     dateCell ? dateCell.text : '',
            url:      firstUrl,
            metadata: metadata
        });
    });

    return results;
}
"""


# ─────────────────────────────────────────────────────────────────
# SonnetExtractor — BaseExtractor subclass
# ─────────────────────────────────────────────────────────────────

class SonnetExtractor(BaseExtractor):
    """
    Pure JS table extractor — no LLM involved in data capture.
    The LLM model dropdown selection is accepted but ignored here;
    extraction is deterministic and costs $0.00.
    """

    async def extract(
        self,
        url: str,
        start_date: str,
        end_date: str,
        model: str,       # accepted for interface compatibility; not used
        job_id: str,
        manager,
    ):
        from playwright.async_api import async_playwright

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(
                    viewport={"width": 1366, "height": 768},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                page = await context.new_page()

                await manager.send_log(job_id, f"[DOM-Extract] Navigating to {url} …")
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(1.5)

                # ── Locate the first visible table ────────────────────
                table_selector = await self._find_table_selector(page)
                if not table_selector:
                    await manager.send_log(job_id, "ERROR: No results table found on page.")
                    await manager.send_result(job_id, [])
                    await browser.close()
                    return

                await manager.send_log(job_id, f"[DOM-Extract] Table found: {table_selector}")
                await manager.send_log(job_id, "[DOM-Extract] Extracting rows via JS …")

                # ── Run JS extractor ──────────────────────────────────
                raw_rows: List[dict] = await page.evaluate(_EXTRACT_JS, table_selector)

                await manager.send_log(
                    job_id,
                    f"[DOM-Extract] ✅ Extracted {len(raw_rows)} rows — $0.00 cost",
                )

                # Number each row title
                for idx, row in enumerate(raw_rows, 1):
                    row["title"] = f"{idx}. {row.get('title', 'Document')}"

                # ── Save results.json ─────────────────────────────────
                with open("results.json", "w", encoding="utf-8") as fh:
                    json.dump(
                        {"extracted_rows": len(raw_rows), "rows": raw_rows},
                        fh, indent=2, default=str,
                    )
                await manager.send_log(job_id, "[DOM-Extract] Saved to results.json")

                await manager.send_result(job_id, raw_rows)
                await browser.close()

        except Exception as exc:
            await manager.send_log(job_id, f"ERROR in DOM-Extract: {exc}")
            await manager.send_result(job_id, [])

    # ── Helpers ───────────────────────────────────────────────────

    async def _find_table_selector(self, page: Page) -> Optional[str]:
        """Return the CSS selector of the first visible table on the page."""
        candidates = [
            "#results-table",
            "#resultsTable",
            ".results-table",
            "table.data-table",
            "table.views-table",
            "table.listing",
            "table",   # last resort — first visible table
        ]
        for sel in candidates:
            try:
                if await page.locator(sel).first.is_visible(timeout=1500):
                    return sel
            except Exception:
                continue
        return None
