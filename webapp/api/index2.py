"""
webapp/api/index2.py — Flask app, singletons, and CLI entry point.

Architecture (see CLAUDE.md):
  WIKI_LLM / Sonnet (wiki agent): hybrid BM25+MiniLM search → top-5 pages as context,
    iterative read_page tool to follow relationship chains until sufficient
  → MAIN_LLM / Opus (answer agent): synthesis, optional rag_search tool call
  → structured JSON response → answer string to frontend + async wiki update

CLI usage (from project root):
    python webapp/api/index2.py                        # interactive REPL
    python webapp/api/index2.py --query "..."          # single query
    python webapp/api/index2.py --rebuild-graph        # rebuild _graph.json
    python webapp/api/index2.py --build-wiki-index     # build FAISS index → data/ (no API key needed)
    python webapp/api/index2.py --serve                # run Flask dev server
    python webapp/api/index2.py --model1 MODEL --model2 MODEL
"""

import os
import sys
import json
import base64
import argparse
from pathlib import Path

from flask import Flask, request, Response, jsonify, send_from_directory

# kb.py performs sys.path setup and dotenv loading on import.
from kb import (
    KnowledgeBase,
    WikiSearchIndex,
    _load_wiki_pages,
    DATA_DIR,
    PROJECT_ROOT,
    _WIKI_FAISS_CACHE,
    WIKI_LLM_MODEL,
    MAIN_LLM_MODEL,
    WIKI_LLM_PROVIDER,
    MAIN_LLM_PROVIDER,
)
from pipeline import query_streaming, query
from llm_client import LLMClient
from wiki import update_wiki_async
from pdf_utils import extract_and_process_pdf
# graph.save_graph is only needed for the --rebuild-graph CLI flag.
from graph import save_graph

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

STATIC_DIR    = str(Path(__file__).resolve().parent.parent)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"]  = ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def _options(path):
    return "", 204


# ---------------------------------------------------------------------------
# Singletons — initialised once per process
# ---------------------------------------------------------------------------

_KB:          KnowledgeBase = None
_WIKI_CLIENT: LLMClient     = None
_MAIN_CLIENT: LLMClient     = None


def _get_kb() -> KnowledgeBase:
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


def _make_client(provider: str, model: str) -> LLMClient:
    if provider == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
    else:
        key = os.environ.get("NEBIUS_API_KEY", "")
        if not key:
            raise RuntimeError("NEBIUS_API_KEY is not set")
    base_url = os.environ.get("NEBIUS_BASE_URL") if provider == "nebius" else None
    return LLMClient(provider=provider, model=model, api_key=key, base_url=base_url)


def _get_wiki_client() -> LLMClient:
    global _WIKI_CLIENT
    if _WIKI_CLIENT is None:
        _WIKI_CLIENT = _make_client(WIKI_LLM_PROVIDER, WIKI_LLM_MODEL)
    return _WIKI_CLIENT


