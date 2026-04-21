"""
Delete a wiki page and clean it from all index files.

Fuzzy-searches all wiki pages, shows top 10 matches, lets you pick one.

Removes / patches:
  - The .md file from Vault/wiki/**
  - Node + all edges from webapp/data/_graph.json
  - Relationship entries in OTHER wiki pages that reference this slug
    (both YAML frontmatter and ## Relationships body section)
  - Entry from webapp/data/wiki_search_slugs.json
  - webapp/data/wiki_search.faiss  (deleted — rebuilt at next server startup)
  - Reference lines in webapp/Vault/wiki/index.md

Each location is handled independently — missing entries are skipped, not errors.

Usage:
    python scripts/remove_wiki_page.py <search-term>

Examples:
    python scripts/remove_wiki_page.py digital transformation
    python scripts/remove_wiki_page.py "logistic regression"
    python scripts/remove_wiki_page.py overfitting
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
# Helpers
# ---------------------------------------------------------------------------

def to_slug(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip()).lower()


def _extract_title(content: str, stem: str) -> str:
    body = FRONTMATTER_RE.sub("", content)
    for line in body.splitlines():
        if line.startswith("# "):
            return line.lstrip("# ").strip()
    return stem.replace("-", " ").title()


def _load_all_pages() -> list[dict]:
    """Return list of {slug, title, path} for every wiki .md file."""
    pages = []
    if not WIKI_DIR.exists():
        return pages
    for md in sorted(WIKI_DIR.rglob("*.md")):
        if md.name.startswith("_") or md.stem in ("index", "log"):
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except Exception:
            continue
        pages.append({
            "slug": md.stem,
            "title": _extract_title(content, md.stem),
            "path": md,
            "content": content,
        })
    return pages


def _score(page: dict, query: str) -> float:
    """Fuzzy score: highest of slug ratio, title ratio, and substring bonus."""
    q = query.lower()
    slug_ratio  = SequenceMatcher(None, q, page["slug"]).ratio()
    title_ratio = SequenceMatcher(None, q, page["title"].lower()).ratio()
    # Substring match gives a strong bonus
    slug_sub  = 0.3 if q in page["slug"] else 0.0
    title_sub = 0.3 if q in page["title"].lower() else 0.0
    return max(slug_ratio + slug_sub, title_ratio + title_sub)


def fuzzy_search(query: str, pages: list[dict], top_k: int = 10) -> list[dict]:
    scored = sorted(pages, key=lambda p: _score(p, query), reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Removal functions
# ---------------------------------------------------------------------------

def remove_md_file(page: dict) -> bool:
    md = page["path"]
    bak = md.with_suffix(".md.bak")
    shutil.copy2(md, bak)
    md.unlink()
    print(f"  [file] Deleted {md.relative_to(PROJECT_ROOT)}  (backup: {bak.name})")
    return True


def remove_from_graph(slug: str):
    if not GRAPH_PATH.exists():
        print(f"  [graph] {GRAPH_PATH.name} not found — skipping")
        return
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
        print(f"  [graph] node={node_found}, {edges_removed} edge(s) removed  (backup: {bak.name})")
    else:
        print(f"  [graph] '{slug}' not found in nodes or edges — skipping")


def clean_other_pages(slug: str, all_pages: list[dict]):
    """
    Remove all references to `slug` from every OTHER wiki page:
      - YAML frontmatter: relationships entries with target: slug
      - Body: lines containing [[slug|...]] or [[slug]]
    """
    type_line_pattern = re.compile(r"^\s*type:\s*\S+\s*$")
    wikilink_pattern  = re.compile(
        r"^\s*[-*]?\s*\[\[" + re.escape(slug) + r"(?:\|[^\]]+)?\]\].*\n?", re.MULTILINE
    )
    # Also matches inline [[slug|...]] anywhere in a line
    inline_wikilink   = re.compile(r"\[\[" + re.escape(slug) + r"(?:\|[^\]]+)?\]\]")

    patched = 0
    for page in all_pages:
        if page["slug"] == slug:
            continue
        original = page["content"]
        text = original

        # 1. Strip YAML relationship blocks: "  - target: slug\n    type: ...\n"
        # We do this manually to avoid breaking other frontmatter
        fm_match = FRONTMATTER_RE.match(text)
        if fm_match:
            fm_raw = fm_match.group(1)
            fm_new_lines = []
            lines = fm_raw.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i]
                if re.match(r"^\s*-\s*target:\s*" + re.escape(slug) + r"\s*$", line):
                    # Skip this line and the next `type:` line if present
                    i += 1
                    if i < len(lines) and type_line_pattern.match(lines[i]):
                        i += 1
                    continue
                fm_new_lines.append(line)
                i += 1
            new_fm = "\n".join(fm_new_lines)
            text = text[:fm_match.start(1)] + new_fm + text[fm_match.end(1):]

        # 2. Strip standalone wikilink lines in body (## Relationships section etc.)
        text = wikilink_pattern.sub("", text)

        # 3. Strip inline [[slug|...]] references mid-line
        text = inline_wikilink.sub("", text)

        if text != original:
            page["path"].write_text(text, encoding="utf-8")
            patched += 1
            print(f"  [refs] Cleaned reference in {page['path'].relative_to(PROJECT_ROOT)}")

    if patched == 0:
        print(f"  [refs] No other pages reference '{slug}'")


def remove_from_slugs(slug: str) -> bool:
    if not SLUGS_PATH.exists():
        print(f"  [slugs] {SLUGS_PATH.name} not found — skipping")
        return False
    meta = json.loads(SLUGS_PATH.read_text(encoding="utf-8"))
    slugs = meta.get("slugs", [])
    if slug not in slugs:
        print(f"  [slugs] '{slug}' not in slugs file — skipping")
        return False
    meta["slugs"] = [s for s in slugs if s != slug]
    SLUGS_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [slugs] Removed '{slug}'  ({len(slugs)} → {len(meta['slugs'])} slugs)")
    return True


def invalidate_faiss():
    if not FAISS_PATH.exists():
        print(f"  [faiss] {FAISS_PATH.name} not found — skipping")
        return
    bak = FAISS_PATH.with_suffix(".faiss.bak")
    shutil.copy2(FAISS_PATH, bak)
    FAISS_PATH.unlink()
    print(f"  [faiss] Deleted (backup: {bak.name}) — rebuilds at next server startup")


def remove_from_index_md(slug: str):
    if not INDEX_PATH.exists():
        print(f"  [index.md] Not found — skipping")
        return
    original = INDEX_PATH.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    kept = [l for l in lines if slug not in l]
    removed = len(lines) - len(kept)
    if removed:
        INDEX_PATH.write_text("".join(kept), encoding="utf-8")
        print(f"  [index.md] Removed {removed} line(s) referencing '{slug}'")
    else:
        print(f"  [index.md] No lines referencing '{slug}' — skipping")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found: {WIKI_DIR}")
        sys.exit(1)

    query = to_slug(" ".join(sys.argv[1:]))
    all_pages = _load_all_pages()

    if not all_pages:
        print(f"No wiki pages found in {WIKI_DIR}")
        sys.exit(1)

    results = fuzzy_search(query, all_pages, top_k=10)

    print(f"\nSearch: '{query}'  |  Wiki: {WIKI_DIR}\n")
    print("Top matches:")
    for i, p in enumerate(results, 1):
        print(f"  {i:2}.  {p['title']:<45}  [{p['slug']}]")

    print()
    raw = input("Pick a number (or q to quit): ").strip().lower()
    if raw == "q" or not raw:
        print("Aborted.")
        sys.exit(0)

    try:
        choice = int(raw)
        assert 1 <= choice <= len(results)
    except (ValueError, AssertionError):
        print("Invalid choice — aborted.")
        sys.exit(1)

    page = results[choice - 1]
    slug = page["slug"]

    print(f"\nSelected: '{page['title']}'  [{slug}]")
    print(f"File: {page['path'].relative_to(PROJECT_ROOT)}\n")

    answer = input("Delete this page and clean all references? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted — nothing changed.")
        sys.exit(0)

    print()

    # Reload all pages fresh before patching (in case paths changed)
    all_pages_fresh = _load_all_pages()

    remove_md_file(page)
    remove_from_graph(slug)
    clean_other_pages(slug, all_pages_fresh)
    slug_found = remove_from_slugs(slug)
    if slug_found:
        invalidate_faiss()
    else:
        # Still invalidate if the .md existed — the page was in the wiki
        invalidate_faiss()
    remove_from_index_md(slug)

    print(f"\nDone. '{page['title']}' removed from all indexes.")
    print("The wiki FAISS index rebuilds automatically at next server startup.")


if __name__ == "__main__":
    main()
