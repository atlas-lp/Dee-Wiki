"""
webapp/api/kb.py — Config, data loaders, wiki search index, KnowledgeBase.

Contains: path/env constants, disk loaders, hybrid BM25+MiniLM WikiSearchIndex,
and the thread-safe KnowledgeBase singleton used by the query pipeline.
"""

import os
import sys
import json
import threading
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Path setup — resolve project root and make graph.py importable
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_API_DIR     = Path(__file__).resolve().parent
sys.path.insert(0, str(_API_DIR))
sys.path.insert(1, str(PROJECT_ROOT / "scripts"))

# Load .env BEFORE importing graph so WIKI_VAULT_NAME is set when graph.py computes VAULT.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from graph import parse_frontmatter, strip_frontmatter, WIKI_DIR, VAULT  # noqa: E402

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

DATA_DIR   = Path(__file__).resolve().parent.parent / "data"
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_WIKI_FAISS_CACHE = DATA_DIR / "wiki_search.faiss"
_WIKI_FAISS_SLUGS = DATA_DIR / "wiki_search_slugs.json"
INDEX_MD_PATH = WIKI_DIR / "index.md"
LOG_MD_PATH   = WIKI_DIR / "log.md"

_GITHUB_BASE = os.environ.get("GITHUB_BASE_PATH", "webapp")

WIKI_LLM_MODEL    = os.environ.get("WIKI_LLM_MODEL",    "claude-sonnet-4-6")
MAIN_LLM_MODEL    = os.environ.get("MAIN_LLM_MODEL",    "claude-opus-4-6")
WIKI_LLM_PROVIDER = os.environ.get("WIKI_LLM_PROVIDER", "claude")
MAIN_LLM_PROVIDER = os.environ.get("MAIN_LLM_PROVIDER", "claude")

EMBED_MODEL   = "gemini-embedding-2-preview"  # 3072 dims, 2048-token (~7500 char) input limit
QUERY_PREFIX  = "Represent this query for retrieval: "
CHUNK_SIZE    = 6000   # chars — stays inside the 7500-char embedding input window
CHUNK_OVERLAP = 300

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_wiki_pages() -> list:
    """Load all wiki .md files from Vault/wiki/ into dicts."""
    pages = []
    if not WIKI_DIR.exists():
        print(f"[WikiLoad] Wiki directory not found: {WIKI_DIR}")
        return pages

    for md_file in sorted(WIKI_DIR.rglob("*.md")):
        if md_file.name.startswith("_") or md_file.stem in ("index", "log"):
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
            fm   = parse_frontmatter(content)
            body = strip_frontmatter(content)

            title = md_file.stem.replace("-", " ").title()
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line.lstrip("# ").strip()
                    break

            aliases = fm.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]

            pages.append({
                "slug":    md_file.stem,
                "title":   title,
                "aliases": aliases,
                "tags":    tags,
                "content": content,
                "path":    str(md_file),
                "type":    fm.get("type", "unknown"),
            })
        except Exception as e:
            print(f"[WikiLoad] Skipped {md_file.name}: {e}")

    print(f"[WikiLoad] Loaded {len(pages)} pages from {WIKI_DIR}")
    return pages


def _load_chunks() -> list:
    """Load RAG chunks from data/chunks.json."""
    p = DATA_DIR / "chunks.json"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [c for c in raw if isinstance(c, dict) and "content" in c]
    except Exception as e:
        print(f"[ChunkLoad] {e}")
        return []


def _load_graph() -> dict:
    """Load the knowledge graph from webapp/data/_graph.json."""
    graph_path = DATA_DIR / "_graph.json"
    if graph_path.exists():
        try:
            g = json.loads(graph_path.read_text(encoding="utf-8"))
            print(f"[Graph] Loaded from data/_graph.json ({len(g.get('nodes', {}))} nodes)")
            return g
        except Exception as e:
            print(f"[Graph] data/_graph.json unreadable ({e})")
    else:
        print("[Graph] data/_graph.json not found — run: python scripts/graph.py --build")
    return {"nodes": {}, "edges": []}


def _load_faiss_index():
    """Load FAISS index from data/chunks.faiss. Returns index or None."""
    p = DATA_DIR / "chunks.faiss"
    if not p.exists():
        return None
    try:
        import faiss
        idx = faiss.read_index(str(p))
        print(f"[FAISS] Loaded index: {idx.ntotal} vectors")
        return idx
    except ImportError:
        print("[FAISS] faiss not installed — using numpy fallback. pip install faiss-cpu")
        return None
    except Exception as e:
        print(f"[FAISS] Failed to load: {e}")
        return None


