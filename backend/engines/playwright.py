import os
import json
import random
import asyncio
import urllib.parse
from playwright.async_api import async_playwright
from .base import BaseExtractor, call_llm, get_interactive_elements

# Human-like helpers
async def human_delay(min_ms=300, max_ms=900):
    """Pause for a random duration like a human would."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)

async def human_type(element, text: str):
    """Type text character by character with variable speed like a human."""
    for char in text:
        await element.type(char, delay=random.uniform(40, 140))
        # Occasionally pause mid-word as if thinking
        if random.random() < 0.05:
            await human_delay(200, 600)

async def human_click(page, element):
    """Scroll into view, hover, pause, then click — like a real user."""
    await element.scroll_into_view_if_needed(timeout=5000)
    await element.hover()
    await human_delay(80, 250)  # Pause between hover and click
    await element.click()

class PlaywrightExtractor(BaseExtractor):
    async def extract(self, url, start_date, end_date, model, job_id, manager):
        total_cost = 0.0
        try:
            async with async_playwright() as p:
                # Launch with a realistic viewport and user agent
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(
                    viewport={"width": 1366, "height": 768},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    timezone_id="America/New_York"
                )
                page = await context.new_page()

                await manager.send_log(job_id, f"Navigating to {url} ...")
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await human_delay(500, 1200)  # Simulate page read time

                max_steps = 60
                action_history = []
                acted_selectors = set()
                agent_downloads = []
                all_collected_docs = []  # accumulates across pages

                for step in range(max_steps):
                    if manager.is_cancelled(job_id):
                        await manager.send_log(job_id, "ERROR: Job was cancelled.")
                        break

                    await manager.send_log(job_id, f"--- Step {step+1}/{max_steps} ---")
                    await page.wait_for_timeout(2000)

                    page_text = await self._get_page_content(page)
                    elements = await get_interactive_elements(page)
                    filtered_elements = [el for el in elements if el.get('selector') not in acted_selectors]

                    prompt = self._get_agent_prompt(page_text, filtered_elements, action_history, start_date, end_date)
                    action_plan, total_cost = await call_llm(model, prompt, manager, job_id, total_cost)

                    if not action_plan: break
                    # Guard: LLM may return a bare list instead of an action dict
                    if isinstance(action_plan, list):
                        action_plan = {"action": "extract", "documents": action_plan}
                    action = action_plan.get("action")
                    action_history.append(action_plan)
                    await manager.send_log(job_id, f"Agent decided to: {action}")

                    if action in ("extract_table", "extract"):
                        if action == "extract_table":
                            # --- Deterministic JS table extraction (no LLM limits) ---
                            await manager.send_log(job_id, "Running deterministic table extraction...")
                            scraped = await self._extract_table_from_page(page)
                            if not scraped and not all_collected_docs and not agent_downloads:
                                await manager.send_log(job_id, "No table data found yet. Waiting...")
                                await page.wait_for_timeout(2000)
                                continue
                            if scraped:
                                all_collected_docs.extend(scraped)
                                await manager.send_log(job_id, f"Table scraper captured {len(scraped)} rows. Total: {len(all_collected_docs)}")
                        else:
                            # Fallback: use LLM-provided documents (filter placeholders)
                            docs = action_plan.get("documents", [])
                            real_docs = [
                                d for d in docs
                                if d.get("title") and d.get("title") not in ("Document", "", None)
                                and d.get("date") and d.get("date") not in ("-", "", None)
                            ]
                            if not real_docs and not all_collected_docs and not agent_downloads:
                                await manager.send_log(job_id, f"Agent extracted {len(docs)} placeholder doc(s). Waiting...")
                                await page.wait_for_timeout(2000)
                                continue
                            if real_docs:
                                all_collected_docs.extend(real_docs)
                                await manager.send_log(job_id, f"Collected {len(real_docs)} docs. Total: {len(all_collected_docs)}")

                        # --- Pagination: look for a visible next-page control ---
                        next_selectors = [
                            "a[aria-label='Next page']", "a[aria-label='next']",
                            "button[aria-label='Next page']", "li.next > a",
                            "a.next", ".pagination .next",
                            "a:text('Next')", "button:text('Next')",
                            "[class*='next']:not([disabled])",
                        ]
                        next_el = None
                        for sel in next_selectors:
                            try:
                                el = page.locator(sel).first
                                if await el.is_visible(timeout=800):
                                    next_el = el
                                    break
                            except: pass

                        if next_el:
                            await manager.send_log(job_id, "Found next page — collecting remaining documents...")
                            await human_click(page, next_el)
                            await page.wait_for_load_state("networkidle", timeout=15000)
                            acted_selectors = set()
                            continue

                        # No more pages — wrap up
                        await self._download_docs(all_collected_docs, page, job_id, manager)
                        final_docs = all_collected_docs + agent_downloads
                        for i, d in enumerate(final_docs):
                            if not d.get("title", "").startswith(f"{i+1}. "):
                                d["title"] = f"{i+1}. {d.get('title', 'Document')}"

                        await manager.send_log(job_id, f"Extraction finished! Found {len(final_docs)} documents across all pages.")
                        await manager.send_result(job_id, final_docs)
                        await browser.close()
                        return

                    elif action == "download":
                        download_doc = await self._handle_download(action_plan, page, job_id, manager)
                        if download_doc:
                            agent_downloads.append(download_doc)
                            await manager.send_log(job_id, f"Manual download successful: {download_doc['title']}")
                    elif action in ["fill", "select", "click"]:
                        await self._handle_action(action, action_plan, page, manager, acted_selectors, job_id)
                        # Human-like wait after any interaction
                        await human_delay(800, 2000)
                    else:
                        await manager.send_log(job_id, f"Unknown action: {action}. Skipping...")
                        continue

                await manager.send_log(job_id, "Max steps reached.")
                await manager.send_result(job_id, agent_downloads)
                await browser.close()
        except Exception as e:
            await manager.send_log(job_id, f"ERROR in Playwright: {str(e)}")
            await manager.send_result(job_id, [])

    async def _extract_table_from_page(self, page):
        """Deterministically extract ALL rows from every visible results table using JS.
        Returns a list of document dicts with title, date, url, metadata."""
        return await page.evaluate(r'''
        () => {
            const results = [];
            const tables = document.querySelectorAll('table');
            tables.forEach(table => {
                const headers = Array.from(
                    table.querySelectorAll('thead th, thead td')
                ).map(h => h.innerText.trim());

                const rows = table.querySelectorAll('tbody tr, tr');
                rows.forEach(row => {
                    const cells = row.querySelectorAll('td, th');
                    if (cells.length === 0) return;

                    const cellData = Array.from(cells).map((td, i) => ({
                        header: headers[i] || ('col_' + i),
                        text: td.innerText.trim().replace(/\n/g, ' ').replace(/\s+/g, ' '),
                        urls: Array.from(td.querySelectorAll('a[href]')).map(a => a.href)
                    }));

                    // Skip header-only rows
                    if (cellData.every(c => !c.text || c.text === c.header)) return;

                    // Most descriptive cell = title
                    const titleCell = cellData.reduce((best, c) =>
                        c.text.length > best.text.length ? c : best, cellData[0]);

                    // Find a date-like cell by header name or date pattern
                    const dateCell = cellData.find(c =>
                        /date|filed|received|submitted/i.test(c.header) ||
                        /\d{1,2}\/\d{1,2}\/\d{2,4}/.test(c.text)
                    );

                    const allUrls = cellData.flatMap(c => c.urls);

                    const metadata = {};
                    cellData.forEach(c => {
                        if (c.text && c.text !== titleCell.text) {
                            metadata[c.header] = c.text;
                        }
                    });

                    results.push({
                        title: titleCell.text || 'Document',
                        date: dateCell ? dateCell.text : '',
                        url: allUrls[0] || '',
                        metadata: metadata
                    });
                });
            });
            return results;
        }
        ''')

    async def _get_page_content(self, page):
        return await page.evaluate('''() => {
            // Read page text while skipping noisy tags — does NOT mutate live DOM
            const skipTags = new Set(['SCRIPT', 'STYLE', 'NAV', 'FOOTER', 'HEADER']);
            function getText(node) {
                if (node.tagName && skipTags.has(node.tagName)) return '';
                if (node.nodeType === Node.TEXT_NODE) return node.textContent;
                return Array.from(node.childNodes).map(getText).join(' ');
            }
            let text = getText(document.body).replace(/\\s+/g, ' ').trim();

            // Extract tables with ALL links per cell for maximum metadata
            let tableText = Array.from(document.querySelectorAll('table')).map((table, ti) => {
                const headers = Array.from(table.querySelectorAll('thead th, thead td')).map(h => h.innerText.trim());
                const headerRow = headers.length ? 'HEADERS: ' + headers.join(' | ') : '';

                const rows = Array.from(table.querySelectorAll('tbody tr, tr')).map(tr => {
                    return Array.from(tr.querySelectorAll('th, td')).map((td, ci) => {
                        let content = td.innerText.trim().replace(/\\n/g, ' ');
                        // Capture ALL links in this cell
                        const links = Array.from(td.querySelectorAll('a[href]'));
                        links.forEach(a => { content += ` [URL: ${a.href}]`; });
                        const header = headers[ci] ? `[${headers[ci]}] ` : '';
                        return header + content;
                    }).join(' | ');
                }).join('\\n');

                return (headerRow ? headerRow + '\\n' : '') + rows;
            }).join('\\n\\n');

            return "--- PAGE TEXT ---\\n" + text.substring(0, 2000) + "\\n\\n--- FULL RESULTS TABLE (extract ALL rows, do not truncate) ---\\n" + tableText;
        }''')

    def _get_agent_prompt(self, page_text, elements, history, start, end):
        history_str = json.dumps(history[-5:], indent=2)
        return f"""
        You are an autonomous web scraper agent. Your task is to extract docket documents strictly matching the date range: {start} to {end}.

        Current Page Content:
        {page_text}

        Interactive Elements (top 50):
        {json.dumps(elements[:50])}

        Previous Actions:
        {history_str}

        CRITICAL DATE INSTRUCTION:
        When filling date fields, look at the input's placeholder for the required format (e.g., MM/DD/YYYY).
        Default to MM/DD/YYYY if no format is shown. Never use relative dates.

        CRITICAL EXTRACTION INSTRUCTION:
        1. After filling a search form, you MUST click "Search" / "Submit" / "Find" before extracting.
        2. Only use action="extract" when a results table is visible in the page content.
        3. For EVERY row in the results table (do not stop early, capture ALL rows):
           - "title": the most descriptive text column (Description, Subject, Name)
           - "date": the date column value
           - "url": the FULL URL from any [URL: ...] marker in that row — use the EXACT URL string
           - "metadata": ALL remaining columns as key-value pairs (Docket #, Type, Status, Submitter, etc.)
        4. Do NOT skip rows. Do NOT truncate. Capture EVERY single row in the table.
        5. If a row has no URL marker, set "url" to "".

        IMPORTANT: Return exactly ONE action per response. Do not combine multiple actions.
        Output ONLY a single JSON object (no explanation, no markdown, no extra text):
        - {{ "action": "click", "selector": "..." }}
        - {{ "action": "fill", "selector": "...", "value": "..." }}
        - {{ "action": "select", "selector": "...", "value": "..." }}
        - {{ "action": "extract_table" }}   <-- use this when results are visible; the system will scrape the table directly
        """

    async def _download_docs(self, docs, page, job_id, manager):
        os.makedirs("downloads", exist_ok=True)
        for i, d in enumerate(docs):
            d["title"] = f"{i+1}. {d.get('title', 'Document')}"
            doc_url = d.get("url", "")
            if not doc_url:
                continue  # No URL — just keep metadata, nothing to download

            await manager.send_log(job_id, f"Attempting to download: {d['title']}")
            downloaded = False

            # Strategy 1: Find the matching <a> link on the current page and click it
            # This preserves the browser session (cookies, tokens) needed for the download
            try:
                url_tail = doc_url.split("/")[-1].split("?")[0]
                candidates = [
                    f'a[href*="{url_tail}"]',
                    f'a[href="{doc_url}"]',
                ]
                link_el = None
                for sel in candidates:
                    try:
                        el = page.locator(sel).first
                        if await el.is_visible(timeout=800):
                            link_el = el
                            break
                    except: pass

                if link_el:
                    async with page.expect_download(timeout=20000) as dl_info:
                        await human_click(page, link_el)
                    download = await dl_info.value
                    fname = download.suggested_filename or f"doc_{i+1}"
                    suffix = os.path.splitext(fname)[1] or ".pdf"
                    filepath = f"downloads/{job_id}_doc_{i+1}{suffix}"
                    await download.save_as(filepath)

                    # Validate: reject HTML masquerading as a document
                    with open(filepath, 'rb') as f:
                        header = f.read(512)
                    if b'<!DOCTYPE' in header or b'<html' in header.lower():
                        os.remove(filepath)
                        await manager.send_log(job_id, f"Link returned HTML (session-gated). URL kept for manual access: {d['title']}")
                    else:
                        d["local_url"] = f"/{filepath}"
                        await manager.send_log(job_id, f"Downloaded: {fname}")
                        downloaded = True
                    await human_delay(600, 1200)

            except Exception as e:
                await manager.send_log(job_id, f"Click download failed for {d['title']}: {type(e).__name__}")

            if not downloaded:
                # No file saved — URL is still accessible, user can open it from the UI
                await manager.send_log(job_id, f"URL recorded for manual access: {doc_url[:80]}")

    async def _handle_download(self, plan, page, job_id, manager):
        selector = plan.get("selector")
        try:
            async with page.expect_download(timeout=15000) as di:
                el = page.locator(selector).first
                await human_click(page, el)
            download = await di.value
            filename = f"downloads/{job_id}_{download.suggested_filename}"
            await download.save_as(filename)
            return {
                "title": download.suggested_filename, "url": "", "local_url": f"/{filename}",
                "date": "Downloaded", "metadata": {"Action": "Agent Download"}
            }
        except Exception: return None

    async def _handle_action(self, action, plan, page, manager, acted_selectors, job_id):
        selector = plan.get("selector")
        if action in ["fill", "select"]: acted_selectors.add(selector)
        try:
            el = page.locator(selector).first
            if action == "click":
                await human_click(page, el)
            elif action == "fill":
                await el.scroll_into_view_if_needed(timeout=5000)
                await el.hover()
                await human_delay(80, 200)
                await el.click()                  # Focus the field
                await human_delay(100, 300)
                await el.select_text()            # Select existing content
                await el.press("Backspace")       # Clear it
                await human_type(el, plan.get("value", ""))  # Type like a human
            elif action == "select":
                await el.scroll_into_view_if_needed(timeout=5000)
                await human_delay(100, 300)
                await page.select_option(selector, plan.get("value"), timeout=5000)
        except Exception as e:
            await manager.send_log(job_id, f"Action failed ({action} on '{selector}'): {e}")
