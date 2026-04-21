"""
Delete a wiki page and clean it from all index files.

Candidates are collected from ALL sources (graph nodes, wiki_search_slugs.json,
and .md files) so the page shows up even if it was already partially deleted.

Removes / patches whatever still exists:
  - The .md file from Vault/wiki/** (if present)
  - Node + all edges from webapp/data/_graph.json
  - Relationship entries in OTHER wiki pages referencing this slug
    (YAML frontmatter and [[wikilink]] lines in body)
  - Entry from webapp/data/wiki_search_slugs.json
  - webapp/data/wiki_search.faiss (deleted — rebuilt at next server startup)
  - Reference lines in webapp/Vault/wiki/index.md

Each location is handled independently — missing = skipped, not an error.

Usage:
    python scripts/remove_wiki_page.py <search-term>

Examples:
    python scripts/remove_wiki_page.py logistic regression
    python scripts/remove_wiki_page.py overfitting
    python scripts/remove_wiki_page.py "digital transformation"
"""

import os
import re
import sys
import json
import shutil
from difflib import SequenceMatcher
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_vault_name = os.environ.get("WIKI_VAULT_NAME", "Vault")
VAULT = PROJECT_ROOT / "webapp" / _vault_name
if not VAULT.exists():
    VAULT = PROJECT_ROOT / "webapp" / Path(_vault_name).name
if not VAULT.exists():
    VAULT = PROJECT_ROOT / Path(_vault_name).name

WIKI_DIR    = VAULT / "wiki"
WEBAPP_DATA = PROJECT_ROOT / "webapp" / "data"
GRAPH_PATH  = WEBAPP_DATA / "_graph.json"
FAISS_PATH  = WEBAPP_DATA / "wiki_search.faiss"
SLUGS_PATH  = WEBAPP_DATA / "wiki_search_slugs.json"
INDEX_PATH  = WIKI_DIR / "index.md"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Candidate collection — from every source, not just .md files
# ---------------------------------------------------------------------------

def to_slug(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip()).lower()


def _title_from_content(content: str, stem: str) -> str:
    body = FRONTMATTER_RE.sub("", content)
    for line in body.splitlines():
        if line.startswith("# "):
            return line.lstrip("# ").strip()
    return stem.replace("-", " ").title()


def _collect_candidates() -> dict:
    """
    Build {slug: {title, md_path|None}} from every available source.
    Sources: existing .md files, _graph.json nodes, wiki_search_slugs.json.
    """
    candidates = {}

    # Source 1: .md files
    if WIKI_DIR.exists():
        for md in WIKI_DIR.rglob("*.md"):
            if md.name.startswith("_") or md.stem in ("index", "log"):
                continue
            try:
                content = md.read_text(encoding="utf-8")
                title = _title_from_content(content, md.stem)
            except Exception:
                title = md.stem.replace("-", " ").title()
            candidates[md.stem] = {"title": title, "md_path": md}

    # Source 2: _graph.json nodes
    if GRAPH_PATH.exists():
        try:
            graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
            for slug, node in graph.get("nodes", {}).items():
                if slug not in candidates:
                    candidates[slug] = {
                        "title": node.get("title", slug.replace("-", " ").title()),
                        "md_path": None,
                    }
        except Exception:
            pass

    # Source 3: wiki_search_slugs.json
    if SLUGS_PATH.exists():
        try:
            meta = json.loads(SLUGS_PATH.read_text(encoding="utf-8"))
            for slug in meta.get("slugs", []):
                if slug not in candidates:
                    candidates[slug] = {
                        "title": slug.replace("-", " ").title(),
                        "md_path": None,
                    }
        except Exception:
            pass

    return candidates


def _score(slug: str, title: str, query: str) -> float:
    q = query.lower()
    slug_ratio  = SequenceMatcher(None, q, slug).ratio()
    title_ratio = SequenceMatcher(None, q, title.lower()).ratio()
    slug_sub    = 0.35 if q in slug else 0.0
    title_sub   = 0.35 if q in title.lower() else 0.0
    return max(slug_ratio + slug_sub, title_ratio + title_sub)


def fuzzy_pick(query: str, candidates: dict, top_k: int = 10) -> list[tuple]:
    """Return top_k (slug, info) tuples sorted by score."""
    scored = sorted(
        candidates.items(),
        key=lambda item: _score(item[0], item[1]["title"], query),
        reverse=True,
    )
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Removal functions
# ---------------------------------------------------------------------------

def remove_md_file(md_path: Path | None):
    if md_path is None or not md_path.exists():
        print("  [file] .md file not present — skipping")
        return
    bak = md_path.with_suffix(".md.bak")
    shutil.copy2(md_path, bak)
    md_path.unlink()
    print(f"  [file] Deleted {md_path.relative_to(PROJECT_ROOT)}  (backup: {bak.name})")


def remove_from_graph(slug: str):
    if not GRAPH_PATH.exists():
        print(f"  [graph] Not found — skipping")
        return
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    node_found    = slug in graph.get("nodes", {})
    edges_before  = len(graph.get("edges", []))
    if node_found:
        del graph["nodes"][slug]
    graph["edges"] = [
        e for e in graph.get("edges", [])
        if e.get("from") != slug and e.get("to") != slug
    ]
    edges_removed = edges_before - len(graph["edges"])
    if node_found or edges_removed:
        bak = GRAPH_PATH.with_suffix(".json.bak")
        shutil.copy2(GRAPH_PATH, bak)
        GRAPH_PATH.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  [graph] node={node_found}, {edges_removed} edge(s) removed  (backup: {bak.name})")
    else:
        print(f"  [graph] '{slug}' not in nodes/edges — skipping")


