"""
webapp/api/main_agent.py — MAIN_LLM answer agent.

Contains: prompts, tool schema, message builders, and the streaming
generator run_main_llm_streaming (the Finn persona / answer synthesiser).
"""

import json

from rag import _extract_json, do_rag_search
from llm_client import LLMClient

# ---------------------------------------------------------------------------
# MAIN_LLM tool definition
# ---------------------------------------------------------------------------

_MAIN_LLM_TOOLS = [
    {
        "name": "rag_search",
        "description": (
            "Search the source library using embedding similarity. "
            "Call this when wiki context is insufficient — e.g., for chapter-level "
            "detail from a book, specific passages, or topics not in the wiki. "
            "Before calling this tool, output a brief conversational line telling "
            "the user you're fetching from your library (e.g. 'Let me dig into my "
            "library for this one.' or 'My memory's a little thin here — give me a moment.'). "
            "Returns raw text chunks from the original source documents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type":        "string",
                    "description": "Search query for the source library.",
                },
                "top_k": {
                    "type":        "integer",
                    "description": "Number of chunks to retrieve. Default 5.",
                },
            },
            "required": ["query"],
        },
    }
]

# ---------------------------------------------------------------------------
# System prompt constants
# ---------------------------------------------------------------------------

_METADATA_SCHEMA = """\
{
  "sources": {
    "wiki": ["Page Title 1", "Page Title 2"],
    "rag":  ["Source Title 1"]
  },
  "new_synthesis": "Novel insight, connection, or resolved contradiction worth preserving. Empty string if none.",
  "should_wiki_update": true
}"""

_METADATA_MARKER = "\n[METADATA]\n"

_MAIN_LLM_SYSTEM_BASE = """\
You are Finn — a Finance professor with three decades of teaching experience.

## Voice & Style
- Blend personal narrative with domain principles — open with an anecdote when it adds warmth
- Mix medium sentences (15–25 words) with short, punchy declaratives
- Use em-dashes for asides—and rhetorical questions to engage
- Explain jargon naturally; favor active voice and confident phrasing
- Tone: measured optimism with a touch of wit
- Draw on specific names, numbers, and places from the knowledge base — never fabricate them
- Prefer "That's genuinely fascinating" over "*laughs* That's a great question"

## Knowledge-Source Policy (strict ladder — stop as soon as you have a solid answer)

You have three sources, ranked by trust. Escalate to the next rung only when the current one leaves a real gap for the user's question.

1. **Memory (wiki)** — the wiki context already provided in this prompt. Read it first.
   - If it answers the question well, write the answer from memory alone.
   - Do NOT call `rag_search`. Do NOT reach for general knowledge.

2. **Library (RAG)** — call `rag_search` only when memory is insufficient (missing facts, shallow coverage, off-topic, or contradicted by the user's framing).
   - Before calling, write one natural sentence telling the user you're checking — e.g. "Let me dig into my library for this." or "Give me a moment — I want to pull from the source on this."
   - Incorporate the returned passages and continue the answer.
   - If the library supplies what's needed, STOP. Do not fall back to general knowledge.

3. **General knowledge** — use only if memory AND the library both fell short for some part of the question.
   - Call out in the source-attribution block exactly which part you filled from general knowledge.
   - Never use general knowledge to polish or expand a memory/library answer that already stood on its own.

Escalation is a response to a gap, not a habit. A good memory-only answer should not grow a library call; a good library answer should not grow a general-knowledge coda.

## RAG instruction
{rag_instruction}

## Formatting
Math: use LaTeX syntax inside proper delimiters.
- Inline math: wrap in \( ... \) — e.g. \(A \cdot v = \lambda v\)
- Display math: wrap in \[ ... \] on its own line — e.g. \[A \cdot v = \lambda v\]
- Do NOT use bare parentheses or bare square brackets around math — they render as literal text.
- Do NOT use $...$ or $$...$.

## Output format — EVERY RESPONSE MUST END WITH A METADATA BLOCK

Your response has THREE parts, in this exact order. All three are mandatory. A response that omits any part is malformed and will be rejected by the pipeline.

### Part 1 — Your answer
Plain conversational text (markdown is fine). This is the substantive reply to the user.

### Part 2 — Source-attribution block
Exactly these three lines, in this order:

**My Memory:** <comma-separated wiki page titles you actually used> — or "Found nothing in my memory" if memory was inspected but unhelpful.

**My Library:** <comma-separated RAG source titles from `rag_search` results> — or "Didn't use the library" if you didn't call it — or "Found nothing in my library" if you called it and nothing was relevant.

**General Knowledge:** <one short phrase on what you filled in from general knowledge> — or "Didn't use general knowledge" if you didn't.

### Part 3 — Metadata block (DO NOT SKIP)
A blank line, then the literal marker `[METADATA]` on its own line, then a JSON object filling in this schema:

{metadata_schema}

The schema above is a **template showing structure** — not the metadata itself. You must emit your own filled-in JSON object after the `[METADATA]` line every time.

Field rules:
- `sources.wiki`: titles of wiki pages you actually drew on (empty list `[]` if none).
- `sources.rag`: source titles returned by `rag_search` that you actually used (empty list `[]` if not called or not used).
- `should_wiki_update`: `true` when you synthesised a non-obvious connection, resolved a contradiction, or produced a novel framing worth preserving; `false` otherwise.
- `new_synthesis`: 1-2 sentences capturing that insight, or `""` if none.

### Worked example of a complete, correctly-shaped response

(Answer text here — one or more paragraphs of conversational prose responding to the user's question.)

**My Memory:** Microequity, Costly State Verification
**My Library:** Didn't use the library
**General Knowledge:** Didn't use general knowledge

[METADATA]
{{"sources": {{"wiki": ["Microequity", "Costly State Verification"], "rag": []}}, "new_synthesis": "", "should_wiki_update": false}}

Every one of your responses must end in this exact shape: answer → three attribution lines → `[METADATA]` → filled JSON. If you find yourself about to stop after the attribution lines, you are not done — emit the metadata block and then stop.
"""

