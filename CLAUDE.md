# Prof. Bhagwan Chowdhry — Dual-LLM Wiki System

## Project Overview
A hybrid knowledge architecture for Prof. Bhagwan Chowdhry (Finance Professor, ISB & UCLA Anderson). Two LLMs collaborate: one maintains the wiki, one answers the user. BM25 over wiki pages (title + tags + aliases) for fast navigation; embedding similarity over RAG chunks for deep source lookup. Deployed as a Flask chatbot on Vercel / Cloud Run.

## LLM Roles

There are two LLMs with distinct, non-overlapping jobs.

### LLM 1 — Wiki Agent
- **Never talks to the user.**
- Navigates the wiki: given BM25 results and `index.md`, decides if context is sufficient or calls `graph_traverse`.
- Maintains the wiki: when `should_wiki_update: true` arrives from LLM 2, calls `update_wiki`.
- Tools: `graph_traverse(slug, hops, max_nodes)`, `update_wiki(structured page data)`

### LLM 2 — Answer Agent
- Talks to the user exclusively.
- Synthesises an answer from wiki context passed by LLM 1.
- Calls `rag_search` only when wiki context is insufficient.
- Always returns a structured JSON response (see schema below).
- Tool: `rag_search(query, top_k)`

---

## Query Pipeline

```
User query
  │
  ▼
BM25 search over wiki pages (title + tags + aliases) → top 2 pages (full content)
  │
  ▼
LLM 1 — wiki agent
  context: index.md (always) + top 2 pages + user query
  │
  ├─ SUFFICIENT (or no better page in index.md)
  │     └─► pass context to LLM 2
  │
  └─ NOT SUFFICIENT (index.md shows a better page BM25 missed)
        └─► graph_traverse(slug, hops=1, max_nodes=5)
              └─► neighbor pages (full content)
                    └─► pass all context to LLM 2
  │
  ▼
LLM 2 — answer agent
  context: wiki pages from LLM 1 + user query
  │
  ├─ SUFFICIENT → structured JSON response
  │
  └─ NOT SUFFICIENT → rag_search(query) [embedding similarity only]
        └─► RAG chunks → structured JSON response
  │
  ▼
answer → user (immediate)
  │
  └─ should_wiki_update: true → async update_wiki (fire-and-forget, never blocks user)
```

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.