def _get_main_client() -> LLMClient:
    global _MAIN_CLIENT
    if _MAIN_CLIENT is None:
        _MAIN_CLIENT = _make_client(MAIN_LLM_PROVIDER, MAIN_LLM_MODEL)
    return _MAIN_CLIENT


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/api/chat",     methods=["POST"])
@app.route("/api/chat-v2", methods=["POST"])
def chat():
    """Dual-LLM agentic chat — SSE stream of {"text": "..."} chunks."""
    try:
        wiki_client = _get_wiki_client()
        main_client = _get_main_client()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    data         = request.get_json(force=True)
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    pdf_b64     = data.get("pdf_base64", "")
    bloom_level = data.get("bloom_level") or None
    kb          = _get_kb()

    def generate():
        nonlocal user_message
        metadata = {}
        try:
            if pdf_b64:
                _pdf_reading_msg = json.dumps({"text": "*Reading your PDF…*\n\n"})
                yield f"data: {_pdf_reading_msg}\n\n"
                try:
                    pdf_text = extract_and_process_pdf(base64.b64decode(pdf_b64))
                    user_message += f"\n\n[Attached PDF]\n{pdf_text}"
                except Exception as e:
                    print(f"[PDF] Extraction failed: {e}")
                    _pdf_err_msg = json.dumps({"text": "*(PDF extraction failed — answering without it.)*\n\n"})
                    yield f"data: {_pdf_err_msg}\n\n"

            for event_type, data in query_streaming(user_message, kb, wiki_client, main_client, bloom_level=bloom_level):
                if event_type == "text":
                    yield f"data: {json.dumps({'text': data})}\n\n"
                elif event_type == "done":
                    # Capture the metadata when the stream finishes
                    metadata = data
            
            # Debug: always log the metadata so we can see what the LLM returned.
            print(f"[Server] Metadata received: should_wiki_update={metadata.get('should_wiki_update')} rag_chunks={len(metadata.get('rag_chunks', []))} new_synthesis={metadata.get('new_synthesis', '')[:80]!r}")
            # Always send rag_chunks + wiki proposal (if any) to the frontend.
            frontend_event = {"rag_chunks": metadata.get("rag_chunks", [])}
            if metadata.get("should_wiki_update"):
                print(f"[Server] Proposing wiki update to frontend for: {user_message[:50]}...")
                frontend_event["should_wiki_update"] = True
                frontend_event["new_synthesis"]      = metadata.get("new_synthesis", "")
                frontend_event["sources"]             = metadata.get("sources", {})
            yield f"data: {json.dumps(frontend_event)}\n\n"

        except Exception as exc:
            print(f"[chat] Error: {exc}")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        
        yield "data: [DONE]\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )

@app.route("/api/health", methods=["GET"])
def health():
    kb = _get_kb()
    with kb._lock:
        n_pages  = len(kb.wiki_pages)
        n_chunks = len(kb.chunks)
        n_nodes  = len(kb.graph.get("nodes", {}))
    github_ok = bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"))
    return jsonify({
        "status":      "ok",
        "model_llm1":  WIKI_LLM_MODEL,
        "model_llm2":  MAIN_LLM_MODEL,
        "wiki_pages":  n_pages,
        "rag_chunks":  n_chunks,
        "graph_nodes": n_nodes,
        "github_push": github_ok,
    })


@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    allowed = {".html", ".css", ".js", ".ico", ".png", ".svg", ".jpg",
               ".woff", ".woff2", ".ttf", ".map"}
    ext  = Path(path).suffix.lower()
    full = Path(STATIC_DIR) / path
    if ext in allowed and full.is_file():
        return send_from_directory(STATIC_DIR, path)
    return send_from_directory(STATIC_DIR, "index.html")





