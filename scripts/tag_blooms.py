"""
Tag all RAG chunks in data/chunks.json with Bloom's Taxonomy metadata.

Pipeline (default):
  Phase 1 — Gemma 4 extracts learning goals/outcomes from each chunk.
  Phase 2 — BERT classifier tags each goal; proportions aggregated per chunk.

After running, execute:
    python scripts/export_for_web.py
to push bloom fields into webapp/data/chunks.json (no re-embedding).

Usage:
    python scripts/tag_blooms.py                  # tag only untagged chunks (Gemma + BERT)
    python scripts/tag_blooms.py --skip-gemma     # BERT only (sentence-split, original method)
    python scripts/tag_blooms.py --reset          # strip all bloom tags and re-tag from scratch
    python scripts/tag_blooms.py --rethreshold    # recompute bloom_predicted_labels from stored
                                                  # confidences using current BLOOMS_TUNED_THRESHOLDS
                                                  # (no model inference — instant)

Flags:
    --reset          Strip existing bloom fields from all chunks before tagging.
                     Use this after changing the model or retraining.

    --rethreshold    Recompute bloom_predicted_labels without re-running any model.
                     Edit BLOOMS_TUNED_THRESHOLDS in chunk_blooms_tagger.py, then
                     run this to instantly see how the label distribution changes.
                     bloom_highest_level is NOT affected (it is always argmax).

    --skip-gemma     Skip Gemma goal extraction; use original sentence-splitting
                     method. Faster but less accurate (BERT out-of-distribution).
"""

import sys
import json
import shutil
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IS_KAGGLE   = Path('/kaggle/input').exists()

if _IS_KAGGLE:
    OFFLINE_DATA = Path('/kaggle/input/bloom-chunks')
    MODEL_PATH   = Path('/kaggle/input/bloom-bert-classifier/checkpoint-600')
    OUTPUT_PATH  = Path('/kaggle/working/chunks_tagged.json')
else:
    OFFLINE_DATA = PROJECT_ROOT / "data"
    MODEL_PATH   = PROJECT_ROOT / "webapp" / "models" / "checkpoint-600"
    OUTPUT_PATH  = OFFLINE_DATA / "chunks.json"  # overwrites in-place locally

BLOOMS_ORDER = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from chunk_blooms_tagger import (
    load_blooms_model,
    tag_chunks,
    tag_chunks_from_goals,
    BLOOMS_TUNED_THRESHOLDS,
    BLOOMS_LABEL_NAMES,
)


def _print_distribution(chunks):
    total     = len(chunks)
    bar_width = 30

    level_counts = Counter(c.get("bloom_highest_level") for c in chunks)
    print("\nBloom's highest level (one per chunk):")
    print(f"  {'Level':<12}  {'Count':>6}  {'%':>6}  Bar")
    for level in BLOOMS_ORDER:
        n   = level_counts.get(level, 0)
        pct = n / total * 100 if total else 0
        bar = "█" * int(pct / 100 * bar_width)
        print(f"  {level:<12}  {n:>6}  {pct:>5.1f}%  {bar}")
    unclassified = level_counts.get(None, 0)
    if unclassified:
        print(f"  {'(untagged)':<12}  {unclassified:>6}  {unclassified/total*100:>5.1f}%")

    label_counts = Counter()
    below_thresh = 0
    for c in chunks:
        labels = c.get("bloom_predicted_labels") or []
        if labels == ["Below threshold"]:
            below_thresh += 1
        else:
            label_counts.update(labels)
    print(f"\nBloom's predicted labels (chunks can appear in multiple):")
    print(f"  {'Level':<12}  {'Count':>6}  {'%':>6}  Bar")
    for level in BLOOMS_ORDER:
        n   = label_counts.get(level, 0)
        pct = n / total * 100 if total else 0
        bar = "█" * int(pct / 100 * bar_width)
        print(f"  {level:<12}  {n:>6}  {pct:>5.1f}%  {bar}")
    if below_thresh:
        print(f"  {'(no label)':<12}  {below_thresh:>6}  {below_thresh/total*100:>5.1f}%")