def clean_other_pages(slug: str):
    """Remove all references to slug from every other wiki .md file."""
    if not WIKI_DIR.exists():
        return
    type_line_re   = re.compile(r"^\s*type:\s*\S+\s*$")
    wikilink_line  = re.compile(
        r"^[^\n]*\[\[" + re.escape(slug) + r"(?:\|[^\]]+)?\]\][^\n]*\n?", re.MULTILINE
    )
    inline_link    = re.compile(r"\[\[" + re.escape(slug) + r"(?:\|[^\]]+)?\]\]")
    target_re      = re.compile(r"^\s*-\s*target:\s*" + re.escape(slug) + r"\s*$")

    patched = 0
    for md in WIKI_DIR.rglob("*.md"):
        if md.stem == slug or md.name.startswith("_") or md.stem in ("index", "log"):
            continue
        try:
            original = md.read_text(encoding="utf-8")
        except Exception:
            continue

        text = original

        # 1. Strip relationship blocks from YAML frontmatter
        fm_match = FRONTMATTER_RE.match(text)
        if fm_match:
            fm_lines = fm_match.group(1).splitlines()
            new_fm_lines = []
            i = 0
            while i < len(fm_lines):
                if target_re.match(fm_lines[i]):
                    # Skip this "- target: slug" line
                    i += 1
                    # Skip the following "  type: ..." line if present
                    if i < len(fm_lines) and type_line_re.match(fm_lines[i]):
                        i += 1
                    continue
                new_fm_lines.append(fm_lines[i])
                i += 1
            new_fm = "\n".join(new_fm_lines)
            text = text[:fm_match.start(1)] + new_fm + text[fm_match.end(1):]

        # 2. Remove entire lines that are solely a wikilink to slug
        text = wikilink_line.sub("", text)

        # 3. Strip inline [[slug|...]] references mid-line
        text = inline_link.sub("", text)

        if text != original:
            md.write_text(text, encoding="utf-8")
            patched += 1
            print(f"  [refs] Cleaned {md.relative_to(PROJECT_ROOT)}")

    if patched == 0:
        print(f"  [refs] No other pages reference '{slug}'")


def remove_from_slugs(slug: str) -> bool:
    if not SLUGS_PATH.exists():
        print(f"  [slugs] Not found — skipping")
        return False
    meta = json.loads(SLUGS_PATH.read_text(encoding="utf-8"))
    slugs = meta.get("slugs", [])
    if slug not in slugs:
        print(f"  [slugs] '{slug}' not present — skipping")
        return False
    meta["slugs"] = [s for s in slugs if s != slug]
    SLUGS_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [slugs] Removed '{slug}'  ({len(slugs)} → {len(meta['slugs'])} entries)")
    return True


def invalidate_faiss():
    if not FAISS_PATH.exists():
        print(f"  [faiss] Not found — skipping")
        return
    bak = FAISS_PATH.with_suffix(".faiss.bak")
    shutil.copy2(FAISS_PATH, bak)
    FAISS_PATH.unlink()
    print(f"  [faiss] Deleted (backup: {bak.name}) — rebuilds at next server startup")


def remove_from_index_md(slug: str):
    if not INDEX_PATH.exists():
        print(f"  [index.md] Not found — skipping")
        return
    lines = INDEX_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [l for l in lines if slug not in l]
    removed = len(lines) - len(kept)
    if removed:
        INDEX_PATH.write_text("".join(kept), encoding="utf-8")
        print(f"  [index.md] Removed {removed} line(s)")
    else:
        print(f"  [index.md] No lines referencing '{slug}' — skipping")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    query = to_slug(" ".join(sys.argv[1:]))
    candidates = _collect_candidates()

    if not candidates:
        print(f"No wiki pages found in any source under {WIKI_DIR} / {WEBAPP_DATA}")
        sys.exit(1)

    results = fuzzy_pick(query, candidates, top_k=10)

    sources_checked = []
    if WIKI_DIR.exists():   sources_checked.append(".md files")
    if GRAPH_PATH.exists(): sources_checked.append("_graph.json")
    if SLUGS_PATH.exists(): sources_checked.append("wiki_search_slugs.json")

    print(f"\nSearch: '{query}'")
    print(f"Sources: {', '.join(sources_checked)}")
    print(f"Total candidates: {len(candidates)}\n")
    print("Top matches:")
    for i, (slug, info) in enumerate(results, 1):
        has_file = "✓ file" if info["md_path"] and info["md_path"].exists() else "✗ file"
        print(f"  {i:2}.  {info['title']:<45}  [{slug}]  {has_file}")

    print()
    raw = input("Pick a number (or q to quit): ").strip().lower()
    if raw in ("q", ""):
        print("Aborted.")
        sys.exit(0)

    try:
        choice = int(raw)
        assert 1 <= choice <= len(results)
    except (ValueError, AssertionError):
        print("Invalid choice — aborted.")
        sys.exit(1)

    slug, info = results[choice - 1]
    md_path = info["md_path"]

    print(f"\nSelected: '{info['title']}'  [{slug}]")
    if md_path and md_path.exists():
        print(f"File: {md_path.relative_to(PROJECT_ROOT)}")
    else:
        print(f"File: not found (already deleted or never existed)")
    print()

    answer = input("Delete and clean all references? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted — nothing changed.")
        sys.exit(0)

    print()
    remove_md_file(md_path)
    remove_from_graph(slug)
    clean_other_pages(slug)
    remove_from_slugs(slug)
    invalidate_faiss()
    remove_from_index_md(slug)

    print(f"\nDone. '{info['title']}' removed from all indexes.")
    print("FAISS rebuilds automatically at next server startup.")


if __name__ == "__main__":
    main()
