"""Public demo: chat UI + API in front of Gemini (Vertex AI). Contains no Candor code or weights."""
import logging, os, threading, time

import candor_tools as T
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

from demo_core import DemoCore
from system_prompt import MOCK_NOTICE, SYSTEM

logging.basicConfig(level=logging.INFO)
PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
SYSTEM_PROMPT = SYSTEM + (MOCK_NOTICE if T.ENGINE_MODE == "mock" else "")
if T.ENGINE_MODE == "mock":
    logging.warning("CANDOR_ENGINE_MODE=mock: results come from a SYNTHETIC test engine, not the Candor model")


def chat_factory(ctx, tools):
    return client.chats.create(model=MODEL, config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT, tools=tools, temperature=0.2))


core = DemoCore(
    chat_factory,
    access_code=os.environ.get("DEMO_ACCESS_CODE", ""),
    max_turns=int(os.environ.get("MAX_TURNS", "30")),
    rate_per_hour=int(os.environ.get("RATE_PER_HOUR", "60")),
    max_concurrent_analyses=int(os.environ.get("MAX_CONCURRENT_ANALYSES", "2")),
)
threading.Thread(target=lambda: [(time.sleep(60), core.cleanup()) for _ in iter(int, 1)], daemon=True).start()

# Warm the private Candor service (it loads its reference data on its first job) so a judge never
# meets a cold start. Re-checked every 20 minutes; the UI shows the engine state.
_WARM = [{"Hugo_Symbol": g, "Chromosome": c, "Start_Position": p, "End_Position": p, "Reference_Allele": r,
          "Tumor_Seq_Allele2": a, "Tumor_Sample_Barcode": "WARMUP"}
         for g, c, p, r, a in (("TP53", "chr17", 7675088, "C", "T"), ("KRAS", "chr12", 25245350, "C", "T"),
                               ("PIK3CA", "chr3", 179218303, "G", "A"))]


def _warmup_loop():
    failures = 0
    while True:
        token = T.use_ctx(T.Ctx())
        try:
            ok = bool(T._run_predict({"variant_records": _WARM}).get("ok"))
        except Exception:
            ok = False
        finally:
            T.reset_ctx(token)
        failures = 0 if ok else failures + 1
        core.engine = "ready" if ok else ("down" if failures >= 3 else "warming")
        logging.info("warm-up: engine=%s", core.engine)
        time.sleep(1200 if ok else 45)


if os.environ.get("WARMUP", "1") == "1":
    threading.Thread(target=_warmup_loop, daemon=True).start()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
INDEX = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def client_ip(request: Request) -> str:
    # Cloud Run appends the real client address last; earlier entries can be client-supplied.
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() or (request.client.host if request.client else "unknown")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class StartReq(BaseModel):
    code: str = ""


class ChatReq(BaseModel):
    session_id: str
    message: str


def reply(result):
    status, payload = result
    return JSONResponse(payload, status_code=status)


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    return reply(core.config())


@app.post("/api/start")
def start(req: StartReq, request: Request):
    return reply(core.start(req.code, client_ip(request)))


@app.post("/api/chat")
def chat(req: ChatReq, request: Request):
    return reply(core.submit(req.session_id, req.message, client_ip(request)))


@app.get("/api/job/{job_id}")
def job(job_id: str, session_id: str):
    return reply(core.job(job_id, session_id))
