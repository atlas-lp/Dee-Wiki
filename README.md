# Dee — Dual-LLM Knowledge Chatbot

A hybrid knowledge system for **Prof. Deepa Mani** (ISB) combining an LLM-maintained wiki (synthesised insights) with vector RAG (large source documents). Deployed as a streaming chatbot: **frontend on Vercel, backend on Google Cloud Run**.

Adaptable to any professor or domain expert.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Two-Tier Knowledge System](#two-tier-knowledge-system)
3. [Agentic Pipeline](#agentic-pipeline)
4. [Knowledge Graph](#knowledge-graph)
5. [Bloom's Taxonomy Tagging](#blooms-taxonomy-tagging)
6. [Project Structure](#project-structure)
7. [Scripts Reference](#scripts-reference)
8. [Webapp API Reference](#webapp-api-reference)
9. [Ingest Workflow](#ingest-workflow)
10. [Bloom's Tagging Workflow](#blooms-tagging-workflow)
11. [Export for Web](#export-for-web)
12. [Deployment](#deployment)
13. [Local Development](#local-development)
14. [Environment Variables](#environment-variables)
15. [Tech Stack](#tech-stack)

---

## Architecture

```
Browser
  │
  ├─ GET /  →  Vercel (static): index.html, script.js, style.css
  │
  └─ POST /api/chat-v2  →  Cloud Run (Flask, port 8080)
         │
         ├─ 1. BM25 + MiniLM search → top wiki pages
         │
         ├─ WIKI_LLM (Claude Sonnet 4.6)
         │       Tools: read_page, graph_traverse
         │       → selects relevant wiki pages, flags if insufficient
         │
         └─ MAIN_LLM (Claude Opus 4.6)
                 Tool: rag_search
                 │
                 ├─ Answer  →  SSE stream  →  Browser
                 │
                 └─ should_wiki_update: true
                         → async update_wiki (never blocks user)
                         → push to GitHub
```

**Two outputs per query**: the answer streams live to the user; if a non-obvious connection or novel synthesis was found, the wiki is updated asynchronously and pushed to GitHub. The knowledge base compounds over time.

---

## Two-Tier Knowledge System

| Tier | Storage | Purpose |
|------|---------|---------|
| **Wiki** | `Vault/wiki/` — markdown + YAML | Synthesised insights, cross-referenced concepts, entity + persona pages |
| **RAG** | `data/chunks.json` + FAISS | Raw retrieval from books, papers, interviews |

All source files are chunked and embedded into the RAG database. Wiki pages (concepts, entities, stubs) are then generated separately from those chunks using LLM extraction scripts.

### Wiki Tiers (inside `Vault/wiki/`)

| Folder | Type | Count | Contents |
|--------|------|-------|---------|
| `concepts/` | concept | ~420 | Business, tech, innovation concepts extracted from sources |
| `entities/` | entity | ~200 | Companies, people, institutions |
| `persona/` | persona | 8 | Prof. Deepa Mani's intellectual identity, thinking patterns, rhetorical style |
| `stubs/` | stub | ~10 | Pointers to RAG-indexed large documents (books, long papers) |
| `synthesized/` | synthesized | growing | Novel connections created at query time by MAIN_LLM |

### Wiki Page Format

Every wiki page is a `.md` file with YAML frontmatter encoding its graph relationships:

```markdown
---
type: concept
aliases: [Digital Transformation, DT]
relationships:
  - target: platform-structures
    type: related_to
  - target: deepa-mani
    type: proposed_by
tags: [strategy, technology]
---

# Digital Transformation

Body text...

## Relationships

- **related_to**: [[platform-structures|Platform Structures]]
- **proposed_by**: [[deepa-mani|Deepa Mani]]

---
*Extracted from: Driving Digital Strategy*
```

### Stub Page Format

Created for every large document (books, long papers) that is RAG-chunked:

```markdown
---
type: stub
tags: [strategy, digital]
---

# Driving Digital Strategy

**Source**: `Vault/raw/books/Driving_Digital_Strategy___.md`
**Pages**: 312 | **Words**: ~95,000

## Abstract
3–5 sentence summary.

## Key Claims
- Claim 1
- Claim 2

## Relationships
- [[digital-transformation|Digital Transformation]]
```

### RAG Chunk Format

Each entry in `data/chunks.json`:

```json
{
  "id": "Driving_Digital_Strategy___chunk_0012",
  "source": "Vault/raw/books/Driving_Digital_Strategy___.md",
  "chunk_index": 12,
  "content": "Text of the chunk (~1200 words)...",
  "word_count": 1187,
  "type": "rag",
  "embedding": [0.00466, 0.02040, -0.02270, ...],
  "bloom_highest_level": "Analyze",
  "bloom_predicted_labels": ["Understand", "Analyze", "Evaluate"],
  "bloom_confidences": {
    "Remember": 0.05, "Understand": 0.22, "Apply": 0.18,
    "Analyze": 0.31, "Evaluate": 0.17, "Create": 0.07
  },
  "bloom_sentence_count": 8,
  "bloom_bucket": "analyze_bkt"
}
```

---

## Agentic Pipeline

### Query Flow (step by step)

```
1. User sends message (+ optional PDF + optional Bloom's level filter)
       ↓
2. Hybrid search: BM25 (30%) + MiniLM cosine (70%) over wiki pages
   → top-5 candidate wiki pages returned
       ↓
3. WIKI_LLM (Claude Sonnet 4.6) — navigation agent
   System prompt: index.md (master catalog) + top-5 pages + user query
   Tools available:
     • read_page(slug)         — load a specific wiki page by slug
     • graph_traverse(slug, hops, max_nodes) — BFS over typed edges in _graph.json
   Decides: sufficient context | need more pages via graph traversal
   Returns: { sufficient: bool, selected_slugs: [...] }
       ↓
4. MAIN_LLM (Claude Opus 4.6) — answer agent
   System prompt: selected wiki pages + user query
   Tool available:
     • rag_search(query, top_k) — cosine search over FAISS chunk index
                                  (filtered by Bloom's level if set)
   Escalation ladder (strict — stops as soon as answer is solid):
     1st: Answer from wiki pages only
     2nd: Call rag_search if wiki is insufficient, incorporate results
     3rd: General knowledge only if both above fall short
   Streams answer text via SSE
   Ends with structured metadata:
     { sources: {wiki, rag}, new_synthesis, should_wiki_update }
       ↓
5a. Answer streamed live to user (SSE)
5b. If should_wiki_update=true → async update_wiki (fire-and-forget)
     → writes synthesized/slug.md
     → patches _graph.json
     → rebuilds BM25 index
     → pushes to GitHub
```

### WIKI_LLM Tools

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `read_page` | `slug: str` | Load full wiki page content by slug |
| `graph_traverse` | `slug, hops=1, max_nodes=5` | BFS expansion over typed knowledge graph edges |

### MAIN_LLM Tool

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `rag_search` | `query: str, top_k: int = 5` | Embed query → cosine search over FAISS, optional Bloom's filter |

### Source Attribution (shown to user)

Every MAIN_LLM response ends with a source block:

```
**My Memory:** Microequity, Costly State Verification
**My Library:** Driving Digital Strategy, The Digital Matrix
**General Knowledge:** Didn't use general knowledge
```

---

## Knowledge Graph

The graph (`webapp/data/_graph.json`, also at `Vault/wiki/_graph.json`) encodes typed relationships between wiki pages:

```json
{
  "nodes": {
    "digital-transformation": {
      "type": "concept",
      "title": "Digital Transformation",
      "aliases": ["Digital Transformation", "DT"],
      "tags": ["strategy", "technology"],
      "path": "wiki/concepts/digital-transformation.md"
    }
  },
  "edges": [
    {
      "source": "digital-transformation",
      "target": "platform-structures",
      "type": "related_to"
    }
  ]
}
```

**Relationship types**: `proposed_by`, `discussed_in`, `related_to`, `includes`, `contrasts_with`, `part_of`, `applied_to`

**Usage**: WIKI_LLM calls `graph_traverse` to follow edges outward from a seed page, pulling in 1–2 hops of related pages to build richer context before MAIN_LLM answers.

**Rebuilding the graph**:

```bash
python scripts/graph.py --build       # rebuild _graph.json from all wiki .md files
python scripts/graph.py --stats       # show node/edge counts
python scripts/graph.py --traverse digital-transformation  # BFS from a node
```

---

## Bloom's Taxonomy Tagging

Every RAG chunk is tagged with Bloom's taxonomy levels so users can filter RAG search by cognitive depth.

**Levels** (lowest → highest): Remember → Understand → Apply → Analyze → Evaluate → Create

### How Tagging Works

```
Chunk text
    ↓
Phase 1 — Gemma 4 (GGUF via llama_cpp)
    Extracts learning goals: ["Understand X", "Apply Y to Z", ...]
    (Keeps BERT in-distribution — trained on short learning outcome sentences)
    ↓
Phase 2 — Fine-tuned BERT (checkpoint-600)
    Classifies each extracted goal → sigmoid scores for 6 labels
    ↓
Aggregation
    Sum sigmoid probs across all goals → normalize to proportions (sum = 1.0)
    bloom_highest_level  = argmax of proportions
    bloom_predicted_labels = labels where proportion ≥ threshold
    bloom_confidences    = { label: proportion, ... }
    ↓
Stored in chunks.json alongside the text
```

### Models

| Model | File | Purpose |
|-------|------|---------|
| Gemma 4 E4B Q4_K_M | `webapp/models/gemma-4-E4B-it-Q4_K_M.gguf` | Learning goal extraction |
| BERT (fine-tuned) | `webapp/models/checkpoint-600/` | Bloom's level classification |

**BERT training data**: EDM2022CLO — short course learning outcome sentences (multilabel, 6 classes).

### Runtime Filtering

When the user selects a Bloom's level in the UI, `rag_search` filters chunks to those whose `bloom_highest_level` matches (or exceeds) the selected level:

```python
# In rag.py
if bloom_level:
    candidates = [c for c in candidates
                  if BLOOMS_ORDER.get(c.get("bloom_highest_level"), 0)
                     >= BLOOMS_ORDER[bloom_level]]
```

---

## Project Structure

```
.
├── CLAUDE.md                          # LLM instructions for wiki maintenance
├── README.md                          # This file
├── requirements.txt                   # Local dev / pipeline dependencies
├── .env.example                       # Environment variable template
│
├── scripts/                           # Offline pipeline (run locally, not deployed)
│   ├── ingest.py                      # Orchestrate PDF/MD → chunks + embeddings
│   ├── chunker.py                     # PDF extraction (3-tier) + text chunking
│   ├── export_for_web.py              # Build FAISS indices → webapp/data/
│   ├── graph.py                       # Build _graph.json from wiki YAML frontmatter
│   ├── tag_blooms.py                  # Tag chunks with Bloom's taxonomy
│   ├── chunk_blooms_tagger.py         # BERT classifier + aggregation logic
│   ├── gemma_goal_extractor.py        # Gemma 4 learning goal extractor
│   ├── kaggle_tag_blooms.ipynb        # Notebook: run Bloom's tagging on Kaggle GPU
│   ├── auto_wiki_builder.py           # Auto-create wiki pages from structured data
│   ├── extract_entities.py            # Named entity extraction
│   ├── sync_wiki.py                   # Push/pull wiki to GitHub
│   ├── remove_chunks.py               # Deindex specific chunks
│   ├── remove_wiki_page.py            # Delete a wiki page + update graph
│   ├── download_models.py             # Pre-download fastembed ONNX model
│   ├── npy_to_faiss.py                # Convert numpy arrays to FAISS index
│   └── wiki_logger.py                 # Append-only wiki log helper
│
├── data/                              # Generated RAG data (local only, not deployed)
│   ├── chunks.json                    # RAG chunks with 3072-dim embeddings (~19 MB)
│   ├── chunks.json.bak                # Auto-backup before tagging runs
│   ├── ingested.json                  # Tracks which source files have been processed
│   └── extracted.json                 # PDF extraction metadata
│
├── webapp/                            # Deployed application
│   ├── Dockerfile                     # Cloud Run image (Python 3.11-slim)
│   ├── .dockerignore
│   ├── vercel.json                    # Vercel static routing
│   ├── requirements.txt               # Backend Python dependencies
│   ├── index.html                     # Frontend (served by Vercel)
│   ├── script.js                      # SSE streaming, markdown + KaTeX rendering
│   ├── style.css                      # Responsive UI, dark mode
│   │
│   ├── api/                           # Flask backend (Cloud Run)
│   │   ├── index2.py                  # Flask app, routes, startup, CLI
│   │   ├── pipeline.py                # Query orchestration (wiki → main LLM)
│   │   ├── kb.py                      # KnowledgeBase singleton, WikiSearchIndex
│   │   ├── llm_client.py              # Provider-agnostic LLM client (Claude / Nebius)
│   │   ├── wiki.py                    # WIKI_LLM agent (navigation, graph traversal)
│   │   ├── main_agent.py              # MAIN_LLM agent (answer synthesis, RAG)
│   │   ├── rag.py                     # Embedding, FAISS search, Bloom's filter
│   │   ├── graph.py                   # Knowledge graph queries
│   │   └── pdf_utils.py               # PDF upload handling (base64 → text)
│   │
│   ├── data/                          # Exported data (baked into Docker image)
│   │   ├── _graph.json                # Knowledge graph (nodes + typed edges)
│   │   ├── chunks.json                # RAG chunks (text only, no embeddings)
│   │   ├── chunks.faiss               # FAISS vector index over RAG chunks
│   │   ├── wiki_search.faiss          # FAISS index over wiki pages (MiniLM)
│   │   └── wiki_search_slugs.json     # Slug → FAISS row mapping
│   │
│   ├── models/                        # ML models (committed to repo)
│   │   ├── models--qdrant--bge-base-en-v1.5-onnx-q/   # fastembed ONNX
│   │   ├── gemma-4-E4B-it-Q4_K_M.gguf                 # Gemma 4 GGUF (local inference)
│   │   └── checkpoint-600/                             # Fine-tuned BERT classifier
│   │
│   └── Vault/                         # Knowledge vault (baked into Docker image)
│       ├── wiki/
│       │   ├── index.md               # Master catalog (always in WIKI_LLM prompt)
│       │   ├── log.md                 # Append-only chronological log
│       │   ├── _graph.json            # Knowledge graph source of truth
│       │   ├── concepts/              # ~420 concept pages
│       │   ├── entities/              # ~200 entity pages
│       │   ├── persona/               # 8 persona pages
│       │   ├── stubs/                 # ~10 stub pages (RAG pointers)
│       │   └── synthesized/           # Query-synthesized pages (auto-growing)
│       └── raw/                       # Source files (NOT in Docker image)
│           ├── books/                 # Book markdown files (large)
│           ├── research_papers/       # PDF papers
│           ├── research_papers_md/    # Extracted markdown from PDFs
│           └── profile/               # Digital profile markdown
│
└── tests/
    ├── run_tests.py
    └── test_comprehensive.py
```

---

## Scripts Reference

### `scripts/ingest.py` — Chunk and embed all source files into RAG

```bash
python scripts/ingest.py --scan                           # List new unprocessed files
python scripts/ingest.py --process Vault/raw/books/AI.md  # Process one file
python scripts/ingest.py --process-all                    # Process all new files
python scripts/ingest.py --search "digital transformation" # BM25 search over chunks
```

**What it does**:
1. Scans `Vault/raw/` for all `.pdf`, `.md`, `.txt` files not yet in `ingested.json`
2. Extracts text (PDFs via `chunker.py`'s 3-tier fallback, markdown read directly)
3. Splits into ~1,200-word chunks with 300-char overlap
4. Generates Gemini embeddings (3072-dim) via `batchEmbedContents` API
5. Merges new chunks into `data/chunks.json`
6. Updates `data/ingested.json` to track what has been processed

All files — regardless of size — are chunked into RAG. Wiki pages are generated from the chunks in a separate step.

**Inputs**: PDF or markdown files under `Vault/raw/`  
**Outputs**: `data/chunks.json` (with embeddings), `data/ingested.json`

---

### `scripts/chunker.py` — Text extraction and chunking library

Used by `ingest.py`. Not run directly.

**PDF extraction** (3-tier fallback):
1. **PyMuPDF** — direct text extraction (fast)
2. **Gemini page-as-image** — page → PNG → Gemini 2.5 Flash OCR
3. **Gemini direct PDF upload** — raw bytes → Gemini (files under 20 MB)

**Chunking**: `RecursiveCharacterTextSplitter` with `CHUNK_SIZE=6000` chars (~1,200 words), `CHUNK_OVERLAP=300` chars.

**Embedding**: `gemini-embedding-2-preview`, 3072 dimensions, batches of 100.

---

### `scripts/export_for_web.py` — Build FAISS indices for deployment

```bash
python scripts/export_for_web.py
```

**What it does**:
1. Loads `data/chunks.json` (full, with embeddings)
2. Deduplicates by SHA-256 hash of chunk content (only processes new chunks)
3. Builds a FAISS index (inner-product over L2-normalised embeddings)
4. Writes `webapp/data/chunks.json` (text-only, no embeddings — keeps Docker image small)
5. Writes `webapp/data/chunks.faiss`
6. Rebuilds `webapp/data/_graph.json` from wiki `.md` files
7. Rebuilds `webapp/data/wiki_search.faiss` + `wiki_search_slugs.json`

Run this after every ingest and after every wiki edit before deploying.

---

### `scripts/graph.py` — Build the knowledge graph

```bash
python scripts/graph.py --build                          # Rebuild _graph.json
python scripts/graph.py --stats                          # Node/edge counts
python scripts/graph.py --traverse digital-transformation # BFS from a node
```

Parses YAML frontmatter `relationships:` blocks from all wiki `.md` files and writes `webapp/data/_graph.json`.

---

### `scripts/tag_blooms.py` — Bloom's taxonomy chunk tagger

```bash
python scripts/tag_blooms.py                  # Tag untagged chunks (Gemma + BERT)
python scripts/tag_blooms.py --skip-gemma     # BERT only (sentence-split, faster)
python scripts/tag_blooms.py --reset          # Strip all bloom tags, re-tag from scratch
python scripts/tag_blooms.py --rethreshold    # Recompute labels from stored confidences
                                              # using current BLOOMS_TUNED_THRESHOLDS
                                              # (no model inference — instant)
```

**Flags**:

| Flag | Effect |
|------|--------|
| `--reset` | Strip all `bloom_*` fields, re-tag everything |
| `--rethreshold` | Change thresholds in `chunk_blooms_tagger.py`, then run this to instantly see distribution change — no model runs |
| `--skip-gemma` | Skip Gemma goal extraction; run BERT directly on sentence-split text (faster but less accurate) |

**Outputs**: Writes bloom fields back into `data/chunks.json` (backs up as `chunks.json.bak` first). Prints distribution tables on finish.

**After tagging**: run `python scripts/export_for_web.py` to push the bloom fields into `webapp/data/chunks.json`.

---

### `scripts/chunk_blooms_tagger.py` — BERT classifier library

Used by `tag_blooms.py`. Can also be run directly:

```bash
python scripts/chunk_blooms_tagger.py \
    --model webapp/models/checkpoint-600 \
    --text "Design a system that can analyze user behavior."

python scripts/chunk_blooms_tagger.py \
    --model webapp/models/checkpoint-600 \
    --input chunks.json \
    --output tagged_chunks.json
```

Key tuneable constant:

```python
BLOOMS_TUNED_THRESHOLDS = {
    "Remember": 0.10, "Understand": 0.10, "Apply": 0.10,
    "Analyze":  0.10, "Evaluate":  0.10, "Create": 0.10,
}
```

Edit thresholds here, then run `tag_blooms.py --rethreshold` to see the distribution change without re-running any model.

---

### `scripts/gemma_goal_extractor.py` — Gemma 4 learning goal extractor

```bash
python scripts/gemma_goal_extractor.py --text "Some chunk text..."
python scripts/gemma_goal_extractor.py --file path/to/chunk.txt
```

Downloads the Gemma 4 E4B Q4_K_M GGUF on first run (to `webapp/models/`). Subsequent runs load from local cache. `n_gpu_layers=-1` offloads all layers to GPU when available.

---

### `scripts/kaggle_tag_blooms.ipynb` — Kaggle GPU notebook for Bloom's tagging

Use this when local CPU inference is too slow. The notebook:

1. **Cell 1**: Detects CUDA version, installs pre-built `llama-cpp-python` GPU wheel
2. **Cell 2**: Paste contents of `gemma_goal_extractor.py`
3. **Cell 3**: Paste contents of `chunk_blooms_tagger.py`
4. **Cell 4**: Paste contents of `tag_blooms.py`
5. **Cell 5**: `sys.argv = ["tag_blooms.py", "--reset"]; main()`
6. **Cell 6**: Verify output count

**Kaggle datasets required** (add via "Add Data"):
- `bloom-chunks` → contains `chunks.json`
- `bloom-bert-classifier` → contains `checkpoint-600/` folder

**Accelerator**: GPU T4 x2

**Output**: `/kaggle/working/chunks_tagged.json` — download and replace `data/chunks.json` locally, then run `export_for_web.py`.

**Note when pasting script cells**: Kaggle notebooks don't define `__file__`. Add `__file__ = "/kaggle/working/script_name.py"` before each pasted script block, and remove any `if __name__ == "__main__":` blocks.

---

### `scripts/extract_entities.py` — Generate wiki pages from RAG chunks

```bash
python scripts/extract_entities.py --all            # Process all unprocessed chunks
python scripts/extract_entities.py --source "AI"    # Process chunks from one source
python scripts/extract_entities.py --list-sources   # Show available sources
```

Uses Gemini 2.5 Pro to analyse RAG chunks in batches and identify key concepts (theories, models, methods) and entities (people, institutions). Generates wiki `.md` pages with YAML frontmatter and typed relationships in `wiki/concepts/` and `wiki/entities/`.

Tracks processed chunks by SHA-256 hash in `data/extracted.json` — safe to re-run, only processes new chunks.

**Inputs**: `data/chunks.json`  
**Outputs**: `Vault/wiki/concepts/*.md`, `Vault/wiki/entities/*.md`, `data/extracted.json`

---

### `scripts/auto_wiki_builder.py` — Build stub pages and index

```bash
python scripts/auto_wiki_builder.py --stubs         # Generate stub pages for all ingested sources
python scripts/auto_wiki_builder.py --index         # Rebuild wiki/index.md master catalog
python scripts/auto_wiki_builder.py --all           # Run stubs → index in sequence
python scripts/auto_wiki_builder.py --all --force   # Regenerate even existing stubs
python scripts/auto_wiki_builder.py --all --model claude-sonnet-4-6
```

`--stubs`: For each source in `ingested.json`, generates a stub wiki page in `wiki/stubs/` summarising the document (title, abstract, key claims, relationships).

`--index`: Scans all wiki pages and regenerates `wiki/index.md` as a complete catalog — this file is always injected into the WIKI_LLM system prompt.

**Inputs**: `data/ingested.json`, `data/chunks.json`, existing wiki pages  
**Outputs**: `Vault/wiki/stubs/*.md`, `Vault/wiki/index.md`

---

### Utility scripts

| Script | Purpose |
|--------|---------|
| `wiki_logger.py` | Helper for appending structured entries to `log.md` |
| `sync_wiki.py` | Push/pull wiki pages to/from GitHub manually |
| `remove_wiki_page.py` | Delete a wiki page, clean cross-references, update graph |
| `remove_chunks.py` | Deindex specific chunks from `data/chunks.json` |
| `npy_to_faiss.py` | Convert a `.npy` embedding matrix to a FAISS index |
| `download_models.py` | Pre-cache the fastembed ONNX model (also used in Dockerfile) |

---

## Webapp API Reference

### `webapp/api/index2.py` — Flask entry point

```bash
python webapp/api/index2.py                    # Interactive REPL (local testing)
python webapp/api/index2.py --query "..."      # Single query, print answer
python webapp/api/index2.py --serve            # Dev server on localhost:5001
python webapp/api/index2.py --rebuild-graph    # Rebuild _graph.json
python webapp/api/index2.py --build-wiki-index # Rebuild wiki FAISS index
```

**Routes**:

| Route | Method | Body | Response |
|-------|--------|------|----------|
| `/api/chat-v2` | POST | `{ message, pdf_base64?, bloom_level? }` | SSE stream of `{ text }` chunks |

`bloom_level` accepts: `Remember`, `Understand`, `Apply`, `Analyze`, `Evaluate`, `Create`

### `webapp/api/kb.py` — KnowledgeBase singleton

Loads once at startup, held in memory for the lifetime of the instance:

1. Parse all wiki `.md` files → page dicts
2. Build BM25 index (title + aliases + tags per page)
3. Load `data/chunks.json` → chunk list
4. Load `data/chunks.faiss` → FAISS index
5. Load `data/_graph.json` → graph dict
6. Load `data/wiki_search.faiss` + slugs → WikiSearchIndex

**WikiSearchIndex**: Hybrid BM25 (30%) + MiniLM cosine (70%) search — returns top-5 wiki pages.

### `webapp/api/pipeline.py` — Query orchestration

Wires WIKI_LLM and MAIN_LLM together. `query_streaming()` is the main entry point — yields `("text", str)` for streaming and `("done", dict)` for metadata when complete.

### `webapp/api/llm_client.py` — Provider-agnostic LLM client

```python
client = LLMClient(provider="claude", model="claude-sonnet-4-6", api_key="...")
client = LLMClient(provider="nebius", model="...", api_key="...", base_url="https://...")
```

Normalises tool call handling across Anthropic and OpenAI-compatible APIs.

### `webapp/api/rag.py` — RAG search

`do_rag_search(query, chunks, faiss_index, top_k, bloom_level)`:
1. Embed query using Gemini `gemini-embedding-2-preview`
2. Search FAISS index (cosine over normalised vectors)
3. If `bloom_level` set: filter candidates by `bloom_highest_level`
4. Return top-k chunks with source info

---

## Ingest Workflow

Full workflow for adding new source material:

```bash
# 1. Add source files to Vault/raw/
cp new-book.md Vault/raw/books/
cp new-paper.pdf Vault/raw/research_papers/

# 2. Scan to see what will be processed
python scripts/ingest.py --scan

# 3. Chunk and embed all new files into the RAG database
python scripts/ingest.py --process-all
# or a single file:
python scripts/ingest.py --process Vault/raw/books/new-book.md

# 4. Tag all new chunks with Bloom's taxonomy levels
python scripts/tag_blooms.py           # skips already-tagged chunks

# 5. Extract wiki pages (concepts + entities) from the new chunks
python scripts/extract_entities.py --all

# 6. Generate stub pages for each source + rebuild index.md
python scripts/auto_wiki_builder.py --all

# 7. Export everything to webapp/data/ (FAISS indices, graph, chunks)
python scripts/export_for_web.py

# 8. Push to GitHub (triggers Cloud Run auto-redeploy)
git add .
git commit -m "ingest: new-book"
git push
```

---

## Bloom's Tagging Workflow

### First time (or after adding new chunks)

```bash
# Tag only untagged chunks (safe to run repeatedly — skips already-tagged ones)
python scripts/tag_blooms.py

# Then export so the web app picks up the tags
python scripts/export_for_web.py
```

### Full re-tag (after changing model or prompt)

```bash
python scripts/tag_blooms.py --reset
python scripts/export_for_web.py
```

### Experimenting with thresholds (no re-inference needed)

```python
# 1. Edit BLOOMS_TUNED_THRESHOLDS in scripts/chunk_blooms_tagger.py
BLOOMS_TUNED_THRESHOLDS = {
    "Remember": 0.10, "Understand": 0.25, ...
}
```

```bash
# 2. Instantly recompute bloom_predicted_labels from stored confidences
python scripts/tag_blooms.py --rethreshold

# 3. Export if happy with the distribution
python scripts/export_for_web.py
```

### Running on Kaggle (recommended for large chunk sets)

GPU T4 x2 reduces Gemma inference from ~2 min/chunk to ~3 sec/chunk.

1. Upload `chunks.json` to a Kaggle dataset named `bloom-chunks`
2. Upload `checkpoint-600/` folder to a Kaggle dataset named `bloom-bert-classifier`
3. Open `scripts/kaggle_tag_blooms.ipynb` in Kaggle, enable GPU T4 x2
4. Paste each script into its cell (see notebook instructions)
5. Run all cells — output is `/kaggle/working/chunks_tagged.json`
6. Download and replace `data/chunks.json`
7. Run `python scripts/export_for_web.py`

---

## Export for Web

Run after any of the following:

- Adding new chunks via `ingest.py`
- Tagging or re-tagging Bloom's levels
- Editing wiki pages
- Changing graph relationships

```bash
python scripts/export_for_web.py
```

This is always the **last step before committing** — it builds everything the Docker image needs.

---

## Deployment

### Backend — Google Cloud Run

1. Go to **Cloud Run → Create Service**
2. Choose **"Continuously deploy from a repository"** → connect `Dee-Wiki` repo
3. Set **Build type** → `Dockerfile`, **Dockerfile location** → `/webapp/Dockerfile`
4. Set **Region** (e.g. `asia-south1`), **Memory** → 2 GiB, enable **Allow unauthenticated invocations**
5. Add environment variables (see [Environment Variables](#environment-variables))
6. Deploy — every `git push` to `main` auto-rebuilds and redeploys

The Dockerfile:
- Copies `api/`, `data/`, and `Vault/wiki/` into the image (source files in `Vault/raw/` are excluded)
- Pre-caches the fastembed ONNX model at build time (avoids download on cold start)
- Exposes port 8080, runs `python api/index2.py`

### Frontend — Vercel (static)

1. Import repo at [vercel.com/new](https://vercel.com/new)
2. Set **Root Directory** to `webapp`
3. No environment variables needed on Vercel — all secrets stay in Cloud Run
4. After deploying the backend, update `window.BACKEND_URL` in `webapp/script.js` with your Cloud Run service URL

```javascript
// In script.js — auto-detects local vs deployed
const _h = window.location.hostname;
window.BACKEND_URL = (_h === 'localhost' || _h === '127.0.0.1')
  ? ''
  : 'https://your-service-region.run.app';
```

---

## Local Development

### Run Flask dev server

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in API keys

python webapp/api/index2.py --serve
# Open http://localhost:5001
```

### Interactive REPL (no browser needed)

```bash
python webapp/api/index2.py
# Type queries directly in the terminal
```

### Single query

```bash
python webapp/api/index2.py --query "What is microequity?"
```

### Docker (local)

```bash
cd webapp
docker build -t dee-backend .
docker run -p 8080:8080 \
  -e ANTHROPIC_API_KEY=... \
  -e GEMINI_API_KEY=... \
  -e GITHUB_TOKEN=... \
  dee-backend
# Open http://localhost:8080
```

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | Yes | Claude API (Sonnet 4.6 wiki + Opus 4.6 answer) |
| `GEMINI_API_KEY` | Yes | Gemini embeddings (`gemini-embedding-2-preview`) |
| `GITHUB_TOKEN` | Recommended | Push wiki updates back to GitHub automatically |
| `GITHUB_REPO` | Recommended | `owner/repo` — e.g. `pathwaysresearch/Dee-Wiki` |
| `ALLOWED_ORIGIN` | Recommended | Vercel frontend URL for CORS |
| `WIKI_LLM_MODEL` | Optional | Override wiki agent model (default: `claude-sonnet-4-6`) |
| `MAIN_LLM_MODEL` | Optional | Override answer agent model (default: `claude-opus-4-6`) |
| `WIKI_LLM_PROVIDER` | Optional | `claude` or `nebius` (default: `claude`) |
| `MAIN_LLM_PROVIDER` | Optional | `claude` or `nebius` (default: `claude`) |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Answer LLM** | Claude Opus 4.6 (answer synthesis, RAG tool use) |
| **Wiki LLM** | Claude Sonnet 4.6 (navigation, graph traversal) |
| **Goal extraction** | Gemma 4 E4B Q4_K_M via llama_cpp (GGUF, GPU offload) |
| **Bloom's classifier** | Fine-tuned BERT (checkpoint-600, multilabel, 6 classes) |
| **RAG embeddings** | `gemini-embedding-2-preview` (3072-dim) |
| **Wiki search** | Hybrid BM25 (30%) + MiniLM cosine (70%) via fastembed ONNX |
| **Vector index** | FAISS (inner-product over L2-normalised vectors) |
| **Backend** | Flask on Google Cloud Run (Python 3.11, 2 GB RAM) |
| **Frontend** | Vercel static — vanilla HTML/CSS/JS, marked.js, KaTeX |
| **Wiki storage** | GitHub (source of truth) — baked into Docker image at deploy |
| **Knowledge base** | Obsidian vault — wiki pages authored in markdown + YAML |
