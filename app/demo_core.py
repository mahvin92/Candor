"""Framework-free core of the public demo: sessions, rate limits, background jobs, per-user tool binding.
No Candor model code lives here or anywhere in this service: analysis is a call to a private API."""
import collections, functools, logging, re, secrets, threading, time, uuid

import candor_tools as T

log = logging.getLogger("demo")
_STEP = re.compile(r"Step\s+(\d+)\s*/\s*(\d+)")


def public_progress(msg: str) -> str:
    """Map internal progress text to generic wording (do not expose internal pipeline step names)."""
    m = _STEP.search(msg or "")
    if m:
        return f"Running the Candor analysis (step {m[1]} of {m[2]})"
    low = (msg or "").lower()
    if "submitting" in low:
        return "Sending the mutation profile to Candor"
    if "interpreting" in low:
        return "Gemini is interpreting the results"
    return "Working"


class DemoCore:
    def __init__(self, chat_factory, access_code="", max_message_chars=40000, max_turns=30,
                 rate_per_hour=60, start_per_hour=60, session_ttl_s=3600, max_sessions=300,
                 max_concurrent_analyses=2, busy_wait_s=120):
        self.chat_factory = chat_factory
        self.access_code = access_code
        self.max_message_chars, self.max_turns = max_message_chars, max_turns
        self.rate_per_hour, self.start_per_hour = rate_per_hour, start_per_hour
        self.session_ttl_s, self.max_sessions, self.busy_wait_s = session_ttl_s, max_sessions, busy_wait_s
        self.analyses = threading.BoundedSemaphore(max_concurrent_analyses)
        self.sessions, self.jobs = {}, {}
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()
        self.engine = "warming"      # warming | ready | down (set by the warm-up loop)

    # ---- helpers
    def _rate_ok(self, key: str, limit: int) -> bool:
        now, dq = time.time(), self.hits[key]
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def cleanup(self) -> None:
        now = time.time()
        with self.lock:
            for sid in [s for s, v in self.sessions.items() if now - v["last"] > self.session_ttl_s and not v["busy"]]:
                del self.sessions[sid]
            for jid in [j for j, v in self.jobs.items() if now - v["t0"] > 3600]:
                del self.jobs[jid]
            for k in [k for k, dq in self.hits.items() if not dq or now - dq[-1] > 3600]:
                del self.hits[k]

    @staticmethod
    def _capture(ctx, res) -> None:
        """Keep the structured model output so the UI shows exact numbers, not the LLM's paraphrase."""
        if not isinstance(res, dict) or res.get("status") != "ok":
            return
        profiles = res.get("profiles")
        if profiles is None:
            profiles = [{k: v for k, v in res.items() if k not in ("status", "job_id", "not_analysed")}]
        ctx.analyses.append({"mode": res.get("mode_used", "typed"), "n_rows": res.get("n_rows_submitted"),
                             "warning": res.get("warning"), "combined_from_patients": res.get("combined_from_patients"),
                             "not_analysed": res.get("not_analysed"), "profiles": profiles})

    def bind_tools(self, ctx) -> list:
        """The only tools the LLM gets, bound to ONE user's context; analysis calls are concurrency-limited."""
        def bind(fn):
            @functools.wraps(fn)
            def wrapper(*a, **k):
                token = T.use_ctx(ctx)
                try:
                    if not self.analyses.acquire(timeout=self.busy_wait_s):
                        return {"status": "busy", "detail": "The analysis service is busy. Please retry in a minute."}
                    try:
                        res = fn(*a, **k)
                        self._capture(ctx, res)
                        return res
                    finally:
                        self.analyses.release()
                finally:
                    T.reset_ctx(token)
            wrapper.__annotations__ = dict(fn.__annotations__)
            return wrapper
        return [bind(T.analyze_pasted_mutation_table), bind(T.analyze_variants)]

    # ---- API (each returns (http_status, payload))
    def config(self):
        return 200, {"access_code_required": bool(self.access_code), "max_turns": self.max_turns, "engine": self.engine}

    def start(self, code: str, ip: str):
        with self.lock:
            if not self._rate_ok("start:" + ip, self.start_per_hour):
                return 429, {"error": "Too many attempts. Please try again later."}
            if self.access_code and not secrets.compare_digest((code or "").strip(), self.access_code):
                return 401, {"error": "Invalid access code."}
        self.cleanup()
        with self.lock:
            if len(self.sessions) >= self.max_sessions:
                return 503, {"error": "The demo is at capacity. Please try again shortly."}
            ctx = T.Ctx()
            sid = secrets.token_urlsafe(24)
            try:
                chat = self.chat_factory(ctx, self.bind_tools(ctx))
            except Exception:
                log.exception("could not create chat")
                return 503, {"error": "The assistant is temporarily unavailable."}
            self.sessions[sid] = {"ctx": ctx, "chat": chat, "turns": 0, "last": time.time(), "busy": False}
        return 200, {"session_id": sid, "max_turns": self.max_turns}

    def submit(self, session_id: str, message: str, ip: str):
        message = (message or "").strip()
        if not message:
            return 400, {"error": "Empty message."}
        if len(message) > self.max_message_chars:
            return 413, {"error": "Message too long."}
        with self.lock:
            sess = self.sessions.get(session_id)
            if not sess:
                return 404, {"error": "Session expired. Please refresh the page."}
            if sess["busy"]:
                return 409, {"error": "Still working on your previous message."}
            if sess["turns"] >= self.max_turns:
                return 429, {"error": "Message limit reached for this session. Please start a new session."}
            if not self._rate_ok("chat:" + ip, self.rate_per_hour):
                return 429, {"error": "Rate limit reached. Please try again later."}
            sess["busy"], sess["last"] = True, time.time()
            jid = uuid.uuid4().hex
            job = {"session_id": session_id, "status": "running", "progress": "Thinking", "phase": "thinking", "step": 0, "total": None,
                   "reply": None, "analyses": [], "t0": time.time()}
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(sess, job, message), daemon=True).start()
        return 200, {"job_id": jid}

    def _run(self, sess, job, message):
        ctx = sess["ctx"]
        ctx.analyses = []

        def on_progress(m):
            low, mm = (m or "").lower(), _STEP.search(m or "")
            if mm:
                job.update(phase="analysis", step=int(mm[1]), total=int(mm[2]))
            elif "submitting" in low:
                job.update(phase="analysis", step=0)
            elif "interpreting" in low:
                job.update(phase="interpreting")
            job["progress"] = public_progress(m)
        ctx.progress = on_progress
        token = T.use_ctx(ctx)
        try:
            text, _info = T.prepare_message(message)
            job["progress"] = "Thinking"
            reply = sess["chat"].send_message(text).text
            job["analyses"] = list(ctx.analyses)
            job["reply"], job["status"] = (reply or "(No response. Please rephrase and try again.)"), "done"
        except Exception:
            log.exception("chat turn failed")
            job["reply"], job["status"] = "Sorry, something went wrong while analysing. Please try again in a moment.", "error"
        finally:
            T.reset_ctx(token)
            if ctx.last_error:
                log.warning("Candor call failed: %s", ctx.last_error)   # server log only, never shown to users
                ctx.last_error = None
            sess["turns"] += 1
            sess["last"], sess["busy"] = time.time(), False

    def job(self, job_id: str, session_id: str):
        j = self.jobs.get(job_id)
        if not j or j["session_id"] != session_id:
            return 404, {"error": "Unknown job."}
        return 200, {"status": j["status"], "progress": j["progress"], "phase": j["phase"], "step": j["step"],
                     "total": j["total"], "reply": j["reply"], "analyses": j["analyses"]}
