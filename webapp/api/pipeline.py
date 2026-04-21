"""
webapp/api/pipeline.py — Query orchestration.

Wires WIKI_LLM and MAIN_LLM together into a full request pipeline:
_pipeline_setup → query_streaming.
"""

from kb import KnowledgeBase, WIKI_DIR
from wiki import run_wiki_llm
from main_agent import run_main_llm_streaming
from llm_client import LLMClient


def _pipeline_setup(
    user_query:  str,
    kb:          KnowledgeBase,
    wiki_client: LLMClient,
    main_client: LLMClient = None,
):
    """
    Snapshot KB state and run the WIKI_LLM navigation pass.
    Returns (selected_pages, wiki_result, chunks, faiss_index).
    """
    print(f"\n{'─'*60}")
    print(f"[Pipeline] {user_query!r}")
    print(f"{'─'*60}")

    with kb._lock:
        wiki_pages  = list(kb.wiki_pages)
        chunks      = list(kb.chunks)
        faiss_index = kb.faiss_index
        graph       = kb.graph
        wiki_search = kb.wiki_search

    print(f"[Diag] WIKI_DIR={WIKI_DIR} exists={WIKI_DIR.exists()}")
    print(f"[Diag] wiki_pages={len(wiki_pages)} rag_chunks={len(chunks)} graph_nodes={len(graph.get('nodes', {}))}")
    print(f"[Diag] wiki_search.pages={len(wiki_search.pages)} bm25={'ok' if wiki_search.bm25 else 'NONE'} faiss={'ok' if wiki_search.faiss_index else 'NONE'}")

    page_by_slug = {p["slug"]: p for p in wiki_pages}

    wiki_result = run_wiki_llm(
        user_query=user_query,
        wiki_search=wiki_search,
        page_by_slug=page_by_slug,
        graph=graph,
        client=wiki_client,
    )
    print(f"[WikiLLM] selected slugs: {wiki_result.get('selected_slugs')} | sufficient={wiki_result.get('sufficient')}")

    selected_pages = []
    for slug in wiki_result.get("selected_slugs", []):
        page = page_by_slug.get(slug)
        if page:
            selected_pages.append(page)
        else:
            print(f"[Pipeline] Warning: slug '{slug}' not found in KB — skipping")

    if not selected_pages:
        print("[Pipeline] No slugs resolved — no wiki context for MAIN_LLM")

    return selected_pages, wiki_result, chunks, faiss_index


def query_streaming(
    user_query:  str,
    kb:          KnowledgeBase,
    wiki_client: LLMClient,
    main_client: LLMClient,
):
    """
    Full dual-LLM query pipeline — streaming generator.

    Yields:
        ("text", str)   — answer chunks to stream to the frontend
        ("done", dict)  — final metadata after stream ends (internal)
    """
    selected_pages, wiki_result, chunks, faiss_index = _pipeline_setup(
        user_query, kb, wiki_client
    )
    sufficient = wiki_result.get("sufficient", False)
    metadata   = {}

    for event_type, data in run_main_llm_streaming(
        user_query=user_query,
        wiki_context=selected_pages,
        wiki_note=wiki_result.get("note", ""),
        sufficient=sufficient,
        chunks=chunks,
        faiss_index=faiss_index,
        client=main_client,
    ):
        if event_type == "text":
            yield ("text", data)
        elif event_type == "metadata":
            metadata = data

    print(f"[MainLLM] should_wiki_update={metadata.get('should_wiki_update')}")

    yield ("done", metadata)


def query(
    user_query:  str,
    kb:          KnowledgeBase,
    wiki_client: LLMClient,
    main_client: LLMClient,
) -> dict:
    """Blocking wrapper for the CLI REPL. Collects all streamed chunks."""
    answer_parts = []
    metadata     = {}
    for event_type, data in query_streaming(user_query, kb, wiki_client, main_client):
        if event_type == "text":
            answer_parts.append(data)
        elif event_type == "done":
            metadata = data
    return {
        "answer":             "".join(answer_parts),
        "sources":            metadata.get("sources", {"wiki": [], "rag": []}),
        "new_synthesis":      metadata.get("new_synthesis", ""),
        "should_wiki_update": metadata.get("should_wiki_update", False),
    }
