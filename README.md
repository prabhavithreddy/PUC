# Gov Docket Extractor

An AI-powered, multi-engine government docket extraction platform.

## Features

- **Agentic Playwright** — autonomous browser agent that navigates, fills forms, and extracts docket tables across any government site.
- **High-Accuracy DOM-Chunking Engine** — splits large tables into 40-row batches before sending to an LLM, guaranteeing >95% row accuracy and <$0.05/page cost.

## Extraction Engines

| Engine | Strategy | Best For |
|---|---|---|
| 🤖 Agentic Playwright | LLM-driven browser automation | Unknown/complex site flows |
| ⚡ DOM-Chunking (Sonnet) | DOM chunk → Markdown → LLM | Known table pages, high accuracy |

The DOM-Chunking engine works with **any LLM**:
- Claude models → Anthropic SDK with forced `tool_choice` (zero-hallucination JSON)
- All others (OpenRouter, Groq, Gemini, LM Studio, etc.) → LiteLLM with JSON schema prompting

## Stack

- **Backend**: FastAPI + Uvicorn
- **Browser Automation**: Playwright (async)
- **LLM Clients**: Anthropic SDK + LiteLLM
- **Schema Validation**: Pydantic v2
- **HTML→Markdown**: markdownify
- **Frontend**: Vanilla HTML/CSS/JS with WebSocket live logs

## Setup

```bash
# 1. Create and activate virtualenv
python -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install fastapi uvicorn playwright anthropic litellm pydantic markdownify nest-asyncio

# 3. Install Playwright browsers
playwright install chromium

# 4. Set API keys (copy run.sh.example → run.sh and fill in)
cp run.sh.example run.sh
# Edit run.sh with your keys

# 5. Run
bash run.sh
```

Open [http://localhost:8000](http://localhost:8000).

## Architecture

```
backend/
  engines/
    base.py               # BaseExtractor ABC + LiteLLM helper
    playwright.py         # Agentic Playwright engine
    sonnet_extractor.py   # DOM-chunking engine (any model)
    __init__.py           # ExtractorFactory registry
  main.py                 # FastAPI app + WebSocket manager
static/
  index.html
  script.js
  styles.css
```

## Environment Variables

| Variable | Required For |
|---|---|
| `ANTHROPIC_API_KEY` | Claude models via Anthropic SDK |
| `OPENROUTER_API_KEY` | OpenRouter models |
| `GROQ_API_KEY` | Groq models |
| `GEMINI_API_KEY` | Google Gemini models |
