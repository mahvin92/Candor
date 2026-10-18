# Prototype performance report

A simple report built around three questions: **how fast, does it work every time, how good.** Measured by the team on
the hosted demo (Gemini 2.5 Flash on Vertex AI and the private Candor engine, both on Google Cloud).

## Summary

| Question | Measure | Result |
|---|---|---|
| **How fast?** Time from sending to answer | Multi-gene profile (16 mutations) | **24 s** |
| | Hotspot mutations (3 mutations) | **17 s** |
| **Does it work every time?** Runs, tests and behaviour checks | Automated tests | **18 / 18** |
| | Live runs completed | **25 / 25** |
| | Unsafe input refused | **10 / 10** |
| **How good?** Reproducibility of the calls | Same results across repeated prompts | **100%** |

* Run times are the **average of 5 runs each**.
* Reproducibility: similar mutations prompted 5 times yielded the same results.

## Speed

Time is measured end to end, from sending the message to the finished answer in the chat. It includes Gemini
understanding the request, the Candor engine analysing the mutations, and Gemini explaining the result. Both examples
finished in under half a minute on average. The app also shows the elapsed time on every result.

## Reliability

* **Automated tests: 18 / 18.** 14 guardrail tests (input handling, validation, caps, secret scrubbing) and 4
  integration tests against the synthetic test engine. Run them with `pytest -q`.
* **Live runs completed: 25 / 25.** Every live run finished without an error.
* **Unsafe input refused: 10 / 10.** Unsafe input, for example protein-level variants such as `KRAS G12D`, was not
  analysed; the assistant asked for hg38 genomic coordinates instead of guessing.

## Quality

* **Reproducibility: 100%.** Repeating a prompt for similar mutations 5 times produced the same results each time.
  This shows the calls are consistent. It does **not** show that they are correct.
* **Not yet measured:** held-out accuracy per target, accuracy when Candor commits versus how often it abstains, and
  calibration of the confidence scores. No accuracy figure is claimed for this prototype.

## Method and caveats

* Averages over 5 runs per example is a small sample. Gemini and network latency vary from run to run.
* Timings include the network between the client and the hosted demo.
* The operating limits below are deployment **settings**, not measurements.

| Operating limit (default) | Value |
|---|---|
| Rows per pasted table | 500 |
| Typed variants per request | 200 |
| Turns per session | 30 |
| Requests per client IP per hour | 60 |
| Parallel analyses | 2 |

## Reproduce it

```bash
pip install requests
python scripts/measure_candor.py --url "https://<your-service>.run.app/?k=<ACCESS_CODE>" --runs 5
pip install -r requirements-dev.txt && pytest -q
```

The script drives the app's public API with the three built-in examples (fresh session per run) and writes
`performance_results.json` and a `PERFORMANCE.md` report to the current directory. The access code is never written
to its output files. It needs access to a running Candor instance, so use the hosted demo or your own deployment.
