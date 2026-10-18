"""SYNTHETIC test engine for the Candor web app.

This is NOT the Candor model. It exists so the web app, the Gemini agent and the guardrails can be run and tested
end to end without access to the private Candor engine. It implements the same small HTTP contract the app uses
(/health, /predict, /status/{job}, /result/{job}) and returns pseudo-random, clearly labelled output:

  * labels are placeholders (SYN-A, SYN-1, SYN-F ...), never real cancer types, stages or sexes;
  * every profile carries a notice saying the values are synthetic;
  * in test mode the app also instructs Gemini to say so in every answer.

Never present anything produced by this service as a result.
"""
import hashlib, os, random, time, uuid

from fastapi import FastAPI, HTTPException, Request

KEY = os.environ.get("MOCK_ENGINE_KEY", "local-dev-key")
KEY_HEADER = os.environ.get("CANDOR_ENGINE_KEY_HEADER", "X-Candor-Token").lower()
DURATION_S = float(os.environ.get("MOCK_DURATION_S", "3"))
STEPS = 4
NOTICE = ("SYNTHETIC OUTPUT FROM THE LOCAL TEST ENGINE. Values are pseudo-random and have no biological meaning. "
          "This is not a Candor model result.")
LABELS = {"CancerType": ["SYN-A", "SYN-B", "SYN-C", "SYN-D", "SYN-E"],
          "Stage": ["SYN-1", "SYN-2", "SYN-3", "SYN-4"],
          "Sex": ["SYN-F", "SYN-M"]}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
JOBS: dict = {}


def _auth(request: Request) -> None:
    if request.headers.get(KEY_HEADER) != KEY:
        raise HTTPException(status_code=403, detail="forbidden")


def _groups(body: dict) -> dict:
    """patient id -> list of gene symbols, from either input form the app can send."""
    groups: dict = {}
    if body.get("variant_text"):
        for line in str(body["variant_text"]).splitlines()[1:]:          # first line is the header
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                groups.setdefault(parts[4] or "CASE", []).append(parts[0])
    for rec in body.get("variant_records") or []:
        groups.setdefault(rec.get("Tumor_Sample_Barcode") or "CASE", []).append(rec.get("Hugo_Symbol") or "UNKNOWN")
    return groups


def _target(rng: random.Random, name: str, genes: list) -> dict:
    labels = LABELS[name]
    raw = sorted((rng.random() ** 2 + 0.05 for _ in labels), reverse=True)
    total = sum(raw)
    scores = [round(x / total, 4) for x in raw]
    margin = round(scores[0] - scores[1], 4)
    cls = "distinct" if margin >= 0.25 else ("shared" if margin < 0.08 else "indeterminate")
    order = rng.sample(labels, len(labels))
    dist = [{"label": l, "score": s} for l, s in zip(order, scores)]
    return {"top1": order[0], "top1_score": scores[0], "top2": order[1], "top2_score": scores[1], "margin": margin,
            "classification": cls, "shared_with": [order[1]] if cls == "shared" else [], "distribution": dist,
            "n_supporting_markers": rng.randint(1, max(1, len(genes))),
            "supporting_markers": rng.sample(genes, min(3, len(genes)))}


def _profile(pid: str, genes: list) -> dict:
    seed = int(hashlib.sha256((pid + "|" + ",".join(sorted(genes))).encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    targets = {n: _target(rng, n, genes) for n in LABELS}
    return {"query_patient_id": pid, "profile_classification": targets["CancerType"]["classification"],
            "primary_target": "CancerType", "n_markers": len(genes), "n_informative_markers": max(1, int(len(genes) * 0.7)),
            "outlier_fraction": round(rng.random() * 0.1, 2), "mean_marker_specificity": round(rng.random(), 3),
            "targets": targets,
            "top_reference_profiles": [{"patient_id": f"SYNTH-{i:04d}", "similarity": round(rng.random(), 3),
                                        "matched_marker_names": rng.sample(genes, min(3, len(genes)))} for i in range(1, 4)],
            "marker_effectiveness": [{"marker": g, "specificity": round(rng.random(), 3), "match_score": round(rng.random(), 3),
                                      "classification": "synthetic"} for g in genes[:6]],
            "interpretation": NOTICE, "ground_truth_note": None, "validation_note": NOTICE}


@app.get("/health")
def health(request: Request):
    _auth(request)
    return {"version_label": "mock-engine (SYNTHETIC)", "supported_molecules": ["gene_mutation"], "queue_depth": 0}


@app.post("/predict")
async def predict(request: Request):
    _auth(request)
    groups = _groups(await request.json())
    if not groups:
        raise HTTPException(status_code=422, detail="no variants")
    job = uuid.uuid4().hex[:12]
    JOBS[job] = {"t0": time.time(), "groups": groups}
    return {"job_id": job}


@app.get("/status/{job}")
def status(job: str, request: Request):
    _auth(request)
    j = JOBS.get(job)
    if not j:
        raise HTTPException(status_code=404, detail="unknown job")
    elapsed = time.time() - j["t0"]
    if elapsed >= DURATION_S:
        return {"status": "done", "progress": f"Step {STEPS}/{STEPS}"}
    return {"status": "running", "progress": f"Step {min(STEPS, 1 + int(elapsed / DURATION_S * STEPS))}/{STEPS}"}


@app.get("/result/{job}")
def result(job: str, request: Request):
    _auth(request)
    j = JOBS.get(job)
    if not j:
        raise HTTPException(status_code=404, detail="unknown job")
    profiles = [_profile(pid, genes) for pid, genes in j["groups"].items()]
    return {"synthetic": True, "notice": NOTICE, "profile_prediction": {"profiles": profiles}}
