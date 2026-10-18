# Estimated cost

An estimate for the demo configuration, from list prices. Check current prices before relying on it.

| Component | Configuration | Estimate / month |
|---|---|---|
| Cloud Run `candor` | 1 vCPU, 1 GiB, always on | about $62 |
| Cloud Run `candor-engine` | 4 vCPU, 16 GiB, always on | about $323 |
| Gemini 2.5 Flash on Vertex AI | about 10K input and 1.5K output tokens per analysis turn | about $0.007 per turn (about $7 per 1,000 turns) |
| **Prototype hosting total (Cloud Run)** | | **about $386** |

## Assumptions

* 30-day month (2,592,000 seconds), instance-based billing (CPU always allocated), as deployed by the script.
* Tier 2 region (`asia-southeast1`) list prices: $0.0000216 per vCPU-second and $0.0000024 per GiB-second, before any free tier.
* Gemini 2.5 Flash at $0.30 / $2.50 per 1M input / output tokens. Token counts per turn are an estimate.
* Excludes egress, storage, Secret Manager, Artifact Registry and Cloud Build.

Calculation: web = (1 x 0.0000216 + 1 x 0.0000024) x 2,592,000 = $62.2; engine = (4 x 0.0000216 + 16 x 0.0000024) x
2,592,000 = $323.5.

## Levers

* Firestore-backed sessions remove the single-instance pin and enable scale-to-zero.
* Cache reference data in the engine to shrink its memory footprint.
* Committed-use discounts (about 17% off the CPU rate).
* A Tier 1 region lowers vCPU and memory rates.
