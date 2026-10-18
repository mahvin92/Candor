"""Tools Gemini can call. Design rules:
  * Gemini only reformats what the user typed. It NEVER supplies coordinates from memory.
  * Parsing/validation is deterministic code. Anything that is not a genomic (hg38) variant
    is returned as 'needs_genomic_coordinates' so the agent asks the user instead of guessing.
  * Candor's numbers/labels are passed through unchanged for Gemini to explain.
"""
import contextvars, json, os, re, subprocess, sys, time
import httpx

CANDOR_ENGINE_URL = os.environ.get("CANDOR_ENGINE_URL", "").rstrip("/")
CANDOR_ENGINE_KEY = os.environ.get("CANDOR_ENGINE_KEY", "")
MAX_VARIANTS = 200
POLL_TIMEOUT_S = int(os.environ.get("CANDOR_POLL_TIMEOUT", "1800"))
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "") == "1"   # strip algorithm internals from tool output
# "live": the private Candor engine (IAM-protected Cloud Run service). "mock": the SYNTHETIC test engine in ../mock_engine.
ENGINE_MODE = os.environ.get("CANDOR_ENGINE_MODE", "live").lower()
# Name of the HTTP header that carries the shared engine key. Override it in the deployment environment if your engine expects another name.
KEY_HEADER = os.environ.get("CANDOR_ENGINE_KEY_HEADER", "X-Candor-Token")


class Ctx:
    """Per-conversation state: pasted table, last error, progress hook. One per web session."""
    def __init__(self):
        self.rows = []
        self.last_error = None
        self.progress = None
        self.analyses = []          # structured results of the current turn (for the UI cards)


DEFAULT_CTX = Ctx()                                       # used by the single-user CLI
_CTX = contextvars.ContextVar("candor_ctx", default=DEFAULT_CTX)


def ctx() -> Ctx:
    return _CTX.get()


def use_ctx(c: Ctx):
    return _CTX.set(c)


def reset_ctx(token) -> None:
    _CTX.reset(token)

VALID_CHROMS = {str(i) for i in range(1, 23)} | {"X", "Y", "M", "MT"}

# Accepted coordinate forms (hg38 / GRCh38 ONLY), optionally preceded by a gene symbol:
#   "TP53 chr17:7675088 C>T" | "chr17:g.7675088C>T" | "17:7675088:C:T" | "chr17 7675088 C T"
# NC_ accessions are rejected on purpose: the version suffix can silently mean hg19 vs hg38.
_GENE_PREFIX = re.compile(r"^(?P<gene>[A-Za-z][A-Za-z0-9\-]{0,15})[\s,;]+(?P<rest>.+)$")
_CHR_LIKE = re.compile(r"^(chr[0-9XYM]+|NC_\d+)$", re.IGNORECASE)
_COORD = re.compile(
    r"^(?:chr)?(?P<chrom>[0-9]{1,2}|X|Y|MT|M)\s*[:\s]\s*(?:g\.)?\s*"
    r"(?P<pos>\d{1,9})\s*[:\s]?\s*(?P<ref>[ACGT]+)\s*(?:>|:|/|\s)\s*(?P<alt>[ACGT]+)$",
    re.IGNORECASE,
)
_PROTEIN = re.compile(r"(p\.)?[A-Z][a-z]{0,2}\d{1,5}([A-Z][a-z]{0,2}|\*|fs|del|ins)", re.IGNORECASE)


