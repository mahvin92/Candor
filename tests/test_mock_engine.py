"""The web app's engine client against the SYNTHETIC test engine, over real HTTP."""
import socket, threading, time, types
from pathlib import Path

import pytest

import candor_tools as T

EXAMPLE = (Path(__file__).parent / "data" / "example_table.csv").read_text().strip().splitlines()
CLASSES = {"distinct", "shared", "indeterminate", "outlier"}


@pytest.fixture(scope="module")
def engine():
    import uvicorn
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config("mock_engine.main:app", host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(autouse=True)
def wire_up(engine, monkeypatch):
    monkeypatch.setattr(T, "CANDOR_ENGINE_URL", engine)
    monkeypatch.setattr(T, "CANDOR_ENGINE_KEY", "local-dev-key")
    monkeypatch.setattr(T, "ENGINE_MODE", "mock")
    monkeypatch.setattr(T, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: time.sleep(0.4)))  # poll faster
    token = T.use_ctx(T.Ctx())
    yield
    T.reset_ctx(token)


def test_health_requires_the_key(monkeypatch):
    assert T.check_engine_health()["ok"] is True
    monkeypatch.setattr(T, "CANDOR_ENGINE_KEY", "wrong")
    assert T.check_engine_health()["ok"] is False


def test_pasted_table_round_trip_is_labelled_synthetic():
    T.register_pasted_table("\n".join(EXAMPLE))
    out = T.analyze_pasted_mutation_table()
    assert out["status"] == "ok" and out["n_rows_submitted"] == 13 and out["n_profiles"] == 1
    prof = out["profiles"][0]
    assert set(prof["targets"]) == {"CancerType", "Stage", "Sex"}
    for tg in prof["targets"].values():
        assert tg["classification"] in CLASSES
        assert tg["top1"].startswith("SYN-")                 # placeholder labels, never real cancer types
        assert abs(sum(d["score"] for d in tg["distribution"]) - 1) < 0.01
    assert "SYNTHETIC" in (prof["interpretation"] or "")


def test_typed_variants_round_trip():
    out = T.analyze_variants(["TP53 chr17:7675088 C>T", "KRAS chr12:25245350 C>T", "KRAS G12D"])
    assert out["status"] == "ok"
    assert out["not_analysed"]["needs_genomic_coordinates"][0]["input"] == "KRAS G12D"   # flagged, not guessed


def test_same_input_gives_same_synthetic_output():
    a = T.analyze_variants(["TP53 chr17:7675088 C>T"])["targets"]["CancerType"]["top1"]
    b = T.analyze_variants(["TP53 chr17:7675088 C>T"])["targets"]["CancerType"]["top1"]
    assert a == b
