"""
Delete a wiki page and clean it from all index files.

Removes / patches:
  - The .md file from Vault/wiki/** (any subdirectory)
  - Node + all edges from webapp/data/_graph.json
  - Entry from webapp/data/wiki_search_slugs.json
  - webapp/data/wiki_search.faiss  (deleted — rebuilt automatically at next server startup)
  - References in webapp/Vault/wiki/index.md

Each location is handled independently — missing entries are skipped with a warning,
not treated as errors.

Usage:
    python scripts/remove_wiki_page.py <page-name>

<page-name> accepts any separator — spaces, dashes, or underscores:
    python scripts/remove_wiki_page.py digital-transformation-strategy
    python scripts/remove_wiki_page.py "Digital Transformation Strategy"
    python scripts/remove_wiki_page.py digital_transformation_strategy
"""

import os
import re
import sys
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Respect WIKI_VAULT_NAME env var (same logic as graph.py and index2.py)
_vault_name = os.environ.get("WIKI_VAULT_NAME", "Vault")
VAULT = PROJECT_ROOT / "webapp" / _vault_name
if not VAULT.exists():
    # Strip path prefix if needed (e.g. "webapp/Vault" → try "Vault" inside webapp/)
    VAULT = PROJECT_ROOT / "webapp" / Path(_vault_name).name
if not VAULT.exists():
    # Last resort: bare vault name at project root
    VAULT = PROJECT_ROOT / Path(_vault_name).name

WIKI_DIR     = VAULT / "wiki"
WEBAPP_DATA  = PROJECT_ROOT / "webapp" / "data"
GRAPH_PATH   = WEBAPP_DATA / "_graph.json"
FAISS_PATH   = WEBAPP_DATA / "wiki_search.faiss"
SLUGS_PATH   = WEBAPP_DATA / "wiki_search_slugs.json"
INDEX_PATH   = WIKI_DIR / "index.md"


def to_slug(name: str) -> str:
    """Normalise any separator style to kebab-case slug."""
    return re.sub(r"[\s_]+", "-", name.strip()).lower()


def find_wiki_file(slug: str) -> Path | None:
    """Search all subdirs of WIKI_DIR for {slug}.md."""
    if not WIKI_DIR.exists():
        return None
    for md in WIKI_DIR.rglob("*.md"):
        if md.stem == slug:
            return md
    return None


def remove_from_graph(slug: str) -> tuple[bool, int]:
    """Remove node and all edges referencing slug. Returns (found, edges_removed)."""
    if not GRAPH_PATH.exists():
        print(f"  [graph] {GRAPH_PATH.name} not found — skipping")
        return False, 0

    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    node_found = slug in graph.get("nodes", {})
    edges_before = len(graph.get("edges", []))

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
        print(f"  [graph] Removed node={node_found}, {edges_removed} edge(s) → backed up as {bak.name}")
    else:
        print(f"  [graph] Slug '{slug}' not found in nodes or edges — skipping")

    return node_found, edges_removed


def remove_from_slugs(slug: str) -> bool:
    """Remove slug from wiki_search_slugs.json. Returns True if found."""
    if not SLUGS_PATH.exists():
        print(f"  [slugs] {SLUGS_PATH.name} not found — skipping")
        return False

    meta = json.loads(SLUGS_PATH.read_text(encoding="utf-8"))
    slugs = meta.get("slugs", [])
    if slug not in slugs:
        print(f"  [slugs] '{slug}' not in {SLUGS_PATH.name} — skipping")
        return False

    meta["slugs"] = [s for s in slugs if s != slug]
    SLUGS_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [slugs] Removed '{slug}' ({len(slugs)} → {len(meta['slugs'])} slugs)")
    return True


def invalidate_faiss() -> bool:
    """Delete wiki_search.faiss so the server rebuilds it on next startup."""
    if not FAISS_PATH.exists():
        print(f"  [faiss] {FAISS_PATH.name} not found — skipping")
        return False
    bak = FAISS_PATH.with_suffix(".faiss.bak")
    shutil.copy2(FAISS_PATH, bak)
    FAISS_PATH.unlink()
    print(f"  [faiss] Deleted {FAISS_PATH.name} (backed up as {bak.name}) — will rebuild at next startup")
    return True


def remove_from_index_md(slug: str) -> bool:
    """Remove lines referencing slug from index.md."""
    if not INDEX_PATH.exists():
        print(f"  [index.md] Not found — skipping")
        return False

    original = INDEX_PATH.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    kept = [l for l in lines if slug not in l]
    removed = len(lines) - len(kept)
    if removed:
        INDEX_PATH.write_text("".join(kept), encoding="utf-8")
        print(f"  [index.md] Removed {removed} line(s) referencing '{slug}'")
        return True
    else:
        print(f"  [index.md] No lines referencing '{slug}' — skipping")
        return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    raw = " ".join(sys.argv[1:])
    slug = to_slug(raw)

    print(f"\nSlug: '{slug}'")
    print(f"Vault: {VAULT}")
    print(f"Wiki dir: {WIKI_DIR}\n")

    # 1. Find the .md file
    md_file = find_wiki_file(slug)
    if md_file:
        print(f"  [file] Found: {md_file.relative_to(PROJECT_ROOT)}")
    else:
        print(f"  [file] No .md file found for '{slug}' — will still clean indexes")

    # Confirm before doing anything
    print()
    answer = input("Proceed with deletion? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted — nothing changed.")
        sys.exit(0)

    print()

    # 2. Delete .md file
    if md_file:
        bak = md_file.with_suffix(".md.bak")
        shutil.copy2(md_file, bak)
        md_file.unlink()
        print(f"  [file] Deleted {md_file.name} (backed up as {bak.name})")
    else:
        print(f"  [file] Nothing to delete")

    # 3. Clean graph
    remove_from_graph(slug)

    # 4. Clean slugs JSON
    slug_found = remove_from_slugs(slug)

    # 5. Invalidate FAISS (only if slug was in the index — otherwise it's already clean)
    if slug_found:
        invalidate_faiss()
    else:
        print(f"  [faiss] Slug not in index — FAISS unchanged")

    # 6. Clean index.md
    remove_from_index_md(slug)

    print(f"\nDone. Restart the server to pick up the changes.")
    print(f"The wiki FAISS index will be rebuilt automatically on next server startup.")


if __name__ == "__main__":
    main()