def parse_variant_strings(items: list[str]) -> dict:
    """Deterministically turn free-text variant strings into validated hg38 records."""
    records, needs_coords, errors = [], [], []
    for raw in items[:MAX_VARIANTS]:
        s = str(raw).strip()
        if not s:
            continue
        gene = None
        gm = _GENE_PREFIX.match(s)
        if gm and not _CHR_LIKE.match(gm["gene"]):
            gene, s_coord = gm["gene"].upper(), gm["rest"].strip()
        else:
            s_coord = s
        m = _COORD.match(s_coord)
        if not m:
            kind = "protein_or_gene_level" if _PROTEIN.search(s) else "unrecognised"
            needs_coords.append({"input": s, "reason": kind})
            continue
        chrom = m["chrom"].upper()
        if chrom == "MT":
            chrom = "M"
        if chrom not in VALID_CHROMS:
            errors.append({"input": s, "error": f"invalid chromosome {chrom}"}); continue
        pos, ref, alt = int(m["pos"]), m["ref"].upper(), m["alt"].upper()
        if pos < 1 or ref == alt:
            errors.append({"input": s, "error": "invalid position or ref == alt"}); continue
        records.append({
            "Hugo_Symbol": gene or "UNKNOWN",
            "Chromosome": f"chr{chrom}",
            "Start_Position": pos,
            "End_Position": pos + len(ref) - 1,
            "Reference_Allele": ref,
            "Tumor_Seq_Allele2": alt,
        })
    if len(items) > MAX_VARIANTS:
        errors.append({"input": "...", "error": f"only the first {MAX_VARIANTS} variants were used"})
    return {"records": records, "needs_genomic_coordinates": needs_coords, "errors": errors}


def _headers() -> dict:
    h = {KEY_HEADER: CANDOR_ENGINE_KEY}
    if ENGINE_MODE == "mock":          # local synthetic test engine: no Cloud Run identity needed
        return h
    try:  # Cloud Run service-to-service identity (private service)
        import google.auth.transport.requests as gtr
        from google.oauth2 import id_token
        h["Authorization"] = "Bearer " + id_token.fetch_id_token(gtr.Request(), CANDOR_ENGINE_URL)
    except Exception:  # local dev / Cloud Shell user credentials
        try:
            tok = subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True, text=True).stdout.strip()
        except OSError:                 # gcloud not installed
            tok = ""
        if tok:
            h["Authorization"] = "Bearer " + tok
    return h


# ---------------------------------------------------------------------------------------------
# Pasted mutation tables: captured by the APP, never retyped by the LLM.
# Accepted rows (Candor's own format; header optional, PatientID optional):
#   Gene,Chromosome,Position,AlleleChange,PatientID     e.g.  OR2L8,chr1,247949008-247949008,G>T,TCGA-05-4382
# ---------------------------------------------------------------------------------------------
TABLE_HEADER = "Gene,Chromosome,Position,AlleleChange,PatientID"
MAX_TABLE_ROWS = int(os.environ.get("MAX_TABLE_ROWS", "5000"))
MAX_PROFILES_RETURNED = 8
_SEP = r"[,\t|;]"
TABLE_ROW = re.compile(
    rf"^\s*(?P<gene>[A-Za-z0-9._\-]+)\s*{_SEP}\s*(?:chr)?(?P<chrom>[0-9]{{1,2}}|X|Y|MT|M)\s*{_SEP}\s*"
    rf"(?P<pos>\d{{1,9}})(?:\s*-\s*(?P<end>\d{{1,9}}))?\s*{_SEP}\s*"
    rf"(?P<ref>[ACGTacgt\-]*)\s*>\s*(?P<alt>[ACGTacgt\-]*)\s*(?:{_SEP}\s*(?P<patient>[^\s,\t|;]+))?\s*$"
)
_LABEL_PREFIX = re.compile(r"^\s*[A-Za-z][A-Za-z ]{0,30}:\s+")   # tolerate "here: GENE,chr1,..."


def looks_like_table_row(line: str) -> bool:
    m = TABLE_ROW.match(line) or TABLE_ROW.match(_LABEL_PREFIX.sub("", line))
    return bool(m) and m["chrom"].upper() in VALID_CHROMS


_HEADER_LINE = re.compile(rf"^\s*gene\s*{_SEP}\s*chrom", re.IGNORECASE)


