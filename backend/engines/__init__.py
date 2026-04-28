from .playwright import PlaywrightExtractor
from .sonnet_extractor import SonnetExtractor

class ExtractorFactory:
    _extractors = {
        "playwright": PlaywrightExtractor(),
        "sonnet": SonnetExtractor(),
    }

    @classmethod
    def get_extractor(cls, engine: str):
        # Default to playwright if engine key not found
        return cls._extractors.get(engine, cls._extractors["playwright"])

async def run_extraction(url: str, start_date: str, end_date: str, engine: str, model: str, job_id: str, manager):
    engine_labels = {
        "playwright": "Agentic Playwright",
        "sonnet": "High-Accuracy Sonnet (DOM Chunking)",
    }
    label = engine_labels.get(engine, engine)
    await manager.send_log(job_id, f"Initializing {label} engine...")
    extractor = ExtractorFactory.get_extractor(engine)
    await extractor.extract(url, start_date, end_date, model, job_id, manager)
