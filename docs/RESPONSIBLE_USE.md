# Responsible use

Candor is a **research prototype built for the AI Builder Cup 2026. It is not a diagnostic device** and has no
regulatory clearance.

* **Clinician review.** Calls come from matching a mutation profile against public reference cohort data. A qualified
  clinician or pathologist must make any diagnosis. Candor gives no treatment advice.
* **No real patient data.** Do not enter identifiable patient information. The examples use public study data.
* **Honesty about uncertainty.** The model can decline to commit (*shared*, *indeterminate*, *outlier*); the interface and
  the assistant present that prominently rather than hiding it.
* **Missing annotation is not a finding.** Labels such as `UNK` (stage) or `U` (sex) mean the matching reference patients
  had no recorded value.
* **Self-matches.** If a closest reference profile has the same patient id as the query, that is a self-match against
  reference data, not independent validation.
* **No invented coordinates.** Protein-level input (for example `KRAS G12D`) is never converted to genomic coordinates;
  the user is asked for hg38 coordinates instead.
* **Not yet validated.** Held-out accuracy, sensitivity and time saved have not been measured for this prototype.
* **Data terms.** TCGA-derived data is subject to its own terms of use.

Before any clinical use, a privacy, audit and regulatory pathway would be required.
