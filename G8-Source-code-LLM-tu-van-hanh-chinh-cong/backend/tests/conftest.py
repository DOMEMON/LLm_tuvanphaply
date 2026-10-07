"""Offline tests against the bundled, versioned administrative corpus."""
from pathlib import Path
import pytest

@pytest.fixture(scope='session')
def data():
    from app.rag.g3.validator import load_serving
    return load_serving(Path(__file__).resolve().parents[2] / 'data/administrative',
                        'company-tthc-9c38dde8-v1-adjudicated-fees-v2', 42)

@pytest.fixture(autouse=True)
def deterministic_evidence(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'g6_candidate_audit_enabled', False)
    monkeypatch.setattr(get_settings(), 'g5_realtime_mcp_enabled', False)
