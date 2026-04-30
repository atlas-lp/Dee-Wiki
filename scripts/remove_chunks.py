"""
remove_chunks.py — Interactive RAG chunk inspector and cleaner.

Works on webapp/data/chunks.json + webapp/data/chunks.faiss (deployed files).
Keeps the two files in sync after every deletion.

Usage:
    python scripts/remove_chunks.py

Menu options:
  1  Browse & delete by source document
  2  Browse & delete by Bloom level
  3  Browse & delete by Bloom bucket
  4  Quality filter — delete short / low-quality chunks
  5  Deduplicate identical content
  6  Show stats
  0  Exit
"""

import sys
import json
import shutil
import collections
from pathlib import Path

import numpy as np

try:
    import faiss
except ImportError:
    print("Error: faiss not installed.  Run: pip install faiss-cpu")
    sys.exit(1)

try:
    import colorama
    colorama.init()
    _HAS_COLOR = True
except ImportError:
    _HAS_COLOR = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBAPP_DATA  = PROJECT_ROOT / "webapp" / "data"
CHUNKS_JSON  = WEBAPP_DATA / "chunks.json"
CHUNKS_FAISS = WEBAPP_DATA / "chunks.faiss"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _HAS_COLOR else text

def _amber(t): return _c(str(t), "33")
def _green(t): return _c(str(t), "32")
def _red(t):   return _c(str(t), "31")
def _dim(t):   return _c(str(t), "2")
def _bold(t):  return _c(str(t), "1")
def _blue(t):  return _c(str(t), "34")
def _cyan(t):  return _c(str(t), "36")

def _hr(ch="─", width=70):
    print(_dim(ch * width))

def _yn(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

def _pick(options: list, prompt: str = "Choose") -> int | None:
    for i, o in enumerate(options, 1):
        print(f"  {_amber(i):>4}  {o}")
    print(f"  {_dim('  0'):>4}  {_dim('Cancel')}")
    raw = input(f"{prompt}: ").strip()
    if not raw or raw == "0":
        return None
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return idx
    except ValueError:
        pass
    print(_red("Invalid choice."))
    return None

# ---------------------------------------------------------------------------
# Data loading / saving
# ---------------------------------------------------------------------------

def _load() -> tuple[list, object | None, np.ndarray | None]:
    """Returns (chunks, faiss_index | None, all_vecs | None)."""
    if not CHUNKS_JSON.exists():
        print(_red(f"Error: {CHUNKS_JSON} not found."))
        sys.exit(1)

    chunks = json.loads(CHUNKS_JSON.read_text(encoding="utf-8"))
    print(f"  Loaded {_amber(len(chunks))} chunks from webapp/data/chunks.json")

    faiss_index = all_vecs = None
    if CHUNKS_FAISS.exists():
        faiss_index = faiss.read_index(str(CHUNKS_FAISS))
        n_vecs = faiss_index.ntotal
        if n_vecs == len(chunks):
            all_vecs = faiss_index.reconstruct_n(0, n_vecs).astype(np.float32)
            print(f"  Loaded {_amber(n_vecs)} FAISS vectors  (aligned ✓)")
        else:
            print(_red(f"  Warning: FAISS has {n_vecs} vectors but JSON has {len(chunks)} — FAISS won't be updated"))
            faiss_index = all_vecs = None
    else:
        print(_dim("  No chunks.faiss found — only chunks.json will be updated"))

    return chunks, faiss_index, all_vecs


def _save(chunks: list, keep_idx: list, all_vecs: np.ndarray | None, faiss_index) -> None:
    """Backup → write filtered chunks.json → rebuild chunks.faiss."""
    # Backup
    bak_json = CHUNKS_JSON.with_suffix(".json.bak")
    shutil.copy2(CHUNKS_JSON, bak_json)
    print(f"  Backed up → {bak_json.name}")
    if all_vecs is not None and CHUNKS_FAISS.exists():
        bak_faiss = CHUNKS_FAISS.with_suffix(".faiss.bak")
        shutil.copy2(CHUNKS_FAISS, bak_faiss)
        print(f"  Backed up → {bak_faiss.name}")

    # Write chunks.json
    kept = [chunks[i] for i in keep_idx]
    CHUNKS_JSON.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    print(f"  Saved {_green(len(kept))} chunks → chunks.json")

    # Rebuild FAISS
    if all_vecs is not None:
        kept_vecs = all_vecs[np.array(keep_idx)].astype(np.float32)
        new_idx   = faiss.IndexFlatIP(kept_vecs.shape[1])
        new_idx.add(kept_vecs)
        faiss.write_index(new_idx, str(CHUNKS_FAISS))
        print(f"  Rebuilt FAISS → chunks.faiss  ({new_idx.ntotal} vectors)")


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------

def _doc_name(source: str) -> str:
    """Short display name from absolute source path."""
    return Path(source).name if source else "?"

def _group_by(chunks: list, key: str) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = collections.defaultdict(list)
    for i, c in enumerate(chunks):
        groups[c.get(key, "?") or "?"].append(i)
    return dict(groups)

def _group_by_doc(chunks: list) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = collections.defaultdict(list)
    for i, c in enumerate(chunks):
        groups[_doc_name(c.get("source", ""))].append(i)
    return dict(groups)

# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def action_by_source(chunks, faiss_index, all_vecs):
    _hr()
    print(_bold("Delete by source document"))
    groups = _group_by_doc(chunks)
    sorted_docs = sorted(groups.keys(), key=lambda d: -len(groups[d]))
    options = [
        f"{_blue(doc[:60]):65}  {_dim(str(len(groups[doc])) + ' chunks')}"
        for doc in sorted_docs
    ]
    idx = _pick(options, "Select document")
    if idx is None:
        return chunks, faiss_index, all_vecs

    doc   = sorted_docs[idx]
    iids  = groups[doc]
    print(f"\n  Document : {_blue(doc)}")
    print(f"  Chunks   : {_red(len(iids))}")
    # Show sample source path
    full_src = chunks[iids[0]].get("source", "")
    print(f"  Path     : {_dim(full_src)}")

    if not _yn(f"\n  Delete all {len(iids)} chunks from '{doc}'?"):
        print("  Cancelled.")
        return chunks, faiss_index, all_vecs

    keep_idx = [i for i in range(len(chunks)) if i not in set(iids)]
    _save(chunks, keep_idx, all_vecs, faiss_index)
    return _load()


def action_by_bloom_level(chunks, faiss_index, all_vecs):
    _hr()
    print(_bold("Delete by Bloom's taxonomy level"))
    groups = _group_by(chunks, "bloom_highest_level")
    order  = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]
    sorted_levels = sorted(groups.keys(), key=lambda l: order.index(l) if l in order else 99)
    options = [
        f"{_cyan(lvl):15}  {_dim(str(len(groups[lvl])) + ' chunks')}"
        for lvl in sorted_levels
    ]
    idx = _pick(options, "Select Bloom level")
    if idx is None:
        return chunks, faiss_index, all_vecs

    lvl  = sorted_levels[idx]
    iids = groups[lvl]
    print(f"\n  Level  : {_cyan(lvl)}")
    print(f"  Chunks : {_red(len(iids))}")
    docs = sorted({_doc_name(chunks[i].get("source", "")) for i in iids})
    print(f"  Docs   : {', '.join(docs[:5])}{'…' if len(docs) > 5 else ''}")

    if not _yn(f"\n  Delete all {len(iids)} '{lvl}' chunks?"):
        print("  Cancelled.")
        return chunks, faiss_index, all_vecs

    keep_idx = [i for i in range(len(chunks)) if i not in set(iids)]
    _save(chunks, keep_idx, all_vecs, faiss_index)
    return _load()