# ---------------------------------------------------------------------------
# Wiki search index — BM25 + MiniLM hybrid
# ---------------------------------------------------------------------------

_WIKI_MINILM_MODEL    = None
_WIKI_EMBED_MODEL_NAME = "BAAI/bge-base-en-v1.5"


def _get_wiki_embed_model():
    global _WIKI_MINILM_MODEL
    if _WIKI_MINILM_MODEL is None:
        from fastembed import TextEmbedding
        _WIKI_MINILM_MODEL = TextEmbedding(_WIKI_EMBED_MODEL_NAME, cache_dir=str(MODELS_DIR))
    return _WIKI_MINILM_MODEL


def _encode(model, texts: list, batch_size: int = 64, show_progress: bool = False) -> np.ndarray:
    """Encode texts with fastembed. Returns normalized float32 array (n, 384)."""
    return np.array(list(model.embed(texts, batch_size=batch_size)), dtype=np.float32)


def _build_wiki_search_text(page: dict) -> str:
    """Clean text for wiki search: strips frontmatter and Relationships section."""
    body = strip_frontmatter(page["content"])
    rel_idx = body.find("## Relationships")
    if rel_idx > 0:
        body = body[:rel_idx].strip()
    aliases = " ".join(a for a in page.get("aliases", []) if isinstance(a, str))
    tags    = " ".join(t for t in page.get("tags",    []) if isinstance(t, str))
    return f"{page['title']} {aliases} {tags} {body}".strip()


