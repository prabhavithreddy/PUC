"""
ChunkingExtractor — High-Accuracy, Low-Cost Table Extraction
=============================================================
Strategy: Playwright DOM-level chunking (40 rows/batch) → markdownify →
LLM extraction (forced JSON) → aggregate & save results.json.

Model routing:
  • Claude models  → Anthropic SDK  with tool_choice="tool" (guaranteed JSON)
  • All others     → LiteLLM        with JSON schema in prompt + response_format
    (openrouter/*, groq/*, lmstudio/*, gemini/*, openai/*, etc.)

Drop-in BaseExtractor subclass; registered as engine="sonnet" in __init__.py.
"""

import os
import re
import json
import asyncio
from typing import List, Optional

from markdownify import markdownify as md
from playwright.async_api import Page
from pydantic import BaseModel, Field
from litellm import acompletion, completion_cost

from .base import BaseExtractor

# ─────────────────────────────────────────────────────────────────
# 1. Pydantic Schema  (shared by both routing paths)
# ─────────────────────────────────────────────────────────────────

class DocketRow(BaseModel):
    date: str = Field(description="Filing date (YYYY-MM-DD)")
    docket_number: str
    description: str
    document_url: Optional[str] = Field(
        None,
        description="Absolute URL to the document, or null if no link exists"
    )

class BatchResult(BaseModel):
    rows: List[DocketRow]


# ─────────────────────────────────────────────────────────────────
# 2. Pre-Processor — Playwright DOM chunking
# ─────────────────────────────────────────────────────────────────

CHUNK_SIZE = 40  # rows per LLM batch

async def chunk_and_clean_table(page: Page, table_selector: str) -> tuple[List[str], int]:
    """
    Clean the target table in the DOM, resolve all relative links to absolute,
    split body rows into CHUNK_SIZE batches, convert each to Markdown.

    Returns:
        (markdown_batches, total_row_count)
    """
    result = await page.evaluate(
        """
        ({ selector, chunkSize }) => {
            const table = document.querySelector(selector);
            if (!table) return [[], 0];

            // Clone to avoid mutating live DOM
            const clone = table.cloneNode(true);

            // Strip noise
            clone.querySelectorAll('script, style, svg').forEach(el => el.remove());
            clone.querySelectorAll('[style*="display:none"],[style*="display: none"],[hidden]')
                 .forEach(el => el.remove());

            // Resolve relative hrefs using the live DOM (a.href is always absolute)
            const origAnchors = Array.from(table.querySelectorAll('a[href]'));
            const cloneAnchors = Array.from(clone.querySelectorAll('a[href]'));
            origAnchors.forEach((a, i) => {
                if (cloneAnchors[i]) cloneAnchors[i].href = a.href;
            });

            // Header rows
            const headerRows = Array.from(clone.querySelectorAll('thead tr, tr:has(th)'));
            const headerHtml = headerRows.map(r => r.outerHTML).join('\\n');

            // Body rows only
            const bodyRows = Array.from(
                clone.querySelectorAll('tbody tr, tr:not(:has(th))')
            ).filter(r => r.querySelectorAll('td').length > 0);

            const chunks = [];
            for (let i = 0; i < bodyRows.length; i += chunkSize) {
                const slice = bodyRows.slice(i, i + chunkSize);
                const rowsHtml = slice.map(r => r.outerHTML).join('\\n');
                chunks.push(`<table><thead>${headerHtml}</thead><tbody>${rowsHtml}</tbody></table>`);
            }

            return [chunks, bodyRows.length];
        }
        """,
        {"selector": table_selector, "chunkSize": CHUNK_SIZE},
    )

    html_chunks: List[str] = result[0]
    total_rows: int = result[1]

    markdown_batches: List[str] = [
        md(chunk, convert_links=True, strip=["img"])
        for chunk in html_chunks
    ]
    return markdown_batches, total_rows


# ─────────────────────────────────────────────────────────────────
# 3. LLM Routing helpers
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a precise data extraction parser. You will be provided with a "
    "Markdown table of government dockets.\n\n"
    "CRITICAL RULES:\n"
    "1. You MUST extract EVERY SINGLE ROW provided in the markdown.\n"
    "2. DO NOT summarize.\n"
    "3. DO NOT use placeholders like '[...]' or 'omitted for brevity'.\n"
    "4. If there are 40 rows in the markdown, your JSON array MUST contain "
    "exactly 40 objects. Failure to extract every row will cause a critical "
    "system crash."
)

_TOOL_DEF = {
    "name": "save_table_data",
    "description": "Save the extracted docket rows.",
    "input_schema": BatchResult.model_json_schema(),
}
_TOOL_CHOICE = {"type": "tool", "name": "save_table_data"}