@app.route("/api/wiki/commit", methods=["POST"])
def commit_wiki():
    """Triggered by the frontend when a user approves a wiki update proposal."""
    try:
        wiki_client = _get_wiki_client()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    data = request.get_json(force=True)
    synthesis = data.get("synthesis", "").strip()
    sources = data.get("sources", {})
    original_query = data.get("original_query", "")
    user_comment = data.get("user_comment", "").strip()

    if not synthesis:
        return jsonify({"error": "No synthesis provided"}), 400

    # Append user comment and/or PDF content to synthesis so the maintenance LLM sees them.
    if user_comment:
        synthesis += f"\n\n**User Correction/Comment:** {user_comment}"

    pdf_b64 = data.get("pdf_base64", "")
    if pdf_b64:
        try:
            pdf_text = extract_and_process_pdf(base64.b64decode(pdf_b64))
            synthesis += f"\n\n[User-Attached PDF]\n{pdf_text}"
        except Exception as e:
            print(f"[PDF] Extraction failed: {e}")

    kb = _get_kb()

    print(f"[Server] User approved wiki update for query: {original_query[:50]}...")

    # update_wiki_async already spawns a daemon thread internally.
    update_wiki_async(
        synthesis=synthesis,
        sources=sources,
        original_query=original_query,
        client=wiki_client,
        kb=kb,
    )

    return jsonify({"status": "ok", "message": "Wiki update initiated"})



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global WIKI_LLM_MODEL, MAIN_LLM_MODEL
    parser = argparse.ArgumentParser(
        description="Dual-LLM agentic query pipeline for the knowledge base.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--query",           "-q", metavar="QUERY", help="Run a single query and exit")
    parser.add_argument("--rebuild-graph",   action="store_true",   help="Rebuild _graph.json from wiki pages and exit")
    parser.add_argument("--build-wiki-index", action="store_true",  help="Build wiki FAISS index → data/ then exit (no API key needed)")
    parser.add_argument("--model1", metavar="MODEL", help=f"WIKI LLM model override (default: {WIKI_LLM_MODEL})")
    parser.add_argument("--model2", metavar="MODEL", help=f"MAIN LLM model override (default: {MAIN_LLM_MODEL})")
    args = parser.parse_args()

    if args.rebuild_graph:
        graph = save_graph()
        print(f"Built graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
        print(f"Saved to: {DATA_DIR / '_graph.json'}")
        return

    if args.build_wiki_index:
        pages = _load_wiki_pages()
        if not pages:
            print("[Error] No wiki pages found — check WIKI_DIR")
            sys.exit(1)
        idx = WikiSearchIndex()
        idx.build(pages)
        print(f"[Done] {len(pages)} pages → {_WIKI_FAISS_CACHE}")
        return

    if args.model1:
        WIKI_LLM_MODEL = args.model1
    if args.model2:
        MAIN_LLM_MODEL = args.model2

    try:
        wiki_client = _make_client(WIKI_LLM_PROVIDER, WIKI_LLM_MODEL)
        main_client = _make_client(MAIN_LLM_PROVIDER, MAIN_LLM_MODEL)
    except RuntimeError as e:
        print(f"[Error] {e}")
        sys.exit(1)

    kb = KnowledgeBase()

    print(f"\nModels — WIKI LLM ({WIKI_LLM_PROVIDER}): {WIKI_LLM_MODEL}")
    print(f"         MAIN LLM ({MAIN_LLM_PROVIDER}): {MAIN_LLM_MODEL}")
    print(f"GitHub push: {'enabled' if os.environ.get('GITHUB_TOKEN') else 'disabled (GITHUB_TOKEN not set)'}")

    if args.query:
        result = query(args.query, kb, wiki_client, main_client)
        print(f"\n{'='*60}")
        print(result.get("answer", "[No answer returned]"))
        print(f"{'='*60}")
        if result.get("sources"):
            print(f"\nSources — wiki: {result['sources'].get('wiki', [])}")
            print(f"          rag:  {result['sources'].get('rag', [])}")
        return

    print("\nDual-LLM Knowledge Base REPL")
    print("Type your question and press Enter. Ctrl-C or 'exit' to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            break
        result = query(user_input, kb, wiki_client, main_client)
        print(f"\nAssistant:\n{result.get('answer', '[No answer returned]')}\n")


if __name__ == "__main__":
    import argparse as _argparse
    _p = _argparse.ArgumentParser(add_help=False)
    _p.add_argument("--serve", action="store_true")
    _p.add_argument("--host", default="127.0.0.1")
    _p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5001)))
    _known, _rest = _p.parse_known_args()

    if _known.serve or _known.host != "127.0.0.1" or _known.port != 5001:
        try:
            from dotenv import load_dotenv as _load_dotenv
            _load_dotenv(PROJECT_ROOT / ".env")
        except ImportError:
            pass
        print(f"[index2] Starting Flask server on http://{_known.host}:{_known.port}")
        print("[index2] Initializing knowledge base...")
        _get_kb()
        print("[index2] Knowledge base ready.")
        app.run(host=_known.host, port=_known.port, debug=False, use_reloader=False)
    else:
        sys.argv = [sys.argv[0]] + _rest
        main()

