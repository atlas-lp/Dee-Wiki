"""
tests/conftest.py — shared sys.path setup, heavy-lib stubs, and fixtures.

Must run at collection time (module level) before any test module is imported.
"""

import sys
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Resolve source directories
# ---------------------------------------------------------------------------

REPO_ROOT   = Path(__file__).resolve().parent.parent
API_DIR     = REPO_ROOT / "webapp" / "api"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _p in [str(API_DIR), str(SCRIPTS_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Stub heavy optional libraries so scripts import cleanly without hardware
# ---------------------------------------------------------------------------

for _mod in [
    "torch",
    "transformers",
    "transformers.pipelines",
    "llama_cpp",
    "faiss",
    "fastembed",
    "fitz",
    "langchain_text_splitters",
]:
    sys.modules.setdefault(_mod, MagicMock())

# Also stub the sub-namespace that chunk_blooms_tagger imports:
sys.modules.setdefault("transformers.pipelines.text_classification", MagicMock())

# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

import pytest


@pytest.fixture()
def minimal_graph():
    """In-memory graph with nodes A, B, C and a simple edge chain."""
    return {
        "nodes": {
            "a": {"type": "concept", "title": "Node A", "aliases": [], "tags": []},
            "b": {"type": "concept", "title": "Node B", "aliases": [], "tags": []},
            "c": {"type": "concept", "title": "Node C", "aliases": [], "tags": []},
        },
        "edges": [
            {"from": "a", "to": "b", "type": "uses"},
            {"from": "b", "to": "c", "type": "related_to"},
            {"from": "a", "to": "c", "type": "depends_on"},
        ],
    }


@pytest.fixture()
def sample_chunks():
    """Three minimal RAG chunk dicts."""
    return [
        {
            "id": "src_chunk_0000",
            "source": "src.md",
            "chunk_index": 0,
            "content": "This is chunk zero content for testing purposes.",
            "bloom_highest_level": "Remember",
            "type": "rag",
        },
        {
            "id": "src_chunk_0001",
            "source": "src.md",
            "chunk_index": 1,
            "content": "This is chunk one content with more detail.",
            "bloom_highest_level": "Understand",
            "type": "rag",
        },
        {
            "id": "src_chunk_0002",
            "source": "src.md",
            "chunk_index": 2,
            "content": "This is chunk two content with analysis.",
            "bloom_highest_level": "Create",
            "type": "rag",
        },
    ]


@pytest.fixture()
def sample_wiki_page():
    """Minimal wiki page dict matching _load_wiki_pages() schema."""
    return {
        "slug":    "test-concept",
        "title":   "Test Concept",
        "aliases": ["TC", "Test"],
        "tags":    ["testing", "example"],
        "content": "---\ntype: concept\naliases: [TC, Test]\ntags: [testing, example]\n---\n\n# Test Concept\n\nThis is the body.\n\n## Relationships\n\n- **related_to**: [[other-slug|Other]]\n",
        "type":    "concept",
        "path":    "/fake/path/test-concept.md",
    }


@pytest.fixture()
def tmp_wiki_dir(tmp_path):
    """A temp dir with two valid .md wiki files."""
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    (wiki_dir / "alpha.md").write_text(
        "---\ntype: concept\naliases: [Alpha]\ntags: [finance]\nrelationships:\n  - target: beta\n    type: uses\n---\n\n# Alpha\n\nAlpha is the first concept.\n",
        encoding="utf-8",
    )
    (wiki_dir / "beta.md").write_text(
        "---\ntype: entity\naliases: [Beta]\ntags: [economics]\n---\n\n# Beta\n\nBeta is a related entity.\n",
        encoding="utf-8",
    )
    return wiki_dir


@pytest.fixture()
def mock_kb():
    """MagicMock KnowledgeBase with preset attributes."""
    kb = MagicMock()
    kb._lock = threading.Lock()
    kb.wiki_pages = []
    kb.chunks = []
    kb.faiss_index = None
    kb.graph = {"nodes": {}, "edges": []}
    kb.wiki_search = MagicMock()
    kb.wiki_search.pages = []
    kb.wiki_search.bm25 = MagicMock()
    kb.wiki_search.faiss_index = None
    return kb


@pytest.fixture()
def mock_llm_client():
    """MagicMock LLMClient whose complete_with_tools returns a basic NormalizedResponse."""
    from llm_client import NormalizedResponse

    client = MagicMock()
    client.complete_with_tools.return_value = NormalizedResponse(
        stop_reason="end_turn",
        tool_calls=[],
        text='{"sufficient": true, "selected_slugs": []}',
        raw=None,
    )
    return client