def register_pasted_table(message: str):
    """Pull table rows out of a user message. Returns (remaining_text, info_or_None)."""
    rows, rest, dropped = [], [], 0
    for line in message.splitlines():
        m = TABLE_ROW.match(line) or TABLE_ROW.match(_LABEL_PREFIX.sub("", line))
        ok = False
        if m:
            chrom = m["chrom"].upper()
            chrom = "M" if chrom == "MT" else chrom
            pos = int(m["pos"]); end = int(m["end"]) if m["end"] else pos
            ok = chrom in VALID_CHROMS and pos >= 1 and end >= pos
        if ok and len(rows) < MAX_TABLE_ROWS:
            patient = m["patient"] or "UNKNOWN"
            position = m["pos"] + (f"-{m['end']}" if m["end"] else "")
            ref, alt = m["ref"].upper().replace("-", ""), m["alt"].upper().replace("-", "")
            rows.append((patient, f"{m['gene']},chr{chrom},{position},{ref}>{alt},{patient}"))
        elif ok:
            dropped += 1
        elif not _HEADER_LINE.match(line):
            rest.append(line)
    if not rows:
        return message, None
    ctx().rows = rows
    counts: dict = {}
    for p, _ in rows:
        counts[p] = counts.get(p, 0) + 1
    info = {"n_rows": len(rows), "n_patients": len(counts), "dropped": dropped,
            "patients": [{"id": p, "n_variants": n} for p, n in sorted(counts.items(), key=lambda x: -x[1])]}
    return "\n".join(rest).strip(), info


EXPECTED_TARGETS = ("CancerType", "Stage", "Sex")


def _condense_target(tg: dict) -> dict:
    return {
        "top1": tg.get("top1"), "top1_score": tg.get("top1_score"),
        "top2": tg.get("top2"), "top2_score": tg.get("top2_score"),
        "margin": tg.get("margin"),
        "classification": tg.get("classification"),        # distinct | shared | indeterminate | outlier
        "shared_with": tg.get("shared_with"),
        "distribution": (tg.get("distribution") or [])[:5],
        "aggregation_basis": None if PUBLIC_MODE else tg.get("aggregation_basis"),
        "n_supporting_markers": tg.get("n_supporting_markers"),
        "supporting_markers": (tg.get("supporting_markers") or [])[:3],
    }


def _trim_refs(refs, n: int) -> list:
    out = []
    for r in (refs or [])[:n]:
        r = dict(r)
        if isinstance(r.get("matched_marker_names"), list):
            r["matched_marker_names"] = r["matched_marker_names"][:15]
        out.append(r)
    return out


def _condense_profile(prof: dict, n_refs: int = 5) -> dict:
    """The RAG-style evidence layer for ONE patient profile; drops bulky fields."""
    targets = prof.get("targets") or {}
    return {
        "query_patient_id": prof.get("query_patient_id"),
        "profile_classification": prof.get("profile_classification"),
        "primary_target": prof.get("primary_target"),
        "n_markers": prof.get("n_markers"),
        "n_informative_markers": prof.get("n_informative_markers"),
        "outlier_fraction": prof.get("outlier_fraction"),
        "mean_marker_specificity": prof.get("mean_marker_specificity"),
        "targets": {name: _condense_target(tg) for name, tg in targets.items()},
        "targets_not_available": [t for t in EXPECTED_TARGETS if t not in targets],   # never infer these
        "closest_reference_profiles": _trim_refs(prof.get("top_reference_profiles"), n_refs),
        "top_markers": [
            {k: m.get(k) for k in ("marker", "specificity", "match_score", "classification")}
            for m in (prof.get("marker_effectiveness") or [])[:6]
        ],
        "interpretation": prof.get("interpretation"),
        "ground_truth_note": prof.get("ground_truth_note"),
        "validation_note": prof.get("validation_note"),
    }


def _profiles_of(result: dict) -> list:
    pb = result.get("profile_prediction") or {}
    return pb.get("profiles") or ([pb["primary_profile"]] if pb.get("primary_profile") else [])