_JSON_SCHEMA_PROMPT = (
    "\n\nReturn ONLY a JSON object matching this exact schema — no markdown, "
    "no explanation, no extra keys:\n"
    + json.dumps(BatchResult.model_json_schema(), indent=2)
)


def _is_claude(model: str) -> bool:
    """Return True for any model string that targets an Anthropic Claude model."""
    m = model.lower()
    return "claude" in m and not m.startswith(("openrouter/", "lmstudio/"))


def _anthropic_model_id(model: str) -> str:
    """Strip any litellm prefix and return a bare Anthropic model id."""
    # e.g. "anthropic/claude-3-5-sonnet-20240620" → "claude-3-5-sonnet-20240620"
    if "/" in model:
        return model.split("/", 1)[1]
    return model


# ── Path A: Anthropic SDK (Claude models) ────────────────────────

async def _extract_batch_anthropic(
    markdown_table: str,
    model: str,
) -> tuple[BatchResult, float]:
    """Use Anthropic SDK with forced tool_choice for guaranteed JSON output."""
    import anthropic  # imported here so non-Claude users don't need the package

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.AsyncAnthropic(api_key=api_key)

    response = await client.messages.create(
        model=_anthropic_model_id(model),
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[_TOOL_DEF],
        tool_choice=_TOOL_CHOICE,
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract ALL rows from the following docket table:\n\n"
                    + markdown_table
                ),
            }
        ],
    )

    # Calculate cost from usage
    in_tok  = response.usage.input_tokens  if response.usage else 0
    out_tok = response.usage.output_tokens if response.usage else 0
    cost = (in_tok * 3.0 / 1_000_000) + (out_tok * 15.0 / 1_000_000)

    for block in response.content:
        if block.type == "tool_use" and block.name == "save_table_data":
            return BatchResult(**block.input), cost

    return BatchResult(rows=[]), cost


# ── Path B: LiteLLM (all other models) ───────────────────────────

def _build_litellm_kwargs(model: str) -> dict:
    """Return extra kwargs needed for LM Studio / OpenRouter quirks."""
    kwargs: dict = {}
    if model.startswith("lmstudio/"):
        kwargs["model"] = f"openai/{model.split('/', 1)[1]}"
        kwargs["api_base"] = "http://127.0.0.1:1234/v1"
        kwargs["api_key"] = "lm-studio"
    elif model.startswith("openrouter/"):
        pass  # LiteLLM handles openrouter natively; no response_format needed
    else:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


