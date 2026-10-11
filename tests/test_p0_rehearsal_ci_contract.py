"""Require that the P0 integration rehearsal retains ALL sibling-preservation suites."""
from pathlib import Path


def test_stacked_pr_preservation_workflow_keeps_all_review_contracts():
    text=(Path(__file__).resolve().parents[1]/".github/workflows/preservation.yml").read_text()
    required=["test_preservation.py","test_tarot_restoration.py","test_message_pipeline.py","test_tarot_quality.py","test_google_production_site.py","test_admin_boundary.py","test_provider_status_resilience.py","test_dependency_compatibility.py","test_integration_docs_contract.py"]
    run=next(line for line in text.splitlines() if "run: python -m pytest -q" in line)
    for item in required:
        assert "tests/"+item in run, item