_RAG_INSTRUCTION_SUFFICIENT = (
    "The wiki context looks **complete** for this query. "
    "Answer from memory only — do NOT call `rag_search`, and do NOT reach for general knowledge."
)

_RAG_INSTRUCTION_INSUFFICIENT = (
    "The wiki context looks **incomplete** for this query. "
    "Follow the escalation ladder: announce the library check in one natural sentence, call `rag_search`, "
    "incorporate the results, and continue the answer. Only if the library also falls short should you use "
    "general knowledge — and when you do, name exactly which part of the answer it covers in the "
    "source-attribution block."
)

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_main_llm_system(sufficient: bool) -> str:
    rag_instruction = _RAG_INSTRUCTION_SUFFICIENT if sufficient else _RAG_INSTRUCTION_INSUFFICIENT
    return _MAIN_LLM_SYSTEM_BASE.format(
        rag_instruction=rag_instruction,
        metadata_schema=_METADATA_SCHEMA,
    )


def _build_wiki_messages(wiki_context: list, wiki_note: str, user_query: str) -> list:
    """Format wiki pages + query into the messages list for MAIN_LLM."""
    wiki_text = ""
    for p in wiki_context:
        wiki_text += f"\n{'='*60}\n{p.get('title', p.get('slug', ''))}\n{'='*60}\n"
        wiki_text += p.get("content", "")
        wiki_text += "\n"
    if wiki_note:
        wiki_text += f"\n[Note: {wiki_note}]\n"
    return [{"role": "user", "content": f"Wiki context:\n{wiki_text}\n\nQuestion: {user_query}"}]

# ---------------------------------------------------------------------------
# run_main_llm_streaming — MAIN_LLM streaming answer generator
# ---------------------------------------------------------------------------

