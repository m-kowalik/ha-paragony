"""Ładuje moduły integracji bez Home Assistanta (bez wykonywania __init__.py pakietu)."""
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "paragony"

pkg = types.ModuleType("paragony")
pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("paragony", pkg)


@pytest.fixture
def zabka_fixture() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "zabka_receipt.json").read_text())
