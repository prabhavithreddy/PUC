import os
import json
import time
import nest_asyncio
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from .engines import run_extraction

nest_asyncio.apply()

app = FastAPI(title="Gov Docket Extractor", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ExtractRequest(BaseModel):
    url: str
    start_date: str
    end_date: str
    engine: str = "firecrawl"
    model: str = "openrouter/meta-llama/llama-3.3-70b-instruct"

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
        self.cancelled_jobs: set[str] = set()

    def cancel_job(self, job_id: str):
        self.cancelled_jobs.add(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self.cancelled_jobs

    async def connect(self, job_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[job_id] = websocket

    def disconnect(self, job_id: str):
        if job_id in self.active_connections:
            del self.active_connections[job_id]

    async def send_log(self, job_id: str, log: str):
        if job_id in self.active_connections:
            try:
                await self.active_connections[job_id].send_text(json.dumps({"type": "log", "message": log}))
            except Exception:
                pass

    async def send_cost(self, job_id: str, cost: float):
        if job_id in self.active_connections:
            try:
                await self.active_connections[job_id].send_text(json.dumps({"type": "cost", "value": cost}))
            except Exception:
                pass

    async def send_result(self, job_id: str, results: list):
        if job_id in self.active_connections:
            try:
                await self.active_connections[job_id].send_text(json.dumps({"type": "result", "data": results}))
            except Exception:
                pass

manager = ConnectionManager()

# Mount static files for the UI
app.mount("/static", StaticFiles(directory="static"), name="static")

os.makedirs("downloads", exist_ok=True)
app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")

@app.post("/api/extract")
async def start_extraction(request: ExtractRequest, background_tasks: BackgroundTasks):
    job_id = "job_" + str(int(time.time() * 1000))
    background_tasks.add_task(run_extraction, request.url, request.start_date, request.end_date, request.engine, request.model, job_id, manager)
    return {"status": "started", "job_id": job_id}

@app.post("/api/cancel/{job_id}")
async def cancel_extraction(job_id: str):
    manager.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}

@app.websocket("/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await manager.connect(job_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # We don't expect messages from the client in this design, 
            # but we keep the connection open to send logs/results.
    except WebSocketDisconnect:
        manager.disconnect(job_id)
