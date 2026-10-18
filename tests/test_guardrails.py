"""Deterministic input handling: what the app accepts, rejects and never guesses.
These tests need no network, no Gemini and no engine."""
from pathlib import Path

import pytest

import candor_tools as T

EXAMPLE = (Path(__file__).parent / "data" / "example_table.csv").read_text().strip().splitlines()


@pytest.fixture(autouse=True)
def fresh_context():
    token = T.use_ctx(T.Ctx())
    yield
    T.reset_ctx(token)


def test_example_table_is_captured_exactly():
    rest, info = T.register_pasted_table("Analyze this molecular profile.\n" + "\n".join(EXAMPLE))
    assert info["n_rows"] == len(EXAMPLE) == 13
    assert info["n_patients"] == 1 and info["dropped"] == 0
    assert "Analyze this molecular profile" in rest          # the free-text question is kept separately


def test_app_note_tells_the_model_not_to_retype_rows():
    text, info = T.prepare_message("Analyze\n" + "\n".join(EXAMPLE))
    assert info and "do not retype rows" in text


def test_plain_question_is_not_mistaken_for_a_table():
    _, info = T.register_pasted_table("What is Candor?")
    assert info is None


def test_typed_hotspots_become_validated_hg38_records():
    p = T.parse_variant_strings(["TP53 chr17:7675088 C>T", "KRAS chr12:25245350 C>T", "PIK3CA chr3:179218303 G>A"])
    assert len(p["records"]) == 3 and not p["needs_genomic_coordinates"]


def test_protein_level_input_is_never_converted_to_coordinates():
    p = T.parse_variant_strings(["KRAS G12D", "TP53 R175H"])
    assert p["records"] == []
    assert [x["reason"] for x in p["needs_genomic_coordinates"]] == ["protein_or_gene_level"] * 2


def test_ref_equals_alt_is_rejected():
    p = T.parse_variant_strings(["chr17 7675088 C C"])
    assert p["records"] == [] and p["errors"]


def test_invalid_chromosome_is_rejected():
    assert T.parse_variant_strings(["TP53 chr99:100 A>T"])["records"] == []


def test_refseq_accessions_are_rejected_because_genome_build_is_ambiguous():
    assert T.parse_variant_strings(["NC_000017.11:g.7675088C>T"])["records"] == []


def test_coordinate_notations_are_normalised():
    p = T.parse_variant_strings(["TP53 chr17:7675088 c>t", "17:7675088:C:T", "chr17:g.7675088C>T"])
    assert len(p["records"]) == 3
    assert {r["Chromosome"] for r in p["records"]} == {"chr17"}


def test_typed_variant_cap_is_enforced():
    items = [f"TP53 chr17:{7675000 + i} C>T" for i in range(T.MAX_VARIANTS + 5)]
    assert len(T.parse_variant_strings(items)["records"]) == T.MAX_VARIANTS


def test_table_row_cap_is_enforced_and_patients_are_counted():
    big = "\n".join(f"GENE{i},chr1,{1000 + i}-{1000 + i},C>T,P{i % 3}" for i in range(T.MAX_TABLE_ROWS + 50))
    _, info = T.register_pasted_table(big)
    assert info["n_rows"] == T.MAX_TABLE_ROWS and info["dropped"] == 50 and info["n_patients"] == 3


def test_errors_do_not_leak_the_engine_url_or_key(monkeypatch):
    monkeypatch.setattr(T, "CANDOR_ENGINE_URL", "https://engine.example.run.app")
    monkeypatch.setattr(T, "CANDOR_ENGINE_KEY", "s3cr3t")
    cleaned = T._scrub("failed at https://engine.example.run.app using s3cr3t")
    assert "engine.example" not in cleaned and "s3cr3t" not in cleaned


def test_missing_table_asks_the_user_instead_of_guessing():
    out = T.analyze_pasted_mutation_table()
    assert out["status"] == "no_table"


def test_only_protein_level_variants_ask_for_coordinates():
    out = T.analyze_variants(["KRAS G12D"])
    assert out["status"] == "no_usable_variants"
