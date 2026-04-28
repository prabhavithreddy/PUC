import asyncio
import os
import json
from abc import ABC, abstractmethod
from litellm import completion, completion_cost

# --- Common Utilities ---

async def get_interactive_elements(page):
    try:
        return await page.evaluate('''() => {
            const elements = Array.from(document.querySelectorAll('input, button, select, a'));
            return elements.map(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return null;
                const obj = {
                    tag: el.tagName,
                    selector: el.id ? `#${el.id}` : (el.tagName.toLowerCase() + (el.className ? `.${el.className.split(' ').join('.')}` : ''))
                };
                if (el.innerText || el.value || el.placeholder) obj.text = (el.innerText || el.value || el.placeholder).trim().substring(0, 100);
                if (el.placeholder) obj.placeholder = el.placeholder;
                if (el.type) obj.type = el.type;
                if (el.name) obj.name = el.name;
                if (el.href) obj.href = el.href;
                return obj;
            }).filter(e => e !== null);
        }''')
    except Exception:
        return []  # Return empty list if page evaluate times out

async def call_llm(model: str, prompt: str, manager, job_id, total_cost):
    await manager.send_log(job_id, f"Consulting LLM ({model})...")
    response = None
    for attempt in range(3):
        try:
            kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0
            }
            # OpenRouter and LM Studio don't reliably support response_format
            if model.startswith("lmstudio/"):
                kwargs["model"] = f"openai/{model.split('/', 1)[1]}"
                kwargs["api_base"] = "http://127.0.0.1:1234/v1"
                kwargs["api_key"] = "lm-studio"
                del kwargs["response_format"]
            elif model.startswith("openrouter/"):
                del kwargs["response_format"]

            response = await asyncio.to_thread(completion, **kwargs)
            break
        except Exception as e:
            if attempt == 2: raise e
            is_rate_limit = "rate_limit" in str(e).lower() or "429" in str(e)
            wait_sec = 30 if is_rate_limit else 2
            await manager.send_log(job_id, f"LLM Error (Attempt {attempt+1}): {'Rate limit hit, waiting 30s...' if is_rate_limit else str(e)}")
            await asyncio.sleep(wait_sec)

    if response:
        try:
            step_cost = completion_cost(completion_response=response)
        except Exception:
            step_cost = 0.0

        if step_cost == 0.0 and hasattr(response, 'usage') and response.usage:
            in_tokens = getattr(response.usage, 'prompt_tokens', 0)
            out_tokens = getattr(response.usage, 'completion_tokens', 0)
            if "llama-3.3-70b" in model.lower() or "llama3-70b" in model.lower():
                step_cost = (in_tokens * 0.00059 / 1000) + (out_tokens * 0.00079 / 1000)
            elif "llama3-8b" in model.lower():
                step_cost = (in_tokens * 0.00005 / 1000) + (out_tokens * 0.00008 / 1000)

        total_cost += step_cost
        await manager.send_cost(job_id, total_cost)
        await manager.send_log(job_id, f"Step Cost: ${step_cost:.6f} | Total Cost: ${total_cost:.6f}")

        raw = response.choices[0].message.content or ""

        # Strip markdown code fences if present
        if "```json" in raw:
            raw = raw.split("```json", 1)[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```", 1)[1].split("```")[0].strip()

        # Try direct parse first
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                parsed = {"action": "extract", "documents": parsed}
            return parsed, total_cost
        except json.JSONDecodeError:
            pass

        # Fallback: use raw_decode to grab the FIRST valid JSON object/array
        # This handles cases where the LLM returns multiple JSON objects back-to-back
        import re
        decoder = json.JSONDecoder()
        # Find the start of the first { or [
        match = re.search(r'[\[\{]', raw)
        if match:
            try:
                parsed, _ = decoder.raw_decode(raw, match.start())
                if isinstance(parsed, list):
                    parsed = {"action": "extract", "documents": parsed}
                return parsed, total_cost
            except json.JSONDecodeError:
                pass

        await manager.send_log(job_id, f"Could not parse LLM response as JSON. Raw: {raw[:200]}")
    return {}, total_cost

class BaseExtractor(ABC):
    @abstractmethod
    async def extract(self, url: str, start_date: str, end_date: str, model: str, job_id: str, manager):
        pass
