# Architecture

![Architecture](images/architecture.png)

## Components

| Component | Role | Notes |
|---|---|---|
| Browser | Chat UI (static HTML/CSS/JS), light/dark, mobile, print | No external scripts or CDNs; strict CSP |
| **Cloud Run `candor`** (public) | Serves the UI and API; runs the Gemini agent | FastAPI + Uvicorn; sessions, background jobs, rate limits, access code |
| **Gemini 2.5 Flash on Vertex AI** | Understands the request, calls tools (function calling), explains results | Temperature 0.2; grounded by the system prompt; not fine-tuned |
| **Cloud Run `candor-engine`** (private) | Serves the proprietary Candor model | Not in this repository; IAM-only; async job API |
| Secret Manager | Engine key, demo access code | Injected as environment variables at deploy time |
| Artifact Registry + Cloud Build | Container build and storage | Scripted in `scripts/deploy_cloud_run.sh` |

## Request lifecycle

1. The browser posts a message to `/api/chat`; the server returns a job id and the UI polls `/api/job/{id}`.
2. **Deterministic capture:** if the message contains a mutation table, the app stores the rows itself
   (`register_pasted_table`). The model is told a table exists and is never asked to retype it.
3. Gemini receives the message and two tools: `analyze_pasted_mutation_table` and `analyze_variants`.
4. The tool submits an analysis job to the private engine (`POST /predict`), polls `/status/{job}` and reads
   `/result/{job}`. Progress is relayed to the UI.
5. The structured result is captured for the UI (exact numbers) and passed back to Gemini, which explains it.
6. The UI renders the result cards from the structured data; Gemini's text is shown alongside, never in place of it.

## Public / private boundary

* The public image contains no model code, weights or data. `scripts/deploy_cloud_run.sh` refuses to build if model-like
  files or credential-like strings are present in `app/`.
* The engine accepts calls only with Cloud Run IAM credentials (the web app's service account holds `roles/run.invoker`
  on it) **and** a shared key from Secret Manager.
* Errors shown to users are generic; internal details are logged server-side and scrubbed of URLs and keys.

## Security controls in the web app

Content-Security-Policy (`default-src 'self'`), `X-Content-Type-Options`, `Referrer-Policy`, `Cache-Control: no-store`
on API routes, per-IP rate limits, per-session turn limits, a cap on concurrent analyses, input size caps, an access
code, and a least-privilege service account (Vertex AI user, invoker on the engine, accessor on two secrets).

## Scaling

The prototype is deliberately pinned to **one instance** (`--min-instances 1 --max-instances 1`) because chat sessions
and job state are held in memory. That is the main scalability limit today. The planned path:

1. Move sessions and job state to **Firestore** so instances are stateless and can autoscale (and scale to zero).
2. Add **BigQuery** for cohort analytics and **Vertex AI Search** for grounded retrieval with citations.
3. Use **ADK / Agent Engine** to split the agent into specialised steps (curation, literature, reporting).
4. Add **Document AI** to ingest pathology and lab reports.
