import json
import random
import asyncio
from playwright.async_api import async_playwright
from .base import BaseExtractor, call_llm, get_interactive_elements

# ---------------------------------------------------------------------------
# Human-like interaction helpers
# ---------------------------------------------------------------------------

async def human_delay(min_ms=300, max_ms=900):
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)

async def human_type(element, text: str):
    for char in text:
        await element.type(char, delay=random.uniform(40, 140))
        if random.random() < 0.05:
            await human_delay(200, 600)

async def human_click(page, element):
    await element.scroll_into_view_if_needed(timeout=5000)
    await element.hover()
    await human_delay(80, 250)
    await element.click()


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

class PlaywrightExtractor(BaseExtractor):
    async def extract(self, url, start_date, end_date, model, job_id, manager):
        total_cost = 0.0
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

                await manager.send_log(job_id, f"Navigating to {url} ...")
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await human_delay(500, 1200)

                max_steps = 60
                action_history = []
                acted_selectors = set()
                all_collected_docs = []  # accumulates across pages

                for step in range(max_steps):
                    if manager.is_cancelled(job_id):
                        await manager.send_log(job_id, "Job was cancelled.")
                        break

                    await manager.send_log(job_id, f"--- Step {step+1}/{max_steps} ---")
                    await page.wait_for_timeout(1000)

                    # Read page content (with retry on transient JS errors)
                    try:
                        page_text = await self._get_page_content(page)
                    except Exception as pe:
                        await manager.send_log(job_id, f"Page read warning: {pe}. Retrying...")
                        await page.wait_for_timeout(1500)
                        try:
                            page_text = await self._get_page_content(page)
                        except Exception:
                            page_text = "(page unreadable)"

                    elements = await get_interactive_elements(page)
                    filtered_elements = [
                        el for el in elements if el.get("selector") not in acted_selectors
                    ]

                    prompt = self._get_agent_prompt(
                        page_text, filtered_elements, action_history, start_date, end_date
                    )
                    action_plan, total_cost = await call_llm(
                        model, prompt, manager, job_id, total_cost
                    )

                    if not action_plan:
                        break

                    # Guard: some LLMs return a bare list instead of an action dict
                    if isinstance(action_plan, list):
                        action_plan = {"action": "extract_table"}

                    action = action_plan.get("action")
                    action_history.append(action_plan)
                    await manager.send_log(job_id, f"Agent decided to: {action}")

                    # ----------------------------------------------------------
                    # TABLE EXTRACTION (deterministic JS — no LLM data capture)
                    # ----------------------------------------------------------
                    if action in ("extract_table", "extract"):
                        await manager.send_log(job_id, "Running deterministic table extraction...")
                        scraped = await self._extract_table_from_page(page)

                        if not scraped and not all_collected_docs:
                            await manager.send_log(job_id, "No table data found yet. Waiting...")
                            await page.wait_for_timeout(2000)
                            continue

                        if scraped:
                            all_collected_docs.extend(scraped)
                            await manager.send_log(
                                job_id,
                                f"Table scraper captured {len(scraped)} rows. "
                                f"Total: {len(all_collected_docs)}",
                            )

                        # Check for a next-page button before finishing
                        next_el = await self._find_next_page(page)
                        if next_el:
                            await manager.send_log(job_id, "Found next page — collecting more...")
                            await human_click(page, next_el)
                            await page.wait_for_load_state("networkidle", timeout=15000)
                            acted_selectors = set()
                            continue

                        # No more pages — enrich with click-based URLs then return
                        await self._enrich_with_click_urls(page, all_collected_docs, job_id, manager)
                        final_docs = [
                            {**d, "title": f"{i+1}. {d.get('title', 'Document')}"}
                            for i, d in enumerate(all_collected_docs)
                        ]
                        await manager.send_log(
                            job_id,
                            f"Extraction finished! Found {len(final_docs)} documents.",
                        )
                        await manager.send_result(job_id, final_docs)
                        await browser.close()
                        return

                    # ----------------------------------------------------------
                    # NAVIGATION ACTIONS
                    # ----------------------------------------------------------
                    elif action in ("fill", "select", "click"):
                        await self._handle_action(
                            action, action_plan, page, manager, acted_selectors, job_id
                        )
                        await human_delay(800, 2000)
                    else:
                        await manager.send_log(job_id, f"Unknown action '{action}'. Skipping.")

                # Loop exhausted
                await manager.send_log(job_id, "Max steps reached.")
                final_docs = [
                    {**d, "title": f"{i+1}. {d.get('title', 'Document')}"}
                    for i, d in enumerate(all_collected_docs)
                ]
                await manager.send_result(job_id, final_docs)
                await browser.close()

        except Exception as e:
            await manager.send_log(job_id, f"ERROR in Playwright: {e}")
            await manager.send_result(job_id, [])

    # ---------------------------------------------------------------------------
    # Click-based URL capture (for JS-gated document links like OnBase)
    # ---------------------------------------------------------------------------

    async def _enrich_with_click_urls(self, page, docs, job_id, manager):
        """Click each table row that has no URL and capture the popup/navigation URL.
        OnBase opens documents in a new browser window on row click."""
        missing = [d for d in docs if not d.get("url")]
        if not missing:
            return

        await manager.send_log(job_id, f"Capturing click-based URLs for {len(missing)} rows...")

        # Get all body rows from the first table on the page
        row_handles = await page.query_selector_all("table tbody tr")
        doc_idx = 0  # index into `docs` (skip docs that already have a URL)

        for row_el in row_handles:
            # Find the next doc that still needs a URL
            while doc_idx < len(docs) and docs[doc_idx].get("url"):
                doc_idx += 1
            if doc_idx >= len(docs):
                break

            doc = docs[doc_idx]
            captured = False

            # --- Strategy 1: popup window (most common for OnBase) ---
            try:
                async with page.expect_popup(timeout=4000) as popup_info:
                    await row_el.scroll_into_view_if_needed()
                    await row_el.click()
                popup = await popup_info.value
                await popup.wait_for_load_state("domcontentloaded", timeout=8000)
                doc["url"] = popup.url
                await popup.close()
                await manager.send_log(job_id, f"  Got popup URL for row {doc_idx+1}: {doc['url'][:80]}")
                captured = True
            except Exception:
                pass

            # --- Strategy 2: same-tab navigation ---
            if not captured:
                try:
                    origin = page.url
                    await row_el.scroll_into_view_if_needed()
                    await row_el.click()
                    await page.wait_for_url(lambda u: u != origin, timeout=3000)
                    doc["url"] = page.url
                    await page.go_back(wait_until="networkidle", timeout=10000)
                    await manager.send_log(job_id, f"  Got nav URL for row {doc_idx+1}: {doc['url'][:80]}")
                    captured = True
                except Exception:
                    pass

            if not captured:
                await manager.send_log(job_id, f"  No URL captured for row {doc_idx+1} — link may require auth")

            doc_idx += 1
            await human_delay(300, 700)  # Polite pause between clicks

    # ---------------------------------------------------------------------------
    # Deterministic JS table scraper
    # ---------------------------------------------------------------------------

    async def _extract_table_from_page(self, page):
        """Read ALL document rows from every visible table using JS.
        Filters out pagination/calendar rows. Captures JS-based links too."""
        return await page.evaluate(r'''
        () => {
            // Day-of-week headers found in calendar pagination widgets
            const DAY_HEADERS = new Set(['Su','Mo','Tu','We','Th','Fr','Sa',
                                         'Sun','Mon','Tue','Wed','Thu','Fri','Sat']);

            const results = [];

            document.querySelectorAll('table').forEach(table => {
                const headers = Array.from(
                    table.querySelectorAll('thead th, thead td')
                ).map(h => h.innerText.trim());

                // Skip calendar/pagination tables: if most headers are day names, ignore the table
                const dayHeaderCount = headers.filter(h => DAY_HEADERS.has(h)).length;
                if (headers.length > 0 && dayHeaderCount / headers.length > 0.4) return;

                table.querySelectorAll('tbody tr, tr').forEach(row => {
                    const cells = row.querySelectorAll('td, th');
                    if (!cells.length) return;

                    const cellData = Array.from(cells).map((td, i) => {
                        const text = td.innerText.trim()
                            .replace(/\n/g, ' ')
                            .replace(/\s+/g, ' ');

                        // Capture real href links (non-javascript, non-empty)
                        const hrefs = Array.from(td.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h && !h.startsWith('javascript:') && h !== '#');

                        // Also capture onclick for JS-driven document links
                        const clickAttrs = Array.from(td.querySelectorAll('[onclick]'))
                            .map(el => el.getAttribute('onclick'))
                            .filter(Boolean);

                        // Capture any data-id, data-doc, data-url attributes
                        const dataAttrs = {};
                        Array.from(td.querySelectorAll('[data-id],[data-doc],[data-url],[data-docid]'))
                            .forEach(el => {
                                ['data-id','data-doc','data-url','data-docid'].forEach(attr => {
                                    if (el.getAttribute(attr)) dataAttrs[attr] = el.getAttribute(attr);
                                });
                            });

                        return {
                            header: headers[i] || ('col_' + i),
                            text,
                            hrefs,
                            clickAttrs,
                            dataAttrs
                        };
                    });

                    // --- Filters: skip non-document rows ---

                    // Skip pure header rows
                    if (cellData.every(c => !c.text || c.text === c.header)) return;

                    // Skip calendar/pagination rows: title cell is a short number or "View"
                    const firstText = cellData[0]?.text || '';
                    if (/^\d{1,2}$/.test(firstText) || firstText.toLowerCase() === 'view') return;

                    // Skip rows where most cells are single digits (calendar date rows)
                    const singleDigitCells = cellData.filter(c => /^\d{1,2}$/.test(c.text));
                    if (singleDigitCells.length >= cellData.length * 0.5) return;

                    // Skip rows whose headers are day-of-week names
                    const rowDayHeaders = cellData.filter(c => DAY_HEADERS.has(c.header));
                    if (rowDayHeaders.length >= cellData.length * 0.4) return;

                    // --- Build document record ---

                    // Longest text cell = title (prefer cells with real document-type words)
                    const titleCell = cellData.reduce(
                        (best, c) => c.text.length > best.text.length ? c : best,
                        cellData[0]
                    );

                    // Date cell: by header name or date pattern
                    const dateCell = cellData.find(c =>
                        /^date$/i.test(c.header) ||
                        /filed|received|submitted/i.test(c.header) ||
                        /\d{1,2}\/\d{1,2}\/\d{2,4}/.test(c.text)
                    );

                    // Best URL: prefer real href, fall back to onclick string
                    const allHrefs = cellData.flatMap(c => c.hrefs);
                    const allOnclicks = cellData.flatMap(c => c.clickAttrs);
                    const allDataAttrs = Object.assign({}, ...cellData.map(c => c.dataAttrs));
                    const bestUrl = allHrefs[0] || '';

                    // Build metadata from all cells
                    const metadata = {};
                    cellData.forEach(c => {
                        if (c.text && c.text !== titleCell.text) {
                            metadata[c.header] = c.text;
                        }
                    });
                    if (allOnclicks.length) metadata['_onclick'] = allOnclicks[0];
                    if (Object.keys(allDataAttrs).length) metadata['_data'] = JSON.stringify(allDataAttrs);

                    results.push({
                        title: titleCell.text || 'Document',
                        date:  dateCell ? dateCell.text : '',
                        url:   bestUrl,
                        metadata
                    });
                });
            });
            return results;
        }
        ''')

    # ---------------------------------------------------------------------------
    # Pagination helper
    # ---------------------------------------------------------------------------

    async def _find_next_page(self, page):
        selectors = [
            "a[aria-label='Next page']", "a[aria-label='next']",
            "button[aria-label='Next page']", "li.next > a",
            "a.next", ".pagination .next",
            "a:text('Next')", "button:text('Next')",
            "[class*='next']:not([disabled])",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=800):
                    return el
            except Exception:
                pass
        return None

    # ---------------------------------------------------------------------------
    # Page content reader (for LLM context — does NOT mutate the live DOM)
    # ---------------------------------------------------------------------------

    async def _get_page_content(self, page):
        return await page.evaluate('''() => {
            const skipTags = new Set(["SCRIPT","STYLE","NAV","FOOTER","HEADER"]);
            function getText(node) {
                if (node.tagName && skipTags.has(node.tagName)) return "";
                if (node.nodeType === Node.TEXT_NODE) return node.textContent;
                return Array.from(node.childNodes).map(getText).join(" ");
            }
            const text = getText(document.body).replace(/\\s+/g, " ").trim();

            const tableText = Array.from(document.querySelectorAll("table")).map(table => {
                const headers = Array.from(
                    table.querySelectorAll("thead th, thead td")
                ).map(h => h.innerText.trim());
                const headerRow = headers.length ? "HEADERS: " + headers.join(" | ") : "";

                const rows = Array.from(table.querySelectorAll("tbody tr, tr")).map(tr =>
                    Array.from(tr.querySelectorAll("th, td")).map((td, ci) => {
                        let content = td.innerText.trim().replace(/\\n/g, " ");
                        Array.from(td.querySelectorAll("a[href]")).forEach(
                            a => { content += " [URL: " + a.href + "]"; }
                        );
                        return (headers[ci] ? "[" + headers[ci] + "] " : "") + content;
                    }).join(" | ")
                ).join("\\n");

                return (headerRow ? headerRow + "\\n" : "") + rows;
            }).join("\\n\\n");

            return "--- PAGE TEXT ---\\n" + text.substring(0, 2000) +
                   "\\n\\n--- RESULTS TABLE ---\\n" + tableText;
        }''')

    # ---------------------------------------------------------------------------
    # Agent prompt
    # ---------------------------------------------------------------------------

    def _get_agent_prompt(self, page_text, elements, history, start, end):
        return f"""
You are an autonomous web scraper. Navigate the page to find docket documents
filed between {start} and {end}, then trigger table extraction.

Current Page:
{page_text}

Interactive Elements (top 50):
{json.dumps(elements[:50])}

Recent Actions:
{json.dumps(history[-5:], indent=2)}

DATE FORMAT: Check each date field's placeholder attribute and use that exact format.
Default to MM/DD/YYYY. Never use relative dates.

RULES:
1. Fill ALL required search fields before clicking Search/Submit.
2. After clicking Search, wait for results to appear before extracting.
3. Use extract_table ONLY when a results table is visible on screen.

IMPORTANT: Return exactly ONE JSON action. No markdown, no explanation.
Options:
  {{"action":"fill","selector":"...","value":"..."}}
  {{"action":"click","selector":"..."}}
  {{"action":"select","selector":"...","value":"..."}}
  {{"action":"extract_table"}}
"""

    # ---------------------------------------------------------------------------
    # Navigation action handler
    # ---------------------------------------------------------------------------

    async def _handle_action(self, action, plan, page, manager, acted_selectors, job_id):
        selector = plan.get("selector")
        if action in ("fill", "select"):
            acted_selectors.add(selector)
        try:
            el = page.locator(selector).first
            if action == "click":
                await human_click(page, el)
            elif action == "fill":
                await el.scroll_into_view_if_needed(timeout=5000)
                await el.hover()
                await human_delay(80, 200)
                await el.click()
                await human_delay(100, 300)
                await el.select_text()
                await el.press("Backspace")
                await human_type(el, plan.get("value", ""))
            elif action == "select":
                await el.scroll_into_view_if_needed(timeout=5000)
                await human_delay(100, 300)
                await page.select_option(selector, plan.get("value"), timeout=5000)
        except Exception as e:
            await manager.send_log(job_id, f"Action failed ({action} → '{selector}'): {e}")
