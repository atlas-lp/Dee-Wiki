"""
webapp/api/wiki.py — WIKI_LLM agent (navigation + maintenance) and wiki update pipeline.

Contains: WIKI_LLM prompts/tools, run_wiki_llm, tool_read_page,
GitHub push helper, and the async wiki update pipeline (_do_wiki_update,
_write_wiki_page, update_wiki_async).
"""

import os
import re
import json
import base64
import threading
import requests
from datetime import date
from pathlib import Path

from kb import (
    PROJECT_ROOT,
    DATA_DIR,
    WIKI_DIR,
    VAULT,
    INDEX_MD_PATH,
    LOG_MD_PATH,
    _GITHUB_BASE,
    _WIKI_FAISS_CACHE,
    _WIKI_FAISS_SLUGS,
    WikiSearchIndex,
    KnowledgeBase,
    _load_graph,
)
from rag import _extract_json
from llm_client import LLMClient

# ---------------------------------------------------------------------------
# WIKI_LLM tool definition
# ---------------------------------------------------------------------------

_WIKI_LLM_TOOLS = [
    {
        "name": "read_page",
        "description": (
            "Read a single wiki page by slug. Returns its full content and a related_pages list "
            "of slugs this page links to. "
            "IMPORTANT: the slug you pass MUST come from the related_pages list of a previous "
            "read_page result, or from a [[slug|Title]] wikilink visible in page content — "
            "use the part before the | character. Never invent or guess slugs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Exact slug from related_pages or a [[slug|Title]] wikilink. Bare kebab-case, no path, no .md.",
                }
            },
            "required": ["slug"],
        },
    }
]

# ---------------------------------------------------------------------------
# WIKI_LLM system prompts
# ---------------------------------------------------------------------------

_WIKI_LLM_NAVIGATION_SYSTEM = """\
You are the wiki navigation agent. Output JSON only. Never speak to the user.

You have the top search results for the user query. Each page lists its typed relationship links to other pages.

## Decision rule

Before declaring sufficient=true, ask: "Can I quote a specific sentence from these pages that directly answers the query?"
- YES → output JSON with sufficient=true and that exact quote in evidence.
- NO → call read_page on the most promising relationship slug. The search results are a starting point, not the answer. Relationships connect to deeper, more specific pages — follow them.

Only call read_page if a relationship is directly relevant to the query — not just topically adjacent. Stop as soon as you have a quotable answer or have exhausted genuinely relevant leads.

## Valid slugs for read_page

Only call read_page with a slug that comes from one of these sources — never invent or guess:
1. The `related_pages` list returned by a previous read_page call (each entry has a `slug` field).
2. A [[slug|Title]] wikilink visible in page content — use the part before the | character.

If the slug you want to follow is not in either of these sources, do not call read_page.

## Sufficiency test

sufficient=true ONLY if you can quote or closely paraphrase a specific passage that answers the query.
Not sufficient: keyword overlap, topical adjacency, partial answers, or "the page mentions this topic."

When uncertain → sufficient=false. MAIN_LLM has RAG fallback; a false positive (declaring sufficient when you're not) is worse than a false negative.

## Output (JSON only)

{
  "sufficient": true | false,
  "selected_slugs": ["slug-one", "slug-two"],
  "evidence": "<if sufficient=true: quoted passage + slug, e.g. 'microequity: contracts are self-enforcing because...'>",
  "note": "<if sufficient=false: one sentence on what is missing>"
}

- selected_slugs: every page you read, most relevant first. Include even when sufficient=false.
- Slugs: bare kebab-case only — no path, no .md.
"""

_WIKI_LLM_MAINTENANCE_SYSTEM = """\
You are the wiki maintenance agent. Output raw JSON only — no markdown, no explanation.

Related pages are in <related_pages>. If one already covers this insight, set "action":"update" and use its slug. Otherwise "action":"create" with a new kebab-case slug.

Output this exact structure:
{"action":"create","slug":"kebab-case-slug","title":"Human Readable Title","type":"synthesized","tags":["tag1"],"aliases":[],"relationships":[{"target":"related-slug","type":"extends"}],"body":"Full markdown body. No frontmatter."}

Rules:
- slug: lowercase kebab-case, no spaces, no .md
- body: escape all quotes and newlines properly for JSON (\\n not literal newlines inside the string)
- relationships: only slugs that exist in <related_pages>
- Output nothing outside the JSON object\
"""

