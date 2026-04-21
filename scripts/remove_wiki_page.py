"""
Delete a wiki page and clean it from all index files.

Accepts the page slug in any format (spaces, dashes, underscores — all normalised).
Searches every source independently so partial deletions are handled correctly.

Removes / patches whatever still exists:
  - The .md file from Vault/wiki/**
  - Node + all edges from webapp/data/_graph.json
  - Relationship entries in OTHER wiki pages referencing this slug
    (YAML frontmatter and [[wikilink]] lines in body)
  - Entry from webapp/data/wiki_search_slugs.json
  - webapp/data/wiki_search.faiss  (deleted — rebuilt at next server startup)
  - Reference lines in Vault/wiki/index.md

Usage:
    python scripts/remove_wiki_page.py <slug>

Examples:
    python scripts/remove_wiki_page.py digital-transformation-4-module-course-outline
    python scripts/remove_wiki_page.py "logistic regression conceptual arc"
    python scripts/remove_wiki_page.py overfitting_prevention_strategies
"""

import os
import re
import sys
import json
import shutil
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


def to_slug(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip()).lower()


def find_md_file(slug: str) -> Path | None:
    if not WIKI_DIR.exists():
        return None
    for md in WIKI_DIR.rglob("*.md"):
        if md.stem == slug:
            return md
    return None


def in_graph(slug: str) -> bool:
    if not GRAPH_PATH.exists():
        return False
    try:
        g = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        return slug in g.get("nodes", {})
    except Exception:
        return False


def in_slugs(slug: str) -> bool:
    if not SLUGS_PATH.exists():
        return False
    try:
        meta = json.loads(SLUGS_PATH.read_text(encoding="utf-8"))
        return slug in meta.get("slugs", [])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

def remove_md_file(md_path: Path):
    bak = md_path.with_suffix(".md.bak")
    shutil.copy2(md_path, bak)
    md_path.unlink()
    print(f"  [file] Deleted  (backup: {bak.name})")


def remove_from_graph(slug: str):
    if not GRAPH_PATH.exists():
        print("  [graph] Not found — skipping")
        return
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    node_found   = slug in graph.get("nodes", {})
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
        print(f"  [graph] node={node_found}, {edges_removed} edge(s) removed  (backup: {bak.name})")
    else:
        print(f"  [graph] '{slug}' not found — skipping")


def clean_other_pages(slug: str):
    if not WIKI_DIR.exists():
        return
    type_line_re  = re.compile(r"^\s*type:\s*\S+\s*$")
    target_re     = re.compile(r"^\s*-\s*target:\s*" + re.escape(slug) + r"\s*$")
    wikilink_line = re.compile(
        r"^[^\n]*\[\[" + re.escape(slug) + r"(?:\|[^\]]+)?\]\][^\n]*\n?", re.MULTILINE
    )
    inline_link   = re.compile(r"\[\[" + re.escape(slug) + r"(?:\|[^\]]+)?\]\]")

    patched = 0
    for md in WIKI_DIR.rglob("*.md"):
        if md.stem == slug or md.name.startswith("_") or md.stem in ("index", "log"):
            continue
        try:
            original = md.read_text(encoding="utf-8")
        except Exception:
            continue
        text = original

        # Strip relationship entries from YAML frontmatter
        fm_match = FRONTMATTER_RE.match(text)
        if fm_match:
            fm_lines = fm_match.group(1).splitlines()
            new_lines = []
            i = 0
            while i < len(fm_lines):
                if target_re.match(fm_lines[i]):
                    i += 1
                    if i < len(fm_lines) and type_line_re.match(fm_lines[i]):
                        i += 1
                    continue
                new_lines.append(fm_lines[i])
                i += 1
            new_fm = "\n".join(new_lines)
            text = text[:fm_match.start(1)] + new_fm + text[fm_match.end(1):]

        # Remove wikilink lines and inline references from body
        text = wikilink_line.sub("", text)
        text = inline_link.sub("", text)

        if text != original:
            md.write_text(text, encoding="utf-8")
            patched += 1
            print(f"  [refs] Cleaned {md.relative_to(PROJECT_ROOT)}")

    if patched == 0:
        print(f"  [refs] No other pages reference '{slug}'")


def remove_from_slugs(slug: str):
    if not SLUGS_PATH.exists():
        print("  [slugs] Not found — skipping")
        return
    meta = json.loads(SLUGS_PATH.read_text(encoding="utf-8"))
    slugs = meta.get("slugs", [])
    if slug not in slugs:
        print(f"  [slugs] '{slug}' not present — skipping")
        return
    meta["slugs"] = [s for s in slugs if s != slug]
    SLUGS_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [slugs] Removed  ({len(slugs)} → {len(meta['slugs'])} entries)")


def invalidate_faiss():
    if not FAISS_PATH.exists():
        print("  [faiss] Not found — skipping")
        return
    bak = FAISS_PATH.with_suffix(".faiss.bak")
    shutil.copy2(FAISS_PATH, bak)
    FAISS_PATH.unlink()
    print(f"  [faiss] Deleted  (backup: {bak.name}) — rebuilds at next server startup")


def remove_from_index_md(slug: str):
    if not INDEX_PATH.exists():
        print("  [index.md] Not found — skipping")
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

    slug = to_slug(" ".join(sys.argv[1:]))
    md_path = find_md_file(slug)

    print(f"\nSlug  : {slug}")
    print(f"Vault : {VAULT}")
    print()
    print(f"  .md file     : {md_path.relative_to(PROJECT_ROOT) if md_path else 'not found'}")
    print(f"  _graph.json  : {'found' if in_graph(slug) else 'not found'}")
    print(f"  slugs.json   : {'found' if in_slugs(slug) else 'not found'}")
    print(f"  wiki_search.faiss : {'found' if FAISS_PATH.exists() else 'not found'}")
    print()

    has_anything = md_path or in_graph(slug) or in_slugs(slug) or FAISS_PATH.exists()
    if not has_anything:
        print("Nothing to clean — slug not found in any source and no FAISS on disk.")
        sys.exit(0)

    answer = input("Delete and clean all references? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted — nothing changed.")
        sys.exit(0)

    print()
    if md_path:
        remove_md_file(md_path)
    else:
        print("  [file] Not present — skipping")

    remove_from_graph(slug)
    clean_other_pages(slug)
    remove_from_slugs(slug)
    invalidate_faiss()  # always delete stale FAISS so it rebuilds clean at startup
    remove_from_index_md(slug)

    print(f"\nDone. FAISS rebuilds automatically at next server startup.")


if __name__ == "__main__":
    main()
