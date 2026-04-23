"""
Gemma 4 learning-goal extractor for Bloom's taxonomy tagging.

Downloads the GGUF model on first run (cached by llama_cpp / HuggingFace Hub).
Subsequent runs load from the local cache — no network required.

Usage — import:
    from gemma_goal_extractor import load_gemma, extract_goals

    llm   = load_gemma()
    goals = extract_goals(chunk_text, llm)
    # goals → ["Understand the concept of X", "Apply Y to solve Z", ...]

Usage — CLI (test a single chunk):
    python scripts/gemma_goal_extractor.py --text "Some chunk text here..."
    python scripts/gemma_goal_extractor.py --file path/to/chunk.txt

Flags:
    --text TEXT       Chunk text to extract goals from (quoted string)
    --file FILE       Path to a plain-text file containing the chunk
    --repo-id ID      HuggingFace repo (default: unsloth/gemma-4-E4B-it-GGUF)
    --filename FILE   GGUF filename    (default: gemma-4-E4B-it-Q4_K_M.gguf)
    --n-ctx N         Context window size (default: 8192)
    --max-goals N     Maximum goals to extract per chunk (default: 10)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an educational content analyst. Your only job is to extract "
    "learning goals or outcomes from academic text."
)

_USER_TEMPLATE = """\
Read the following text and extract the key learning goals or outcomes it \
addresses — what a student would learn, understand, apply, analyze, evaluate, \
or be able to create after studying this material.

Return ONLY a numbered list of concise learning outcome sentences (one per \
line, no headings, no extra explanation). Each sentence should start with a \
Bloom's verb (e.g. "Understand ...", "Apply ...", "Analyze ..."). \
If the text contains no clear learning goals, return exactly: NONE

Text:
{chunk_text}
"""

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IS_KAGGLE    = Path('/kaggle/input').exists()
_MODELS_DIR   = (
    Path('/kaggle/working/models') if _IS_KAGGLE
    else _PROJECT_ROOT / "webapp" / "models"
)


def load_gemma(
    repo_id: str      = "unsloth/gemma-4-E4B-it-GGUF",
    filename: str     = "gemma-4-E4B-it-Q4_K_M.gguf",
    n_ctx: int        = 8192,
    n_gpu_layers: int = -1,
):
    """Load Gemma 4 GGUF via llama_cpp. Downloads to webapp/models/ (or /kaggle/working/models/) on first run.

    n_gpu_layers=-1 offloads all layers to GPU when available; falls back to CPU automatically.
    """
    try:
        from llama_cpp import Llama
    except ImportError:
        print("Error: llama_cpp not installed. Run: pip install llama-cpp-python")
        sys.exit(1)

    local_path = _MODELS_DIR / filename
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if local_path.exists():
        print(f"[Gemma] Loading from local cache: {local_path}")
        llm = Llama(model_path=str(local_path), n_ctx=n_ctx,
                    n_gpu_layers=n_gpu_layers, verbose=False)
    else:
        print(f"[Gemma] Downloading {filename} → {_MODELS_DIR} ...")
        llm = Llama.from_pretrained(
            repo_id=repo_id,
            filename=filename,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
            local_dir=str(_MODELS_DIR),
        )
        print(f"[Gemma] Saved to {local_path}")
    print("[Gemma] Model ready.")
    return llm


# ---------------------------------------------------------------------------
# Goal extraction
# ---------------------------------------------------------------------------

def extract_goals(
    chunk_text: str,
    llm,
) -> list[str]:
    """
    Extract learning goals/outcomes from a chunk using Gemma 4.

    Returns a list of short goal strings (empty list if none found or on error).
    Each goal is suitable as direct input to the BERT Bloom's classifier.
    """
    prompt = _USER_TEMPLATE.format(chunk_text=chunk_text.strip())
    try:
        response = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=512,
            temperature=0.3,
        )
        raw = response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"\n[Gemma] Inference error: {e}")
        return []

    if raw.upper().startswith("NONE") or not raw:
        return []

    goals = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip leading numbering/bullets: "1.", "1)", "-", "*", "•"
        line = re.sub(r"^[\d]+[.)]\s*|^[-*•]\s*", "", line).strip()
        if len(line) > 5:
            goals.append(line)


    return goals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description="Extract learning goals from text using Gemma 4.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--text", metavar="TEXT", help="Chunk text (quoted string)")
    grp.add_argument("--file", metavar="FILE", help="Path to plain-text chunk file")
    p.add_argument("--repo-id",   default="unsloth/gemma-4-E4B-it-GGUF")
    p.add_argument("--filename",  default="gemma-4-E4B-it-Q4_K_M.gguf")
    p.add_argument("--n-ctx",     type=int, default=8192)
    p.add_argument("--max-goals", type=int, default=10)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.file:
        from pathlib import Path
        chunk_text = Path(args.file).read_text(encoding="utf-8")
    else:
        chunk_text = args.text

    llm   = load_gemma(repo_id=args.repo_id, filename=args.filename, n_ctx=args.n_ctx)
    goals = extract_goals(chunk_text, llm)

    print(f"\nExtracted {len(goals)} learning goal(s):")
    for i, g in enumerate(goals, 1):
        print(f"  {i}. {g}")
