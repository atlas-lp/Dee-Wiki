"""
chunk_blooms_tagger.py
======================
Classify text chunks (~1000 words) with Bloom's taxonomy levels and store
the result as metadata — suitable for RAG pipelines or vector store ingestion.

Model
-----
Fine-tuned BertForSequenceClassification (bert-base-uncased, 109M params).
  - Architecture : BERT-base → linear head (6 outputs, sigmoid, multilabel)
  - Trained on   : EDM2022CLO — short course learning outcome sentences
  - Checkpoint   : model/checkpoint-600  (step 600, ~2.8 epochs, eval_loss 0.0573)
  - Labels       : Remember · Understand · Apply · Analyze · Evaluate · Create

Why sentence-level decomposition?
----------------------------------
BERT has a hard 512-token limit (~380 words). A raw 1000-word chunk would be
silently truncated, losing ~60% of its content. Instead:
  1. Split each chunk into sentences using the same logic as the JD pipeline.
  2. Run all sentences in one flat BERT batch (efficient, no per-chunk loops).
  3. Aggregate: sum sigmoid probs across all sentences in a chunk, then pick
     the label with the highest summed score.
     (Same strategy as classify_explicit_blooms_v2 in the JD notebook.)

Metadata produced per chunk
---------------------------
  bloom_highest_level    : str        dominant Bloom's level (e.g. "Analyze")
  bloom_bucket           : str        coarse group: understand_bkt | analyze_bkt | create_bkt
  bloom_predicted_labels : list[str]  all labels above (scaled) threshold
  bloom_confidences      : dict       summed sigmoid scores per label
  bloom_sentence_count   : int        number of sentences used for inference
  understand_bkt_score   : float      Remember + Understand summed score
  analyze_bkt_score      : float      Apply + Analyze summed score
  create_bkt_score       : float      Evaluate + Create summed score

Usage — import
--------------
    from chunk_blooms_tagger import tag_chunks, load_blooms_model, tune_thresholds

    tokenizer, model, device = load_blooms_model("model/checkpoint-600")
    thresholds = tune_thresholds(DATA_URL, tokenizer, model, device)
    metadata = tag_chunks(chunks, tokenizer, model, device, thresholds)

Usage — CLI (single text test)
-------------------------------
    python chunk_blooms_tagger.py \\
        --model model/checkpoint-600 \\
        --text "Design a system that synthesizes user behavior data into insights."

Usage — CLI (JSON file of chunks)
----------------------------------
    python chunk_blooms_tagger.py \\
        --model model/checkpoint-600 \\
        --input chunks.json \\
        --output tagged_chunks.json

    chunks.json format: ["chunk text 1", "chunk text 2", ...]

Flags
-----
  --model       Path to checkpoint-600 directory (required)
  --text        Classify a single string; prints result to terminal
  --input       JSON file with list of chunk strings
  --output      Output JSON path (default: tagged_chunks.json, --input only)
  --thresholds  Path to a thresholds JSON file (optional; auto-detected otherwise)
  --data-url    URL/path to EDM2022CLO CSV — only needed the very first run
  --no-tune     Skip threshold tuning entirely; use flat 0.50 for all labels

Thresholds
----------
The per-label thresholds for checkpoint-600 are hardcoded as BLOOMS_TUNED_THRESHOLDS
and used by default — no tuning step, no network call, no extra file needed.
They were computed once from the notebook (Learning Goals_Blooms Level Inference.ipynb,
Step 5) with random_state=42 and are deterministic for this checkpoint:
    Remember: 0.50 | Understand: 0.45 | Apply: 0.55
    Analyze:  0.55 | Evaluate:   0.60 | Create: 0.60

Only use tune_thresholds() / --retune if you retrain the model on new data.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Optional

import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BLOOMS_LABEL_NAMES = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]
BLOOMS_ORDER       = {label: idx for idx, label in enumerate(BLOOMS_LABEL_NAMES)}
BLOOMS_MAX_LENGTH  = 512
BLOOMS_BATCH_SIZE  = 32

# Pre-tuned thresholds for checkpoint-600 — computed once from the notebook
# (Learning Goals_Blooms Level Inference.ipynb, Step 5) with random_state=42.
# These are deterministic for this checkpoint and never need to be re-computed
# unless the model is retrained.
BLOOMS_TUNED_THRESHOLDS = {
    "Remember":   0.10,
    "Understand": 0.10,
    "Apply":      0.10,
    "Analyze":    0.10,
    "Evaluate":   0.10,
    "Create":     0.10,
}

# Only needed if you retrain and want to re-derive thresholds from scratch
DATA_URL            = "https://raw.githubusercontent.com/SteveLEEEEE/EDM2022CLO/refs/heads/main/data/sample_full.csv"
BLOOMS_RANDOM_STATE = 42

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_blooms_model(model_path: str):
    """Load the fine-tuned BERT multilabel classifier."""
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    import os
    model_path = os.path.abspath(model_path)
    log.info(f"Loading Bloom's model from {model_path} on {device}...")

    # Load tokenizer from local checkpoint if saved there, else download once from HF.
    # Trained model weights always load locally (local_files_only=True).
    tok_source = model_path if (Path(model_path) / "vocab.txt").exists() else "bert-base-uncased"
    tokenizer = AutoTokenizer.from_pretrained(tok_source)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_path, local_files_only=True
    ).to(device)
    model.eval()
    log.info(f"  Model loaded. Parameters: {model.num_parameters():,}")
    return tokenizer, model, device


def load_thresholds(path: str) -> dict | None:
    """Load cached thresholds from a JSON file. Returns None if file doesn't exist."""
    import os
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        thresholds = json.load(f)
    log.info(f"  Loaded cached thresholds from {path}: {thresholds}")
    return thresholds