def _progress(msg: str) -> None:
    cb = ctx().progress
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _describe(e: Exception) -> str:
    msg = f"{type(e).__name__}: {str(e)[:200]}"
    resp = getattr(e, "response", None)
    if resp is not None:
        msg += f" | HTTP {resp.status_code}: {str(resp.text)[:200]}"
    return msg


def _scrub(text: str) -> str:
    for secret, label in ((CANDOR_ENGINE_URL, "[engine]"), (os.environ.get("CHECKPOINT_PATH", ""), "[checkpoint]"), (CANDOR_ENGINE_KEY, "[secret]")):
        if secret:
            text = text.replace(secret, label)
    return text


def _fail(status: str, detail: str, **extra) -> dict:
    detail = _scrub(detail)
    ctx().last_error = f"{status}: {detail}"
    return {"ok": False, "status": status, "detail": detail, **extra}


def _run_predict(payload: dict) -> dict:
    if not CANDOR_ENGINE_URL.startswith("http"):
        return _fail("not_configured", "CANDOR_ENGINE_URL is not set in this shell (export it, then restart the agent)")
    body = {"user_id": "agent", "molecule_type": "gene_mutation", "predict_mode": "balanced",
            "checkpoint_path": os.environ.get("CHECKPOINT_PATH", ""),
            "checkpoint_revision": os.environ.get("CHECKPOINT_REVISION", ""), **payload}
    try:
        with httpx.Client(base_url=CANDOR_ENGINE_URL, headers=_headers(), timeout=60) as c:
            _progress("Candor: submitting the mutation profile")
            r = c.post("/predict", json=body)
            r.raise_for_status()
            job = r.json()["job_id"]
            t0, last = time.time(), None
            while time.time() - t0 < POLL_TIMEOUT_S:
                s = c.get(f"/status/{job}").json()
                if s.get("progress") and s["progress"] != last:
                    last = s["progress"]
                    _progress(f"Candor: {last}")
                if s["status"] == "done":
                    _progress("Gemini is interpreting the results")
                    return {"ok": True, "job_id": job, "result": c.get(f"/result/{job}").json()}
                if s["status"] == "error":
                    return _fail("analysis_error", str(s.get("error"))[:300])
                time.sleep(4)
            return _fail("timeout", f"job {job} did not finish within {POLL_TIMEOUT_S}s", job_id=job)
    except Exception as e:
        return _fail("service_error", _describe(e))