async def _extract_batch_litellm(
    markdown_table: str,
    model: str,
) -> tuple[BatchResult, float]:
    """Use LiteLLM acompletion with JSON schema in the prompt."""
    kwargs = _build_litellm_kwargs(model)
    effective_model = kwargs.pop("model", model)

    user_content = (
        "Extract ALL rows from the following docket table:\n\n"
        + markdown_table
        + _JSON_SCHEMA_PROMPT
    )

    for attempt in range(3):
        try:
            response = await acompletion(
                model=effective_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_content},
                ],
                temperature=0,
                **kwargs,
            )
            break
        except Exception as exc:
            if attempt == 2:
                raise
            is_rate = "rate_limit" in str(exc).lower() or "429" in str(exc)
            await asyncio.sleep(30 if is_rate else 2)

    # Calculate cost (best-effort)
    try:
        cost = completion_cost(completion_response=response)
    except Exception:
        cost = 0.0

    raw = response.choices[0].message.content or ""

    # Strip markdown code fences
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```")[0].strip()

    # Parse — try direct first, then find first JSON object/array
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"[\[{]", raw)
        if match:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(raw, match.start())
            except json.JSONDecodeError:
                pass

    if parsed is None:
        return BatchResult(rows=[]), cost

    # Accept {"rows": [...]} or a bare [...]
    if isinstance(parsed, list):
        parsed = {"rows": parsed}

    try:
        return BatchResult(**parsed), cost
    except Exception:
        return BatchResult(rows=[]), cost


# ── Unified dispatcher ────────────────────────────────────────────

async def extract_batch(
    markdown_table: str,
    model: str,
) -> tuple[BatchResult, float]:
    """Route to Anthropic SDK or LiteLLM based on the model string."""
    if _is_claude(model):
        return await _extract_batch_anthropic(markdown_table, model)
    else:
        return await _extract_batch_litellm(markdown_table, model)


# ─────────────────────────────────────────────────────────────────
# 4. ChunkingExtractor — BaseExtractor subclass
# ─────────────────────────────────────────────────────────────────

class SonnetExtractor(BaseExtractor):
    """
    High-accuracy extractor using DOM-level chunking + any LLM.

    Claude models  → Anthropic SDK tool-calling (forced, zero-hallucination JSON).
    All others     → LiteLLM + JSON schema prompt (OpenRouter, Groq, Gemini, etc.)
    """

    async def extract(
        self,
        url: str,
        start_date: str,
        end_date: str,
        model: str,
        job_id: str,
        manager,
    ):
        # Guard: bare claude-* strings use the Anthropic SDK and need ANTHROPIC_API_KEY.
        # openrouter/anthropic/claude-* models go through LiteLLM → OPENROUTER_API_KEY only.
        if _is_claude(model) and not os.environ.get("ANTHROPIC_API_KEY", ""):
            await manager.send_log(
                job_id,
                "ERROR: ANTHROPIC_API_KEY is not set. "
                "Either set it, or pick a Claude model via OpenRouter "
                "(e.g. openrouter/anthropic/claude-sonnet-4-5) which only needs OPENROUTER_API_KEY."
            )
            await manager.send_result(job_id, [])
            return

        from playwright.async_api import async_playwright

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

                await manager.send_log(job_id, f"[DOM-Chunking] Navigating to {url} …")
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(1.5)

                # ── Find the results table ────────────────────────────
                table_selector = await self._find_table_selector(page)
                if not table_selector:
                    await manager.send_log(job_id, "ERROR: No results table found on page.")
                    await manager.send_result(job_id, [])
                    await browser.close()
                    return

                await manager.send_log(job_id, f"[DOM-Chunking] Table found: {table_selector}")

                # ── Chunk & markdownify ───────────────────────────────
                await manager.send_log(job_id, "[DOM-Chunking] Chunking table rows …")
                markdown_batches, total_dom_rows = await chunk_and_clean_table(
                    page, table_selector
                )
                await manager.send_log(
                    job_id,
                    f"[DOM-Chunking] {total_dom_rows} DOM rows → "
                    f"{len(markdown_batches)} batch(es) of ≤{CHUNK_SIZE} | model: {model}",
                )

                # ── Send batches to LLM sequentially ─────────────────
                all_dockets: List[DocketRow] = []
                for i, batch_md in enumerate(markdown_batches, 1):
                    if manager.is_cancelled(job_id):
                        await manager.send_log(job_id, "ERROR: Job was cancelled.")
                        break

                    await manager.send_log(
                        job_id,
                        f"[DOM-Chunking] Batch {i}/{len(markdown_batches)} → {model} …",
                    )
                    try:
                        result, batch_cost = await extract_batch(batch_md, model)
                        total_cost += batch_cost
                        await manager.send_cost(job_id, total_cost)
                        all_dockets.extend(result.rows)
                        await manager.send_log(
                            job_id,
                            f"[DOM-Chunking] Batch {i}: {len(result.rows)} rows extracted "
                            f"(${batch_cost:.6f}) | total: {len(all_dockets)}",
                        )
                    except Exception as exc:
                        await manager.send_log(
                            job_id, f"[DOM-Chunking] Batch {i} ERROR: {exc}"
                        )

                # ── Validation ────────────────────────────────────────
                await manager.send_log(
                    job_id,
                    f"[DOM-Chunking] ✅ Expected rows: {total_dom_rows} | "
                    f"Extracted rows: {len(all_dockets)} | "
                    f"Total cost: ${total_cost:.6f}",
                )

                # ── Map to app document schema ────────────────────────
                final_docs = [
                    {
                        "title": f"{idx}. {row.description}",
                        "date": row.date,
                        "url": str(row.document_url) if row.document_url else "",
                        "metadata": {"Docket #": row.docket_number},
                    }
                    for idx, row in enumerate(all_dockets, 1)
                ]

                # ── Persist results.json ──────────────────────────────
                with open("results.json", "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "model": model,
                            "expected_rows": total_dom_rows,
                            "extracted_rows": len(all_dockets),
                            "total_cost_usd": total_cost,
                            "dockets": [r.model_dump() for r in all_dockets],
                        },
                        fh,
                        indent=2,
                        default=str,
                    )
                await manager.send_log(job_id, "[DOM-Chunking] Results saved to results.json")

                await manager.send_result(job_id, final_docs)
                await browser.close()

        except Exception as exc:
            await manager.send_log(job_id, f"ERROR in ChunkingExtractor: {exc}")
            await manager.send_result(job_id, [])

    # ── Helpers ───────────────────────────────────────────────────

    async def _find_table_selector(self, page: Page) -> Optional[str]:
        """Try ranked selectors; return the first visible one."""
        candidates = [
            "#results-table",
            "#resultsTable",
            ".results-table",
            "table.data-table",
            "table.views-table",
            "table.listing",
            "table",   # last resort
        ]
        for sel in candidates:
            try:
                if await page.locator(sel).first.is_visible(timeout=1500):
                    return sel
            except Exception:
                continue
        return None
