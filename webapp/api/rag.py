"""
webapp/api/rag.py — RAG utilities: JSON extraction, Gemini embedding, FAISS search.

_extract_json is a shared utility consumed by wiki.py, main_agent.py, and the pipeline.
"""

import os
import re
import json

import numpy as np
import requests

from kb import EMBED_MODEL, QUERY_PREFIX


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """Extract first JSON object from text. Tries multiple strategies before giving up."""
    clean = re.sub(r"```[a-zA-Z]*\n?", "", text).replace("```", "").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", clean)
    if match:
        candidate = match.group()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for end in range(len(candidate) - 1, -1, -1):
            if candidate[end] == "}":
                for start in range(end):
                    if candidate[start] == "{":
                        try:
                            result = json.loads(candidate[start:end + 1])
                            if isinstance(result, dict):
                                return result
                        except json.JSONDecodeError:
                            continue

    return {}


# ---------------------------------------------------------------------------
# Gemini embedding
# ---------------------------------------------------------------------------

def _get_query_embedding(query: str):
    """Embed query text using Gemini. Returns np.ndarray or None."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{EMBED_MODEL}:embedContent?key={gemini_key}"
    )
    payload = {"content": {"parts": [{"text": QUERY_PREFIX + query}]}}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return np.array(resp.json()["embedding"]["values"], dtype=np.float32)
    except Exception as e:
        print(f"[Embed] Query embedding failed: {e}")
        return None


# ---------------------------------------------------------------------------
# RAG search
# ---------------------------------------------------------------------------

def do_rag_search(query: str, chunks: list, faiss_index, top_k: int = 5) -> list:
    """FAISS inner-product search over RAG chunks (vectors pre-normalised at index build time)."""
    if faiss_index is None or not chunks:
        return []
    query_emb = _get_query_embedding(query)
    if query_emb is None:
        return []
    q_norm = (query_emb / (np.linalg.norm(query_emb) + 1e-8)).astype(np.float32)
    scores_arr, idx_arr = faiss_index.search(q_norm.reshape(1, -1), top_k)
    return [
        {
            "source":  chunks[i].get("source", ""),
            "content": chunks[i]["content"],
            "score":   float(s),
        }
        for i, s in zip(idx_arr[0].tolist(), scores_arr[0].tolist())
        if 0 <= i < len(chunks)
    ]