def run_main_llm_streaming(
    user_query:   str,
    wiki_context: list,
    wiki_note:    str,
    sufficient:   bool,
    chunks:       list,
    faiss_index,
    client:       LLMClient,
):
    """
    Streaming generator for MAIN_LLM (answer agent).

    Yields:
        ("text", str)      — conversational answer chunks to stream to the user
        ("metadata", dict) — parsed metadata JSON (internal; triggers wiki update)
    """
    system   = _build_main_llm_system(sufficient)
    messages = _build_wiki_messages(wiki_context, wiki_note, user_query)

    _MAX_RAG_CALLS  = 2
    _rag_calls_made = 0
    _BARE_MARKER    = "[METADATA]"
    tail_buffer     = ""
    metadata_mode   = False
    metadata_buf    = ""
    full_response   = ""
    rag_sources_used: list = []

    for _ in range(_MAX_RAG_CALLS + 1):
        current_tool_list = _MAIN_LLM_TOOLS if _rag_calls_made < _MAX_RAG_CALLS else None
        final_response    = None

        for event, payload in client.stream_text(
            system=system,
            messages=messages,
            tools=current_tool_list,
            max_tokens=4096,
        ):
            if event == "text":
                text_chunk     = payload
                full_response += text_chunk

                if metadata_mode:
                    metadata_buf += text_chunk
                    continue

                tail_buffer += text_chunk

                marker_hit = None
                for marker in (_METADATA_MARKER, _BARE_MARKER):
                    if marker in tail_buffer:
                        marker_hit = marker
                        break

                if marker_hit:
                    before, _, after = tail_buffer.partition(marker_hit)
                    if before:
                        yield ("text", before)
                    metadata_mode = True
                    metadata_buf  = after
                    tail_buffer   = ""
                else:
                    safe_len = max(0, len(tail_buffer) - len(_METADATA_MARKER))
                    if safe_len > 0:
                        yield ("text", tail_buffer[:safe_len])
                        tail_buffer = tail_buffer[safe_len:]

            elif event == "final":
                final_response = payload

        if final_response.stop_reason != "tool_use":
            if not metadata_mode and tail_buffer:
                yield ("text", tail_buffer)
                tail_buffer = ""
            break

        # Execute rag_search tool call(s), then resume streaming
        tool_calls_to_run = []
        results           = []
        for tc in final_response.tool_calls:
            if tc.name == "rag_search":
                print(f"[MainLLM] rag_search({tc.input.get('query')!r})")
                rag_results = do_rag_search(
                    query=tc.input.get("query", user_query),
                    chunks=chunks,
                    faiss_index=faiss_index,
                    top_k=tc.input.get("top_k", 7),
                )
                _rag_calls_made += 1
                for r in rag_results:
                    src = r.get("source", "")
                    if src and src not in rag_sources_used:
                        rag_sources_used.append(src)
                tool_calls_to_run.append(tc)
                results.append(json.dumps(rag_results, ensure_ascii=False))

        safe_len = max(0, len(tail_buffer) - len(_METADATA_MARKER))
        if safe_len > 0:
            yield ("text", tail_buffer[:safe_len])
            tail_buffer = tail_buffer[safe_len:]

        client.append_assistant_turn(messages, final_response)
        client.append_tool_results(messages, tool_calls_to_run, results)

    # --- Metadata parsing (3-tier fallback) ---
    metadata = None
    for candidate in (metadata_buf.strip(), full_response):
        if not candidate:
            continue
        try:
            metadata = json.loads(candidate.strip())
            break
        except (json.JSONDecodeError, ValueError):
            extracted = _extract_json(candidate)
            if extracted and "sources" in extracted:
                metadata = extracted
                break

    if metadata is None:
        print(f"[MainLLM] Metadata parse failed — using synthetic metadata. Raw: {metadata_buf[:100]!r}")
        wiki_titles = [p.get("title", p.get("slug", "")) for p in wiki_context]
        metadata = {
            "sources":           {"wiki": wiki_titles, "rag": rag_sources_used},
            "new_synthesis":     "",
            "should_wiki_update": False,
        }

    yield ("metadata", metadata)
