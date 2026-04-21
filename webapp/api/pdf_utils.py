"""
webapp/api/pdf_utils.py — Gemini-backed PDF text extraction and summarization.

extract_and_process_pdf(pdf_bytes) -> str
  1. Sends PDF to Gemini for text extraction.
  2. If word count > WORD_LIMIT, summarizes with a second Gemini call.
"""

import os

WORD_LIMIT   = 4000
_GEMINI_MODEL = "gemini-3.0-flash-preview"


def extract_and_process_pdf(pdf_bytes: bytes) -> str:
    """
    Extract text from a PDF using Gemini document understanding.
    Summarizes automatically if the extracted text exceeds WORD_LIMIT words.
    Returns the processed text string, or raises on API error.
    """
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=key)

    print(f"[PDF] Extracting text ({len(pdf_bytes) // 1024} KB)…")
    extract_response = client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            "Extract all text from this document. Return the text only, preserving structure and headings.",
        ],
    )
    text = extract_response.text.strip()
    word_count = len(text.split())
    print(f"[PDF] Extracted {word_count} words.")

    if word_count > WORD_LIMIT:
        print(f"[PDF] Summarizing ({word_count} > {WORD_LIMIT} words)…")
        summary_response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=(
                f"Summarize the following document in under 800 words. "
                f"Preserve key claims, data, and conclusions:\n\n{text}"
            ),
        )
        text = summary_response.text.strip()
        print(f"[PDF] Summary: {len(text.split())} words.")

    return text