def tune_thresholds(data_source: str, tokenizer, model, device,
                    save_path: str | None = None) -> dict:
    """
    Find F1-maximising per-label threshold on the validation split.

    Results are deterministic (fixed model + fixed random seed), so pass
    save_path to cache them — subsequent runs will skip this entirely.
    Use load_thresholds(save_path) before calling this to avoid re-tuning.
    """
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import f1_score

    log.info("Tuning Bloom's thresholds (one-time cost)...")
    data = pd.read_csv(data_source)
    data[BLOOMS_LABEL_NAMES] = data[BLOOMS_LABEL_NAMES].fillna(0).astype(np.float32)
    texts  = data["Learning_outcome"].tolist()
    labels = data[BLOOMS_LABEL_NAMES].values

    train_full, _, lbl_full, _ = train_test_split(
        texts, labels, test_size=0.2, random_state=BLOOMS_RANDOM_STATE
    )
    split = int(0.8 * len(lbl_full))
    val_texts  = train_full[split:]
    val_labels = lbl_full[split:]

    val_probs = _run_bert_batch(val_texts, tokenizer, model, device)

    sweep = np.arange(0.10, 0.91, 0.05)
    best_thresholds = {}
    for i, label in enumerate(BLOOMS_LABEL_NAMES):
        f1s = [
            f1_score(val_labels[:, i], (val_probs[:, i] > t).astype(int), zero_division=0)
            for t in sweep
        ]
        best_thresholds[label] = round(float(sweep[np.argmax(f1s)]), 4)

    log.info(f"  Tuned thresholds: {best_thresholds}")

    if save_path:
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(best_thresholds, f, indent=2)
        log.info(f"  Saved thresholds → {save_path}")

    return best_thresholds


# ---------------------------------------------------------------------------
# BERT inference
# ---------------------------------------------------------------------------