# ---------------------------------------------------------------------------
# tool_read_page — implementation of the read_page tool
# ---------------------------------------------------------------------------

def tool_read_page(slug: str, page_by_slug: dict, graph: dict) -> dict:
    """
    Read one wiki page by slug. Returns its full content and outgoing relationship targets.
    Called by run_wiki_llm when iterating relationship chains.
    """
    page = page_by_slug.get(slug)
    if not page:
        return {"error": f"Page '{slug}' not found in wiki"}
    related = [
        {"slug": e["to"], "relationship": e["type"]}
        for e in graph.get("edges", []) if e["from"] == slug
    ]
    return {
        "slug":          slug,
        "title":         page["title"],
        "content":       page["content"],
        "related_pages": related,
    }

# ---------------------------------------------------------------------------
# run_wiki_llm — WIKI_LLM navigation pass
# ---------------------------------------------------------------------------

def run_wiki_llm(
    user_query:   str,
    wiki_search:  WikiSearchIndex,
    page_by_slug: dict,
    graph:        dict,
    client:       LLMClient,
) -> dict:
    """
    Run WIKI_LLM navigation pass. Returns:
      {"sufficient": bool, "selected_slugs": [...], "note": str | "evidence": str}

    Hybrid search surfaces top-5 pages. WIKI_LLM reads them and may call
    read_page iteratively to follow relationship chains (max _MAX_HOPS hops).
    """
    top_pages = wiki_search.search(user_query, top_k=5)
    if not top_pages:
        return {"sufficient": False, "selected_slugs": [], "note": "No wiki pages found"}
    print(f"[WikiLLM] search → {[p['slug'] for p in top_pages]}")

    context_text = ""
    for p in top_pages:
        context_text += f"\n{'='*50}\n[{p['slug']}] {p['title']}\n{'='*50}\n{p['content']}\n"

    messages = [{"role": "user", "content": (
        f"User query: {user_query}\n\n"
        f"Top search results:\n{context_text}"
    )}]

    accumulated_slugs = [p["slug"] for p in top_pages]
    _MAX_HOPS = 4

    for _ in range(_MAX_HOPS + 1):
        response = client.complete_with_tools(
            system=_WIKI_LLM_NAVIGATION_SYSTEM,
            messages=messages,
            tools=_WIKI_LLM_TOOLS,
            max_tokens=3500,
            thinking={"type": "enabled", "budget_tokens": 2000},
        )
        if response.stop_reason != "tool_use":
            break

        tool_calls_to_run = []
        results = []
        for tc in response.tool_calls:
            if tc.name == "read_page":
                slug = tc.input.get("slug", "")
                slug = slug.split("/")[-1].removesuffix(".md").split("|")[0].strip()
                print(f"[WikiLLM] read_page({slug!r})")
                result = tool_read_page(slug, page_by_slug, graph)
                if "error" not in result and slug not in accumulated_slugs:
                    accumulated_slugs.append(slug)
                tool_calls_to_run.append(tc)
                results.append(json.dumps(result, ensure_ascii=False))
        client.append_assistant_turn(messages, response)
        client.append_tool_results(messages, tool_calls_to_run, results)

    final_text = response.text
    result = _extract_json(final_text)

    if not result or "selected_slugs" not in result:
        result = {
            "sufficient":     False,
            "selected_slugs": accumulated_slugs,
            "note":           "WikiLLM parse failed — using search results",
        }
    return result

# ---------------------------------------------------------------------------
# GitHub push helper
# ---------------------------------------------------------------------------