class WikiSearchIndex:
    """Hybrid BM25 + MiniLM semantic index over wiki pages."""

    def __init__(self):
        self.bm25        = None
        self.faiss_index = None
        self.pages: list = []
        self._embeddings: np.ndarray = None

    def build(self, wiki_pages: list):
        """Full rebuild — startup only. Loads from FAISS cache if slugs match."""
        if not wiki_pages:
            self.bm25 = None
            self.faiss_index = None
            self.pages = []
            self._embeddings = None
            return
        from rank_bm25 import BM25Okapi
        import faiss as _faiss

        texts      = [_build_wiki_search_text(p) for p in wiki_pages]
        slugs      = [p["slug"] for p in wiki_pages]
        model_name = _WIKI_EMBED_MODEL_NAME

        idx = embs = None
        if _WIKI_FAISS_CACHE.exists() and _WIKI_FAISS_SLUGS.exists():
            try:
                meta = json.loads(_WIKI_FAISS_SLUGS.read_text(encoding="utf-8"))
                if (
                    isinstance(meta, dict)
                    and meta.get("model") == model_name
                    and meta.get("slugs") == slugs
                ):
                    idx  = _faiss.read_index(str(_WIKI_FAISS_CACHE))
                    embs = idx.reconstruct_n(0, idx.ntotal).astype(np.float32)
                    print(f"[WikiSearch] Loaded FAISS cache ({len(slugs)} pages, {model_name})")
                else:
                    print("[WikiSearch] Cache model/slugs mismatch — re-encoding")
            except Exception as e:
                print(f"[WikiSearch] Cache load failed: {e} — re-encoding")

        if embs is None:
            print(f"[WikiSearch] Encoding {len(wiki_pages)} pages with {model_name}…")
            try:
                model = _get_wiki_embed_model()
                embs  = _encode(model, texts, batch_size=64)
                idx   = _faiss.IndexFlatIP(embs.shape[1])
                idx.add(embs)
                try:
                    _faiss.write_index(idx, str(_WIKI_FAISS_CACHE))
                    _WIKI_FAISS_SLUGS.write_text(
                        json.dumps({"model": model_name, "slugs": slugs}), encoding="utf-8"
                    )
                    print(f"[WikiSearch] Saved FAISS cache ({len(slugs)} pages)")
                except Exception as e:
                    print(f"[WikiSearch] Cache save skipped (read-only fs): {e}")
            except Exception as e:
                print(f"[WikiSearch] MiniLM encoding failed: {e} — BM25-only mode")
                idx = embs = None

        self.bm25        = BM25Okapi([t.lower().split() for t in texts])
        self.faiss_index = idx
        self._embeddings = embs
        self.pages       = list(wiki_pages)
        mode = "hybrid BM25+FAISS" if idx is not None else "BM25-only"
        print(f"[WikiSearch] Index ready: {len(wiki_pages)} pages ({mode})")

    def add_or_update(self, page: dict):
        """Incremental update after wiki synthesis — encodes ONE page only."""
        if not self.pages or self._embeddings is None:
            self.build([page])
            return
        from rank_bm25 import BM25Okapi
        import faiss as _faiss
        model   = _get_wiki_embed_model()
        new_emb = _encode(model, [_build_wiki_search_text(page)])
        existing = next((i for i, p in enumerate(self.pages) if p["slug"] == page["slug"]), None)
        if existing is not None:
            self.pages[existing]       = page
            self._embeddings[existing] = new_emb[0]
            idx = _faiss.IndexFlatIP(self._embeddings.shape[1])
            idx.add(self._embeddings)
            self.faiss_index = idx
        else:
            self.pages.append(page)
            self._embeddings = np.vstack([self._embeddings, new_emb])
            self.faiss_index.add(new_emb)
        texts     = [_build_wiki_search_text(p) for p in self.pages]
        self.bm25 = BM25Okapi([t.lower().split() for t in texts])
        if self.faiss_index is not None:
            try:
                _faiss.write_index(self.faiss_index, str(_WIKI_FAISS_CACHE))
                _WIKI_FAISS_SLUGS.write_text(
                    json.dumps({
                        "model": _WIKI_EMBED_MODEL_NAME,
                        "slugs": [p["slug"] for p in self.pages],
                    }), encoding="utf-8"
                )
            except Exception:
                pass
        print(f"[WikiSearch] Incremental update: {len(self.pages)} pages (1 encoded)")

    def search(self, query: str, top_k: int = 5) -> list:
        if not self.pages or self.bm25 is None:
            return []
        n           = len(self.pages)
        bm25_scores = np.array(self.bm25.get_scores(query.lower().split()), dtype=np.float32)
        bm25_max    = bm25_scores.max() or 1.0
        # --- RRF parameters ---
        k_rrf   = 60
        w_bm25  = 0.3
        w_sem   = 0.7

        # --- BM25 ranking ---
        bm25_rank = np.argsort(-bm25_scores)  # descending

        # Initialize RRF scores
        rrf_scores = np.zeros(n, dtype=np.float32)

        # Add BM25 contribution
        for rank, idx in enumerate(bm25_rank):
            rrf_scores[idx] += w_bm25 * (1.0 / (k_rrf + rank))


        # --- Semantic ranking ---
        if self.faiss_index is not None:
            try:
                model = _get_wiki_embed_model()
                q_emb = _encode(model, [query])

                sem_scores, sem_idx = self.faiss_index.search(q_emb, n)

                # sem_idx is already ranked by similarity
                for rank, idx in enumerate(sem_idx[0]):
                    if 0 <= idx < n:
                        rrf_scores[idx] += w_sem * (1.0 / (k_rrf + rank))

            except Exception as e:
                print(f"[WikiSearch] Semantic search failed: {e} — BM25-only")


        # --- Final ranking ---
        top_idx = np.argsort(rrf_scores)[::-1][:top_k]
        return [self.pages[i] for i in top_idx]


# ---------------------------------------------------------------------------
# KnowledgeBase — thread-safe in-memory state
# ---------------------------------------------------------------------------

class KnowledgeBase:
    """
    Holds all in-memory state for the dual-LLM pipeline.
    All mutation must hold _lock; queries snapshot state under the lock.
    """

    def __init__(self):
        self.wiki_pages: list          = []
        self.chunks:     list          = []
        self.faiss_index               = None
        self.graph:      dict          = {}
        self.wiki_search: WikiSearchIndex = WikiSearchIndex()
        self._lock = threading.Lock()
        self.reload()

    def reload(self):
        """Full reload from disk."""
        print("[KB] Loading knowledge base...")
        new_pages       = _load_wiki_pages()
        new_chunks      = _load_chunks()
        new_faiss       = _load_faiss_index()
        new_graph       = _load_graph()
        new_wiki_search = WikiSearchIndex()
        new_wiki_search.build(new_pages)
        with self._lock:
            self.wiki_pages  = new_pages
            self.chunks      = new_chunks
            self.faiss_index = new_faiss
            self.graph       = new_graph
            self.wiki_search = new_wiki_search
        rag_backend = "FAISS" if new_faiss else "none (run export_for_web.py)"
        print(
            f"[KB] Ready — {len(self.wiki_pages)} wiki pages, "
            f"{len(self.chunks)} RAG chunks [{rag_backend}], "
            f"{len(self.graph.get('nodes', {}))} graph nodes"
        )