BLOOM_FIELDS = [
    "bloom_highest_level", "bloom_bucket", "bloom_predicted_labels",
    "bloom_confidences", "bloom_sentence_count",
    "understand_bkt_score", "analyze_bkt_score", "create_bkt_score",
]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Tag RAG chunks with Bloom's Taxonomy levels.")
    parser.add_argument("--reset",       action="store_true", help="Strip existing bloom tags and re-tag all chunks from scratch.")
    parser.add_argument("--rethreshold", action="store_true", help="Recompute bloom_predicted_labels from stored confidences using current thresholds (no model inference).")
    parser.add_argument("--skip-gemma",  action="store_true", help="Skip Gemma goal extraction; use original sentence-splitting method.")
    args = parser.parse_args()

    chunks_path = OFFLINE_DATA / "chunks.json"
    if not chunks_path.exists():
        print(f"Error: {chunks_path} not found.")
        sys.exit(1)
    print(f"{'[Kaggle] ' if _IS_KAGGLE else ''}Input : {chunks_path}")
    print(f"{'[Kaggle] ' if _IS_KAGGLE else ''}Output: {OUTPUT_PATH}")

    if not MODEL_PATH.exists():
        print(f"Error: model not found at {MODEL_PATH}")
        sys.exit(1)

    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(chunks)} chunks from data/chunks.json")

    # --rethreshold: recompute labels from stored proportions, no model needed
    if args.rethreshold:
        recomputed = 0
        for c in chunks:
            confs = c.get("bloom_confidences")
            if not confs:
                continue
            # proportions already stored (sum ~1.0) — apply thresholds directly
            predicted = [l for l in BLOOMS_LABEL_NAMES if confs.get(l, 0) >= BLOOMS_TUNED_THRESHOLDS[l]] or ["Below threshold"]
            c["bloom_predicted_labels"] = predicted
            recomputed += 1
        OUTPUT_PATH.write_text(json.dumps(chunks, indent=2), encoding="utf-8")
        print(f"Recomputed bloom_predicted_labels for {recomputed} chunks using current thresholds.")
        _print_distribution(chunks)
        return

    if args.reset:
        for c in chunks:
            for f in BLOOM_FIELDS:
                c.pop(f, None)
        print(f"Reset: stripped bloom fields from all {len(chunks)} chunks.")

    untagged_idx = [
        i for i, c in enumerate(chunks)
        if not c.get("bloom_highest_level")
    ]
    print(f"Already tagged : {len(chunks) - len(untagged_idx)}")
    print(f"To tag         : {len(untagged_idx)}")

    if not untagged_idx:
        print("All chunks already tagged — nothing to do.")
        _print_distribution(chunks)
        return

    texts = [chunks[i]["content"] for i in untagged_idx]

    if args.skip_gemma:
        # Original method: sentence splitting → BERT
        print(f"\nLoading BERT from {MODEL_PATH} ...")
        tok, bert_model, device = load_blooms_model(str(MODEL_PATH))
        print(f"Model on {device}")
        metas = tag_chunks(texts, tok, bert_model, device)

    else:
        # Phase 1: Gemma 4 goal extraction
        from gemma_goal_extractor import load_gemma, extract_goals
        llm = load_gemma()
        goals_per_chunk = []
        log_every = max(1, len(texts) // 20)  # ~20 log lines total
        for idx, text in enumerate(texts):
            goals = extract_goals(text, llm)
            goals_per_chunk.append(goals)
            if (idx + 1) % log_every == 0 or idx == len(texts) - 1:
                total_goals = sum(len(g) for g in goals_per_chunk)
                print(f"  [Gemma] {idx+1}/{len(texts)} chunks  ({total_goals} goals so far)")
        print(f"[Gemma] Done. Total goals extracted: {sum(len(g) for g in goals_per_chunk)}")
        zero_goal = sum(1 for g in goals_per_chunk if not g)
        if zero_goal:
            print(f"[Gemma] Warning: {zero_goal} chunk(s) returned no goals — will be marked unclassified.")

        # Phase 2: BERT classification on extracted goals
        print(f"\nLoading BERT from {MODEL_PATH} ...")
        tok, bert_model, device = load_blooms_model(str(MODEL_PATH))
        print(f"Model on {device}")
        metas = tag_chunks_from_goals(goals_per_chunk, tok, bert_model, device)

    for i, meta in zip(untagged_idx, metas):
        chunks[i].update(meta)

    if not _IS_KAGGLE:
        bak = OUTPUT_PATH.with_suffix(".json.bak")
        shutil.copy2(OUTPUT_PATH, bak)
        print(f"\nBacked up → {bak.name}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(chunks, indent=2), encoding="utf-8")
    print(f"Saved {len(chunks)} chunks → {OUTPUT_PATH}")

    _print_distribution(chunks)

    print(f"\nDone. Bloom tags written for {len(untagged_idx)} chunks.")
    print("Next: python scripts/export_for_web.py")


if __name__ == "__main__":
    main()