def action_by_bloom_bucket(chunks, faiss_index, all_vecs):
    _hr()
    print(_bold("Delete by Bloom bucket"))
    groups = _group_by(chunks, "bloom_bucket")
    sorted_buckets = sorted(groups.keys(), key=lambda b: -len(groups[b]))
    options = [
        f"{_cyan(b):25}  {_dim(str(len(groups[b])) + ' chunks')}"
        for b in sorted_buckets
    ]
    idx = _pick(options, "Select bucket")
    if idx is None:
        return chunks, faiss_index, all_vecs

    bucket = sorted_buckets[idx]
    iids   = groups[bucket]
    print(f"\n  Bucket : {_cyan(bucket)}")
    print(f"  Chunks : {_red(len(iids))}")

    if not _yn(f"\n  Delete all {len(iids)} chunks in bucket '{bucket}'?"):
        print("  Cancelled.")
        return chunks, faiss_index, all_vecs

    keep_idx = [i for i in range(len(chunks)) if i not in set(iids)]
    _save(chunks, keep_idx, all_vecs, faiss_index)
    return _load()


def action_quality_filter(chunks, faiss_index, all_vecs):
    _hr()
    print(_bold("Quality filter — delete short or low-quality chunks"))
    print(f"\n  Current range: {min(c.get('word_count',0) for c in chunks)} – "
          f"{max(c.get('word_count',0) for c in chunks)} words per chunk\n")

    raw = input("  Delete chunks with fewer than N words [default 50, Enter to skip]: ").strip()
    min_words = None
    if raw:
        try:
            min_words = int(raw)
        except ValueError:
            print(_red("  Invalid — skipping word-count filter."))

    flagged = []
    if min_words is not None:
        for i, c in enumerate(chunks):
            if c.get("word_count", 999) < min_words:
                flagged.append((i, f"only {c.get('word_count',0)} words (< {min_words})"))

    if not flagged:
        print(_green("  ✓ No chunks flagged."))
        return chunks, faiss_index, all_vecs

    print(f"\n  Flagged {_red(len(flagged))} chunk(s):\n")
    by_doc: dict[str, list] = collections.defaultdict(list)
    for i, reason in flagged:
        by_doc[_doc_name(chunks[i].get("source", ""))].append((i, reason))
    for doc, items in sorted(by_doc.items()):
        print(f"  {_blue(doc):50}  {_red(len(items))} chunks")
        for _, reason in items[:3]:
            print(f"    {_dim(reason)}")
        if len(items) > 3:
            print(f"    {_dim(f'… and {len(items)-3} more')}")
    print()

    if not _yn(f"  Delete all {len(flagged)} flagged chunk(s)?"):
        print("  Cancelled.")
        return chunks, faiss_index, all_vecs

    remove_set = {i for i, _ in flagged}
    keep_idx   = [i for i in range(len(chunks)) if i not in remove_set]
    _save(chunks, keep_idx, all_vecs, faiss_index)
    return _load()


