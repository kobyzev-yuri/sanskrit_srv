"""Sanskrit SRV — FastAPI entry (skeleton)."""
from fastapi import FastAPI

app = FastAPI(
    title="Sanskrit SRV",
    description="MVP sketch: OCR + LLM draft + expert + scholar assistant",
    version="0.0.1",
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "sanskrit_srv", "mvp": True}


@app.get("/api/v1")
def api_root():
    return {
        "docs": "/docs",
        "spec": "see docs/api.md",
        "note": "endpoints not wired yet — skeleton only",
    }