def _push_batch_to_github(files: dict, commit_msg: str):
    """
    Push multiple files to GitHub in ONE commit using the Git Trees API.
    files: {repo_relative_path: str_content_or_bytes}
    Silently skips if GITHUB_TOKEN or GITHUB_REPO is unset.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO",  "")
    if not token or not repo:
        return

    api     = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
    }

    try:
        r = requests.get(f"{api}/git/refs/heads/main", headers=headers, timeout=10)
        r.raise_for_status()
        head_sha = r.json()["object"]["sha"]

        r = requests.get(f"{api}/git/commits/{head_sha}", headers=headers, timeout=10)
        r.raise_for_status()
        base_tree_sha = r.json()["tree"]["sha"]

        tree_entries = []
        for path, content in files.items():
            if isinstance(content, bytes):
                blob_payload = {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
            else:
                blob_payload = {"content": content, "encoding": "utf-8"}
            r = requests.post(f"{api}/git/blobs", headers=headers, json=blob_payload, timeout=30)
            r.raise_for_status()
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": r.json()["sha"]})

        r = requests.post(f"{api}/git/trees", headers=headers,
                          json={"base_tree": base_tree_sha, "tree": tree_entries}, timeout=30)
        r.raise_for_status()
        new_tree_sha = r.json()["sha"]

        r = requests.post(f"{api}/git/commits", headers=headers,
                          json={"message": commit_msg, "tree": new_tree_sha, "parents": [head_sha]},
                          timeout=30)
        r.raise_for_status()
        new_commit_sha = r.json()["sha"]

        r = requests.patch(f"{api}/git/refs/heads/main", headers=headers,
                           json={"sha": new_commit_sha}, timeout=10)
        r.raise_for_status()
        print(f"[GitHub] Pushed {len(files)} files in 1 commit: {commit_msg[:70]}")

    except Exception as e:
        print(f"[GitHub] Batch push failed: {e}")

# ---------------------------------------------------------------------------
# Wiki update pipeline — async fire-and-forget
# ---------------------------------------------------------------------------

def update_wiki_async(
    synthesis:      str,
    sources:        dict,
    original_query: str,
    client:         LLMClient,
    kb:             KnowledgeBase,
):
    """Trigger wiki update in a background thread. Never blocks the caller."""
    def _run():
        try:
            _do_wiki_update(synthesis, sources, original_query, client, kb)
        except Exception as e:
            print(f"[WikiUpdate] Error: {e}")

    threading.Thread(target=_run, daemon=True).start()


def _do_wiki_update(
    synthesis:      str,
    sources:        dict,
    original_query: str,
    client:         LLMClient,
    kb:             KnowledgeBase,
):
    """Second WIKI_LLM call (maintenance role). Generates a wiki page from synthesis."""
    print("[WikiUpdate] Generating new wiki page...")

    with kb._lock:
        wiki_search = kb.wiki_search

    related = wiki_search.search(synthesis, top_k=10)
    related_text = ""
    for p in related:
        related_text += f"\n{'='*50}\n[{p['slug']}] {p['title']}\n{'='*50}\n{p['content']}\n"

    system = f"<related_pages>\n{related_text}\n</related_pages>\n\n{_WIKI_LLM_MAINTENANCE_SYSTEM}"

    response = client.complete_with_tools(
        system=system,
        messages=[{
            "role":    "user",
            "content": (
                f"Original query: {original_query}\n\n"
                f"Synthesis to preserve:\n{synthesis}\n\n"
                f"Sources used — wiki: {sources.get('wiki', [])}, "
                f"rag: {sources.get('rag', [])}"
            ),
        }],
        tools=[],
        max_tokens=4096,
    )

    text      = response.text
    page_data = _extract_json(text)

    if not page_data or "slug" not in page_data:
        print("[WikiUpdate] Could not parse page data. Raw response:")
        print(text[:500])
        return

    _write_wiki_page(page_data, kb, original_query)
    print(f"[WikiUpdate] Done — {page_data['slug']}")


def _write_wiki_page(page_data: dict, kb: KnowledgeBase, original_query: str = ""):
    """
    Write a wiki page to disk, patch _graph.json, update in-memory KB,
    update index.md and log.md, then push all changed files to GitHub.
    """
    slug      = page_data.get("slug", "synthesized-page")
    title     = page_data.get("title", slug)
    page_type = page_data.get("type", "synthesized")
    tags      = page_data.get("tags", [])
    aliases   = page_data.get("aliases", [])
    rels      = page_data.get("relationships", [])
    body      = page_data.get("body", "")

    rel_yaml = ""
    if rels:
        rel_yaml = "relationships:\n"
        for r in rels:
            rel_yaml += f"  - target: {r.get('target', '')}\n    type: {r.get('type', 'related_to')}\n"

    frontmatter = (
        f"---\n"
        f"type: {page_type}\n"
        f"aliases: {json.dumps(aliases)}\n"
        f"tags: {json.dumps(tags)}\n"
        f"{rel_yaml}"
        f"---\n\n"
    )
    content = frontmatter + f"# {title}\n\n" + body

    today_str = date.today().isoformat()
    out_dir   = WIKI_DIR / "synthesized"
    out_path  = out_dir / f"{slug}.md"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(out_path)
        print(f"[WikiUpdate] Wrote {out_path.relative_to(PROJECT_ROOT)}")
    except OSError as e:
        print(f"[WikiUpdate] Disk write skipped (read-only fs — will push to GitHub): {e}")

    graph = _load_graph()
    graph["nodes"][slug] = {
        "type":    page_type,
        "title":   title,
        "aliases": aliases,
        "tags":    tags,
        "path":    f"wiki/synthesized/{slug}.md",
    }
    # Drop stale outgoing edges from this slug before adding fresh ones.
    graph["edges"] = [e for e in graph["edges"] if e["from"] != slug]
    for r in rels:
        graph["edges"].append({
            "from": slug,
            "to":   r.get("target", ""),
            "type": r.get("type", "related_to"),
        })
    graph_json = json.dumps(graph, indent=2, ensure_ascii=False)
    try:
        (DATA_DIR / "_graph.json").write_text(graph_json, encoding="utf-8")
        print("[WikiUpdate] Graph updated: webapp/data/_graph.json")
    except OSError as e:
        print(f"[WikiUpdate] Graph write failed: {e}")

    new_page_entry = {
        "slug":    slug,  "title":   title,   "aliases": aliases,
        "tags":    tags,  "content": content, "path":    str(out_path),
        "type":    page_type,
    }
    with kb._lock:
        existing = [i for i, p in enumerate(kb.wiki_pages) if p["slug"] == slug]
        if existing:
            kb.wiki_pages[existing[0]] = new_page_entry
        else:
            kb.wiki_pages.append(new_page_entry)
        kb.graph = graph

    kb.wiki_search.add_or_update(new_page_entry)

    # Update index.md — upsert so the same slug is never listed twice.
    new_index_content = None
    try:
        if INDEX_MD_PATH.exists():
            index_text  = INDEX_MD_PATH.read_text(encoding="utf-8")
            link_prefix = f"[[synthesized/{slug}|"
            if link_prefix in index_text:
                index_text = re.sub(
                    rf"\[\[synthesized/{re.escape(slug)}\|[^\]]*\]\][^\n]*",
                    f"[[synthesized/{slug}|{title}]] — synthesized from query",
                    index_text,
                )
            else:
                index_text += f"\n- [[synthesized/{slug}|{title}]] — synthesized from query\n"
            INDEX_MD_PATH.write_text(index_text, encoding="utf-8")
            new_index_content = index_text
    except OSError:
        pass

    log_entry = (
        f"\n## [{today_str}] synthesize | {slug}\n"
        f"- Pages created: {out_path.name}\n"
        f"- From query: {original_query}\n"
    )
    try:
        with open(LOG_MD_PATH, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except OSError:
        pass

    vault_repo_path = str(VAULT.relative_to(PROJECT_ROOT)).replace("\\", "/")
    files_to_push = {
        f"{vault_repo_path}/wiki/synthesized/{slug}.md": content,
        f"{_GITHUB_BASE}/data/_graph.json":              graph_json,
    }
    if new_index_content:
        files_to_push[f"{vault_repo_path}/wiki/index.md"] = new_index_content
    if _WIKI_FAISS_CACHE.exists():
        try:
            files_to_push[f"{_GITHUB_BASE}/data/wiki_search.faiss"] = _WIKI_FAISS_CACHE.read_bytes()
        except Exception as e:
            print(f"[GitHub] Skipping FAISS binary: {e}")
    if _WIKI_FAISS_SLUGS.exists():
        try:
            files_to_push[f"{_GITHUB_BASE}/data/wiki_search_slugs.json"] = _WIKI_FAISS_SLUGS.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[GitHub] Skipping FAISS slugs: {e}")

    _push_batch_to_github(
        files=files_to_push,
        commit_msg=f"wiki: synthesize {slug} from query {today_str}",
    )