def action_dedup(chunks, faiss_index, all_vecs):
    _hr()
    print(_bold("Deduplicate identical chunk content"))
    seen:    set[str]  = set()
    to_del:  list[int] = []
    for i, c in enumerate(chunks):
        text = c.get("content", "")
        if text in seen:
            to_del.append(i)
        else:
            seen.add(text)

    if not to_del:
        print(_green("  ✓ No duplicate chunks found."))
        return chunks, faiss_index, all_vecs

    print(f"\n  Found {_red(len(to_del))} duplicate chunk(s).")
    if not _yn(f"  Delete all {len(to_del)} duplicate chunks?"):
        print("  Cancelled.")
        return chunks, faiss_index, all_vecs

    keep_idx = [i for i in range(len(chunks)) if i not in set(to_del)]
    _save(chunks, keep_idx, all_vecs, faiss_index)
    return _load()


def action_stats(chunks):
    _hr()
    print(_bold("Stats"))

    print(f"\n  {_bold('Chunks')}")
    print(f"    Total           : {_amber(len(chunks))}")
    wc = [c.get("word_count", 0) for c in chunks]
    print(f"    Word count      : min={min(wc)}  avg={sum(wc)//len(wc)}  max={max(wc)}")

    by_doc = _group_by_doc(chunks)
    print(f"\n  {_bold('By source document')}  ({len(by_doc)} documents)")
    for doc, iids in sorted(by_doc.items(), key=lambda x: -len(x[1])):
        bar = _amber("█" * min(len(iids), 30))
        print(f"    {doc[:45]:47}  {str(len(iids)):>4}  {bar}")

    by_bloom = _group_by(chunks, "bloom_highest_level")
    print(f"\n  {_bold('By Bloom level')}")
    for lvl in ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]:
        cnt = len(by_bloom.get(lvl, []))
        bar = _cyan("█" * min(cnt, 30))
        print(f"    {lvl:12}  {str(cnt):>4}  {bar}")

    by_bucket = _group_by(chunks, "bloom_bucket")
    print(f"\n  {_bold('By Bloom bucket')}")
    for bucket, iids in sorted(by_bucket.items(), key=lambda x: -len(x[1])):
        bar = _blue("█" * min(len(iids), 30))
        print(f"    {bucket:25}  {str(len(iids)):>4}  {bar}")

    if CHUNKS_FAISS.exists():
        size_mb = CHUNKS_FAISS.stat().st_size / 1024 / 1024
        print(f"\n  chunks.faiss    : {_amber(f'{size_mb:.1f} MB')}")
    size_kb = CHUNKS_JSON.stat().st_size / 1024
    print(f"  chunks.json     : {_amber(f'{size_kb:.0f} KB')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MENU = [
    "Browse & delete by source document",
    "Browse & delete by Bloom level",
    "Browse & delete by Bloom bucket",
    "Quality filter — delete short / low-quality chunks",
    "Deduplicate identical content",
    "Show stats",
]


def main():
    print(_bold(_amber("\n  ◈  RAG Chunk Manager  ◈\n")))
    print(f"  JSON  : {_dim(CHUNKS_JSON)}")
    print(f"  FAISS : {_dim(CHUNKS_FAISS)}")
    print()

    chunks, faiss_index, all_vecs = _load()

    ACTIONS = [
        lambda c, fi, av: action_by_source(c, fi, av),
        lambda c, fi, av: action_by_bloom_level(c, fi, av),
        lambda c, fi, av: action_by_bloom_bucket(c, fi, av),
        lambda c, fi, av: action_quality_filter(c, fi, av),
        lambda c, fi, av: action_dedup(c, fi, av),
        lambda c, fi, av: (action_stats(c), (c, fi, av))[1],
    ]

    while True:
        _hr("═")
        print(_bold(f"\n  Main Menu  ({_amber(len(chunks))} chunks)\n"))
        for i, label in enumerate(MENU, 1):
            print(f"    {_amber(i):>4}  {label}")
        print(f"    {_dim('   0'):>4}  Exit")
        _hr()

        choice = input("  Choice: ").strip()
        if choice == "0":
            print(_dim("  Bye."))
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(ACTIONS):
                result = ACTIONS[idx](chunks, faiss_index, all_vecs)
                chunks, faiss_index, all_vecs = result
            else:
                print(_red("  Invalid choice."))
        except (ValueError, IndexError):
            print(_red("  Please enter a number."))
        print()


if __name__ == "__main__":
    main()