def check_engine_health() -> dict:
    """Check whether the Candor analysis service is reachable and which molecules/model it serves."""
    try:
        r = httpx.get(f"{CANDOR_ENGINE_URL}/health", headers=_headers(), timeout=30)
        r.raise_for_status()
        j = r.json()
        return {"ok": True, "version": j.get("version_label"), "supported_molecules": j.get("supported_molecules"),
                "queue_depth": j.get("queue_depth")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def analyze_pasted_mutation_table(patient_id: str = "", mode: str = "combined") -> dict:
    """Analyze the mutation table the user pasted or uploaded. The app stores the table, so do NOT
    retype or alter any rows. Call this tool ONCE per request.

    Args:
        patient_id: Optional. Analyze only the rows of this one PatientID.
        mode: "combined" (default) pools ALL rows into ONE mutation profile and returns one result.
            "per_patient" returns a separate profile for each PatientID in the table; use it only
            when the user explicitly asks for per-patient results.

    Returns:
        Calls for cancer type, disease stage and patient sex (each with its own classification,
        scores and margin), closest reference profiles and marker evidence. If the result contains
        combined_from_patients, the rows came from several different PatientIDs and were pooled.
    """
    rows = ctx().rows
    if not rows:
        return {"status": "no_table", "instruction": "Ask the user to paste a mutation table "
                "(Gene, Chromosome, Position, Ref>Alt, PatientID) with hg38 coordinates."}
    pid = (patient_id or "").strip()
    chosen = [(p, line) for p, line in rows if not pid or p == pid]
    if not chosen:
        return {"status": "unknown_patient", "available_patients": sorted({p for p, _ in rows})[:50]}
    patients = sorted({p for p, _ in chosen})
    per_patient = (mode or "combined").strip().lower() == "per_patient"

    if len(patients) > 1 and not per_patient:
        # One pooled profile: give every row the same PatientID so the service builds a single profile.
        lines = [line.rsplit(",", 1)[0] + ",COMBINED" for _, line in chosen]
        used_mode = "combined"
    else:
        lines = [line for _, line in chosen]
        used_mode = "per_patient" if len(patients) > 1 else "single_patient"

    out = _run_predict({"variant_text": TABLE_HEADER + "\n" + "\n".join(lines)})
    if not out["ok"]:
        return {k: v for k, v in out.items() if k != "ok"}
    profiles = _profiles_of(out["result"])
    n_refs = 5 if len(profiles) <= 2 else 3
    res = {"status": "ok", "job_id": out["job_id"], "mode_used": used_mode,
           "n_rows_submitted": len(lines), "n_profiles": len(profiles),
           "profiles": [_condense_profile(p, n_refs) for p in profiles[:MAX_PROFILES_RETURNED]],
           "profiles_not_shown": max(0, len(profiles) - MAX_PROFILES_RETURNED)}
    if used_mode == "combined":
        res["combined_from_patients"] = patients[:20]
        res["warning"] = (f"{len(patients)} different PatientIDs were pooled into one profile, so this describes "
                          "the pooled mutation set, not a single tumor.")
    return res


def analyze_variants(variants: list[str]) -> dict:
    """Analyze a few mutations the user TYPED in chat (not a pasted table), as one tumor profile.

    Args:
        variants: ALL typed mutations in ONE call (one string per mutation), copied from what the user typed, each with hg38 genomic
            coordinates, e.g. "TP53 chr17:7675088 C>T" or "chr12:g.25245350C>T". Do NOT invent or
            look up coordinates. If the user gave only protein-level changes (e.g. "KRAS G12D"),
            do not call this tool for them: ask for hg38 coordinates instead.

    Returns:
        The model's profile-level calls for cancer type, stage and sex with evidence, or a status
        explaining which inputs could not be used.
    """
    parsed = parse_variant_strings(variants)
    if not parsed["records"]:
        return {"status": "no_usable_variants", **{k: v for k, v in parsed.items() if k != "records"},
                "instruction": "Ask the user for hg38 genomic coordinates (chromosome, position, ref>alt)."}
    for rec in parsed["records"]:
        rec["Tumor_Sample_Barcode"] = "CASE"  # one profile per call; no patient identifiers
    out = _run_predict({"variant_records": parsed["records"]})
    if not out["ok"]:
        return {k: v for k, v in out.items() if k != "ok"}
    profiles = _profiles_of(out["result"])
    res = {"status": "ok", "job_id": out["job_id"], **(_condense_profile(profiles[0]) if profiles else {})}
    if parsed["needs_genomic_coordinates"] or parsed["errors"]:
        res["not_analysed"] = {"needs_genomic_coordinates": parsed["needs_genomic_coordinates"], "errors": parsed["errors"]}
    return res


def prepare_message(message: str):
    """Capture any pasted table (stored per conversation) and return (text_for_the_model, info_or_None)."""
    rest, info = register_pasted_table(message)
    if not info:
        return message, None
    ids = ", ".join(p["id"] for p in info["patients"][:8])
    extra = f" Only the first {MAX_TABLE_ROWS} rows were kept ({info['dropped']} ignored)." if info["dropped"] else ""
    note = (f"[App note: the user pasted a mutation table with {info['n_rows']} rows from {info['n_patients']} "
            f"patient(s): {ids}.{extra} The app stored it. Call analyze_pasted_mutation_table once "
            "(default: one combined profile); do not retype rows.]")
    return (rest + "\n\n" if rest else "") + note, info