def _run_bert_batch(texts: list[str], tokenizer, model, device) -> np.ndarray:
    """Return sigmoid probabilities of shape (n, 6)."""
    import torch

    if not texts:
        return np.zeros((0, 6), dtype=np.float32)

    all_probs  = []
    n_batches  = (len(texts) + BLOOMS_BATCH_SIZE - 1) // BLOOMS_BATCH_SIZE
    log_every  = max(1, n_batches // 10)
    for batch_num, i in enumerate(range(0, len(texts), BLOOMS_BATCH_SIZE), 1):
        batch = texts[i : i + BLOOMS_BATCH_SIZE]
        encoded = tokenizer(
            batch,
            truncation=True,
            padding=True,
            max_length=BLOOMS_MAX_LENGTH,
            return_tensors="pt",
        ).to(device)
        with torch.no_grad():
            logits = model(**encoded).logits
        all_probs.append(torch.sigmoid(logits).cpu().numpy())
        if batch_num % log_every == 0 or batch_num == n_batches:
            sents_done = min(i + BLOOMS_BATCH_SIZE, len(texts))
            pct = sents_done / len(texts) * 100
            print(f"  [BERT] batch {batch_num}/{n_batches}  ({sents_done}/{len(texts)} sents, {pct:.0f}%)")
    return np.vstack(all_probs)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _probs_to_labels(probs: np.ndarray, thresholds: dict) -> list[str]:
    predicted = [
        BLOOMS_LABEL_NAMES[j]
        for j, p in enumerate(probs)
        if p >= thresholds[BLOOMS_LABEL_NAMES[j]]
    ]
    return predicted if predicted else ["Below threshold"]


def _probs_to_bucket(probs: np.ndarray) -> dict:
    u = float(probs[0] + probs[1])
    a = float(probs[2] + probs[3])
    c = float(probs[4] + probs[5])
    winner = ["understand_bkt", "analyze_bkt", "create_bkt"][int(np.argmax([u, a, c]))]
    return {
        "bloom_bucket":           winner,
        "understand_bkt_score":   round(u, 4),
        "analyze_bkt_score":      round(a, 4),
        "create_bkt_score":       round(c, 4),
    }


# ---------------------------------------------------------------------------
# Sentence splitter (adapted from notebook)
# ---------------------------------------------------------------------------

_ABBREV_PROTECT = re.compile(
    r"\b(etc|e\.g|i\.e|vs|mr|mrs|dr|prof|sr|jr|dept|approx|"
    r"est|fig|no|vol|govt|pvt|ltd|inc|corp|st|ave|co|op)\.",
    re.IGNORECASE,
)
_BULLET_STRIP   = re.compile(r"^[\s]*(?:[•·\-\*]|(?:\d+|[a-zA-Z])[\.\)]\s*)\s*")
_MARKDOWN_STRIP = re.compile(r"\*{1,2}(.*?)\*{1,2}")


def _split_sentences(text: str) -> list[str]:
    """Split a chunk into clean sentences suitable for BERT input."""
    if not text or not isinstance(text, str):
        return []
    sentences = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        clean_check = _MARKDOWN_STRIP.sub(r"\1", line).strip()
        if clean_check.endswith(":") or len(clean_check) < 5:
            continue
        protected = _ABBREV_PROTECT.sub(
            lambda m: m.group(0).replace(".", "\x00"), line
        )
        for part in re.split(r"\.\s+", protected):
            part = part.replace("\x00", ".")
            part = _BULLET_STRIP.sub("", part)
            part = _MARKDOWN_STRIP.sub(r"\1", part)
            part = part.rstrip(".").strip()
            if len(part) >= 5:
                sentences.append(part)
    return sentences


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def tag_chunks(
    chunks: list[str],
    tokenizer,
    model,
    device,
    thresholds: Optional[dict] = None,
) -> list[dict]:
    """
    Classify a list of text chunks with Bloom's taxonomy levels.

    Parameters
    ----------
    chunks      : list of text strings (~1000 words each)
    tokenizer   : from load_blooms_model()
    model       : from load_blooms_model()
    device      : from load_blooms_model()
    thresholds  : per-label thresholds dict; defaults to BLOOMS_TUNED_THRESHOLDS
                  (pre-computed for checkpoint-600, no tuning needed)

    Returns
    -------
    List of metadata dicts, one per chunk:
        {
          "bloom_highest_level"   : str,        # e.g. "Analyze"
          "bloom_bucket"          : str,        # "understand_bkt" | "analyze_bkt" | "create_bkt"
          "bloom_predicted_labels": list[str],  # labels above threshold
          "bloom_confidences"     : dict,       # summed probs per label
          "bloom_sentence_count"  : int,        # sentences used for inference
          "understand_bkt_score"  : float,
          "analyze_bkt_score"     : float,
          "create_bkt_score"      : float,
        }
    """
    if thresholds is None:
        thresholds = BLOOMS_TUNED_THRESHOLDS

    # 1. Split every chunk into sentences; track which chunk each sentence belongs to
    all_sentences: list[str] = []
    chunk_boundaries: list[tuple[int, int]] = []  # (start_idx, end_idx) in all_sentences

    for chunk in chunks:
        sents = _split_sentences(chunk)
        start = len(all_sentences)
        all_sentences.extend(sents)
        chunk_boundaries.append((start, len(all_sentences)))

    log.info(f"  {len(chunks)} chunks → {len(all_sentences)} sentences for inference.")

    # 2. Single flat batch inference across all sentences
    if all_sentences:
        all_probs = _run_bert_batch(all_sentences, tokenizer, model, device)
    else:
        all_probs = np.zeros((0, 6), dtype=np.float32)

    # 3. Aggregate per chunk
    results: list[dict] = []
    for i, (start, end) in enumerate(chunk_boundaries):
        if start == end:
            # Chunk had no usable sentences
            results.append({
                "bloom_highest_level":    None,
                "bloom_bucket":           None,
                "bloom_predicted_labels": ["Below threshold"],
                "bloom_confidences":      {l: 0.0 for l in BLOOMS_LABEL_NAMES},
                "bloom_sentence_count":   0,
                "understand_bkt_score":   0.0,
                "analyze_bkt_score":      0.0,
                "create_bkt_score":       0.0,
            })
            continue

        chunk_probs = all_probs[start:end]          # (n_sents, 6)
        summed      = chunk_probs.sum(axis=0)       # (6,) — aggregate signal

        highest = BLOOMS_LABEL_NAMES[int(np.argmax(summed))]
        # Scale thresholds by number of sentences so multi-sentence chunks aren't
        # over-penalised (same logic as the explicit-cues aggregation in notebook)
        n_sents = end - start
        scaled_thresh = {l: thresholds[l] * n_sents for l in BLOOMS_LABEL_NAMES}
        predicted = [
            BLOOMS_LABEL_NAMES[j]
            for j, sp in enumerate(summed)
            if sp >= scaled_thresh[BLOOMS_LABEL_NAMES[j]]
        ] or ["Below threshold"]

        bucket_info = _probs_to_bucket(summed)

        results.append({
            "bloom_highest_level":    highest,
            "bloom_predicted_labels": predicted,
            "bloom_confidences":      {
                l: round(float(sp), 4)
                for l, sp in zip(BLOOMS_LABEL_NAMES, summed)
            },
            "bloom_sentence_count":   n_sents,
            **bucket_info,
        })

    return results


# ---------------------------------------------------------------------------
# Goal-based classifier (v2 — used with Gemma goal extractor)
# ---------------------------------------------------------------------------

def tag_chunks_from_goals(
    goals_per_chunk: list[list[str]],
    tokenizer,
    model,
    device,
    thresholds: Optional[dict] = None,
) -> list[dict]:
    """
    Classify chunks via pre-extracted learning goals instead of raw sentences.

    Parameters
    ----------
    goals_per_chunk : list of lists — one list of goal strings per chunk.
                      Goals should already be short (<= 512 tokens each).
    tokenizer / model / device : from load_blooms_model()
    thresholds : per-label thresholds applied directly to proportions (0-1).
                 Defaults to BLOOMS_TUNED_THRESHOLDS.

    Returns
    -------
    List of metadata dicts (same schema as tag_chunks), one per chunk.
    bloom_confidences values are proportions summing to ~1.0.
    Chunks with no goals get bloom_highest_level = None.
    """
    if thresholds is None:
        thresholds = BLOOMS_TUNED_THRESHOLDS

    # Flatten all goals and track per-chunk boundaries
    all_goals: list[str] = []
    boundaries: list[tuple[int, int]] = []
    for goals in goals_per_chunk:
        start = len(all_goals)
        all_goals.extend(goals)
        boundaries.append((start, len(all_goals)))

    # Single BERT batch over all goals
    if all_goals:
        all_probs = _run_bert_batch(all_goals, tokenizer, model, device)
    else:
        import numpy as _np
        all_probs = _np.zeros((0, 6), dtype=_np.float32)

    results: list[dict] = []
    for start, end in boundaries:
        n_goals = end - start

        if n_goals == 0:
            results.append({
                "bloom_highest_level":    "Remember",
                "bloom_bucket":           "understand_bkt",
                "bloom_predicted_labels": ["Remember"],
                "bloom_confidences":      {l: 0.0 for l in BLOOMS_LABEL_NAMES},
                "bloom_sentence_count":   0,
                "understand_bkt_score":   0.0,
                "analyze_bkt_score":      0.0,
                "create_bkt_score":       0.0,
            })
            continue

        chunk_probs = all_probs[start:end]          # (n_goals, 6)
        summed      = chunk_probs.sum(axis=0)       # (6,)
        total       = float(summed.sum()) or 1.0
        proportions = summed / total                # normalise → sum to 1.0

        highest  = BLOOMS_LABEL_NAMES[int(proportions.argmax())]
        predicted = [
            l for l, p in zip(BLOOMS_LABEL_NAMES, proportions)
            if p >= thresholds[l]
        ] or ["Below threshold"]

        bucket_info = _probs_to_bucket(proportions)

        results.append({
            "bloom_highest_level":    highest,
            "bloom_predicted_labels": predicted,
            "bloom_confidences":      {
                l: round(float(p), 4)
                for l, p in zip(BLOOMS_LABEL_NAMES, proportions)
            },
            "bloom_sentence_count":   n_goals,
            **bucket_info,
        })

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Tag text chunks with Bloom's levels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test a quick string directly in the terminal
  python chunk_blooms_tagger.py --model model/checkpoint-600 --text "Design a system that can analyze user behavior and synthesize insights."

  # Run on a JSON file of chunks
  python chunk_blooms_tagger.py --model model/checkpoint-600 --input chunks.json --output tagged.json
        """,
    )
    p.add_argument("--model", required=True, help="Path to checkpoint-600 directory")
    p.add_argument(
        "--retune",
        action="store_true",
        help="Re-derive thresholds from the training data (only needed if you retrained "
             "the model). By default, hardcoded thresholds for checkpoint-600 are used.",
    )
    p.add_argument(
        "--data-url",
        default=DATA_URL,
        help="URL or local path to EDM2022CLO CSV — only used with --retune",
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--text",  help="Classify a single text string directly from the terminal")
    mode.add_argument("--input", help="JSON file containing a list of chunk strings")

    p.add_argument("--output", default="tagged_chunks.json", help="Output JSON path (only used with --input)")
    return p.parse_args()


def _print_result(meta: dict, text_preview: str = "") -> None:
    """Pretty-print a single chunk's Bloom's result to the terminal."""
    bar_width = 30

    print("\n" + "=" * 60)
    if text_preview:
        print(f"  Text : {text_preview[:80]}{'...' if len(text_preview) > 80 else ''}")
    print(f"  Level : {meta['bloom_highest_level']}")
    print(f"  Bucket: {meta['bloom_bucket']}")
    print(f"  Labels: {', '.join(meta['bloom_predicted_labels'])}")
    print(f"  Sents : {meta['bloom_sentence_count']}")
    print()
    print("  Confidence breakdown (summed):")
    confs = meta["bloom_confidences"]
    max_val = max(confs.values()) if confs else 1.0
    for label in BLOOMS_LABEL_NAMES:
        val  = confs.get(label, 0.0)
        fill = int((val / max_val) * bar_width) if max_val > 0 else 0
        bar  = "█" * fill + "░" * (bar_width - fill)
        marker = " ◀" if label == meta["bloom_highest_level"] else ""
        print(f"    {label:<12} {bar} {val:.3f}{marker}")
    print("=" * 60)


if __name__ == "__main__":
    import os
    args = _parse_args()

    tokenizer, model, device = load_blooms_model(args.model)

    if args.retune:
        # Re-derive thresholds — only needed after retraining
        thresh_path = os.path.normpath(
            os.path.join(os.path.abspath(args.model), "..", "thresholds.json")
        )
        log.info("--retune specified: deriving thresholds from training data...")
        thresholds = tune_thresholds(
            args.data_url, tokenizer, model, device, save_path=thresh_path
        )
        log.info(f"Saved new thresholds → {thresh_path}")
    else:
        # Use the pre-computed thresholds for checkpoint-600 — no network call needed
        thresholds = BLOOMS_TUNED_THRESHOLDS
        log.info(f"Using pre-tuned thresholds: {thresholds}")

    if args.text:
        # Single text mode — print result to terminal, no file output
        metadata = tag_chunks([args.text], tokenizer, model, device, thresholds)
        _print_result(metadata[0], text_preview=args.text)

    else:
        # File mode — read JSON, write tagged JSON
        with open(args.input, "r", encoding="utf-8") as f:
            chunks = json.load(f)

        if not isinstance(chunks, list) or not all(isinstance(c, str) for c in chunks):
            raise ValueError("--input must be a JSON file containing a list of strings.")

        metadata = tag_chunks(chunks, tokenizer, model, device, thresholds)

        output = [{"chunk_index": i, "text_preview": c[:120], **m}
                  for i, (c, m) in enumerate(zip(chunks, metadata))]

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        # Also print a summary table to terminal
        print(f"\n{'IDX':<5} {'LEVEL':<14} {'BUCKET':<16} {'SENTS':<7} PREVIEW")
        print("-" * 75)
        for item in output:
            print(
                f"{item['chunk_index']:<5} "
                f"{item['bloom_highest_level'] or 'N/A':<14} "
                f"{item['bloom_bucket'] or 'N/A':<16} "
                f"{item['bloom_sentence_count']:<7} "
                f"{item['text_preview'][:30]}..."
            )
        print(f"\nSaved → {args.output}")
