/**
 * Prof. Bhagwan Chowdhry — Knowledge Chatbot
 * Smooth streaming with token queue, debounced markdown, KaTeX math
 */

const chatContainer = document.getElementById("chat-container");
const messageInput = document.getElementById("message-input");
const sendBtn = document.getElementById("send-btn");
const statusText = document.getElementById("status-text");
const statusDot = document.querySelector(".status-dot");
const themeToggle = document.getElementById("theme-toggle");

let activeWikiPropose = null;

// ---- Theme Logic ----
const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const storedTheme = localStorage.getItem('theme');
if (storedTheme === 'dark' || (!storedTheme && prefersDark)) {
  document.documentElement.classList.add('dark-mode');
}

themeToggle.addEventListener('click', () => {
  document.documentElement.classList.toggle('dark-mode');
  const isDark = document.documentElement.classList.contains('dark-mode');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
});

let conversationHistory = [];
let isStreaming = false;
let attachedPDF = null; // { name, base64 }

// ---- PDF helpers ----
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload  = e => resolve(e.target.result.split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

const PDF_MAX_BYTES = 15 * 1024 * 1024; // 15 MB — base64 expansion stays under 32 MB Cloud Run limit

function attachPDF(file) {
  if (!file || file.type !== "application/pdf") return;
  if (file.size > PDF_MAX_BYTES) {
    alert(`PDF too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Maximum is 15 MB.`);
    return;
  }
  readFileAsBase64(file).then(b64 => {
    attachedPDF = { name: file.name, base64: b64 };
    renderPDFChip();
  });
}

function renderPDFChip() {
  removePDFChip();
  if (!attachedPDF) return;
  const inputWrapper = messageInput.closest(".input-wrapper");
  const chip = document.createElement("div");
  chip.className = "pdf-chip";
  chip.id = "pdf-chip";
  chip.innerHTML = `<span>📄 ${attachedPDF.name}</span><button type="button" aria-label="Remove PDF">×</button>`;
  chip.querySelector("button").onclick = () => { attachedPDF = null; removePDFChip(); };
  inputWrapper.appendChild(chip);
}

function removePDFChip() {
  document.getElementById("pdf-chip")?.remove();
}

marked.setOptions({ breaks: true, gfm: true });

function stripWikiBlocks(text) {
  return text.replace(/<wiki_update>[\s\S]*?<\/wiki_update>/g, "").trimEnd();
}

// Math-safe markdown rendering:
// 1. Extract math blocks ($$...$$ and $...$) and replace with placeholders
// 2. Run marked on the math-free text
// 3. Re-insert the math blocks
// 4. Run KaTeX on the result
// This prevents marked from eating backslashes in LaTeX.

function renderMarkdown(el, text) {
  const mathBlocks = [];

  let safe = text;

  // Display math: $$...$$ 
  safe = safe.replace(/\$\$([\s\S]+?)\$\$/g, (_, math) => {
    const id = `%%MATH_${mathBlocks.length}%%`;
    mathBlocks.push({ id, math: math.trim(), display: true });
    return id;
  });

  // Display math: \[...\]
  safe = safe.replace(/\\\[([\s\S]+?)\\\]/g, (_, math) => {
    const id = `%%MATH_${mathBlocks.length}%%`;
    mathBlocks.push({ id, math: math.trim(), display: true });
    return id;
  });

  // Inline math: \(...\) ONLY — no $...$ to avoid matching dollar amounts
  safe = safe.replace(/\\\(([\s\S]+?)\\\)/g, (_, math) => {
    const id = `%%MATH_${mathBlocks.length}%%`;
    mathBlocks.push({ id, math: math.trim(), display: false });
    return id;
  });

  let html = marked.parse(safe);

  for (const block of mathBlocks) {
    let rendered;
    if (typeof katex !== "undefined") {
      try {
        rendered = katex.renderToString(block.math, {
          displayMode: block.display,
          throwOnError: false,
        });
      } catch {
        rendered = `<span class="math-fallback">${block.display ? "\\[" : "\\("}${block.math}${block.display ? "\\]" : "\\)"}</span>`;
      }
    } else {
      rendered = `<span class="math-fallback">${block.display ? "\\[" : "\\("}${block.math}${block.display ? "\\]" : "\\)"}</span>`;
    }
    html = html.replace(block.id, rendered);
  }

  el.innerHTML = html;
}

// ---- Textarea auto-resize ----
messageInput.addEventListener("input", () => {
  messageInput.style.height = "auto";
  messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
});

messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!isStreaming) handleSubmit();
  }
});

// ---- PDF drag-and-drop on input area ----
const inputWrapper = messageInput.closest(".input-wrapper");
inputWrapper.addEventListener("dragover", e => { e.preventDefault(); inputWrapper.classList.add("drag-over"); });
inputWrapper.addEventListener("dragleave", () => inputWrapper.classList.remove("drag-over"));
inputWrapper.addEventListener("drop", e => {
  e.preventDefault();
  inputWrapper.classList.remove("drag-over");
  const file = Array.from(e.dataTransfer.files).find(f => f.type === "application/pdf");
  if (file) attachPDF(file);
});

// Hidden file input triggered by a clip button in index.html
const pdfFileInput = document.getElementById("pdf-file-input");
if (pdfFileInput) {
  pdfFileInput.addEventListener("change", () => {
    if (pdfFileInput.files[0]) attachPDF(pdfFileInput.files[0]);
    pdfFileInput.value = "";
  });
}

function sendSuggestion(btn) {
  if (isStreaming) return;
  messageInput.value = btn.textContent;
  handleSubmit();
}

// ---- Submit ----
function handleSubmit(e) {
  if (e) e.preventDefault();
  const text = messageInput.value.trim();
  if (!text || isStreaming) return;

  const welcome = chatContainer.querySelector(".welcome-message");
  if (welcome) welcome.remove();

  appendMessage("user", text);
  conversationHistory.push({ role: "user", content: text });
  messageInput.value = "";
  messageInput.style.height = "auto";
  const pdfToSend = attachedPDF;
  attachedPDF = null;
  removePDFChip();
  streamResponse(text, pdfToSend);
}

// ---- Append message ----
function appendMessage(role, content) {
  const msg = document.createElement("div");
  msg.className = `message ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "user" ? "You" : "Prof";

  const bubble = document.createElement("div");
  bubble.className = "msg-body";

  if (role === "user") {
    bubble.textContent = content;
  } else if (content) {
    renderMarkdown(bubble, content);
  }

  msg.appendChild(avatar);
  msg.appendChild(bubble);
  chatContainer.appendChild(msg);
  scrollToBottom();
  return bubble;
}

// ---- Thinking indicator ----
function showThinking() {
  const msg = document.createElement("div");
  msg.className = "message bot";
  msg.id = "thinking-msg";
  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = "Prof";
  const bubble = document.createElement("div");
  bubble.className = "msg-body thinking";
  bubble.innerHTML =
    '<span class="dot-pulse"><span></span><span></span><span></span></span>';
  msg.appendChild(avatar);
  msg.appendChild(bubble);
  chatContainer.appendChild(msg);
  scrollToBottom();
}

function removeThinking() {
  const el = document.getElementById("thinking-msg");
  if (el) el.remove();
}

// =====================================================
// Smooth streaming engine
// =====================================================
// Tokens arrive from SSE in bursts. Instead of rendering
// them all at once (chunky), we queue them and drain at
// a steady rate using requestAnimationFrame. Markdown is
// re-rendered at most every ~80ms (debounced).
// =====================================================

function hasOpenMath(text) {
  // Check for unclosed $$
  const displayCount = (text.match(/\$\$/g) || []).length;
  if (displayCount % 2 !== 0) return true;

  // Check for unclosed \[
  const openDisplay = (text.match(/\\\[/g) || []).length;
  const closeDisplay = (text.match(/\\\]/g) || []).length;
  if (openDisplay !== closeDisplay) return true;

  // Check for unclosed \(
  const openInline = (text.match(/\\\(/g) || []).length;
  const closeInline = (text.match(/\\\)/g) || []).length;
  if (openInline !== closeInline) return true;

  return false;
}

function createStreamRenderer(bubble, onFinished) {
  let tokenQueue = "";
  let revealedText = "";
  let drainRAF = null;
  let renderTimer = null;
  let finished = false;

  function scheduleRender() {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      const clean = stripWikiBlocks(revealedText);
      if (clean && !hasOpenMath(clean)) {
        renderMarkdown(bubble, clean);
        scrollToBottom();
      }
    }, 80);
  }

  function drain() {
    if (tokenQueue.length === 0) {
      drainRAF = null;
      if (finished) finalRender();
      return;
    }

    const chars = Math.min(Math.max(1, Math.ceil(tokenQueue.length / 8)), 12);
    revealedText += tokenQueue.slice(0, chars);
    tokenQueue = tokenQueue.slice(chars);

    scheduleRender();
    drainRAF = requestAnimationFrame(drain);
  }

  function finalRender() {
    if (renderTimer) {
      clearTimeout(renderTimer);
      renderTimer = null;
    }
    bubble.classList.remove("streaming");
    const clean = stripWikiBlocks(revealedText);
    if (clean) {
      renderMarkdown(bubble, clean);
      scrollToBottom();
    }
    // Called after innerHTML is written — safe to append to the DOM now.
    if (onFinished) onFinished();
  }

  return {
    push(text) {
      tokenQueue += text;
      if (!drainRAF) drainRAF = requestAnimationFrame(drain);
    },
    finish() {
      finished = true;
      if (tokenQueue.length === 0 && !drainRAF) finalRender();
    },
    getText() { return revealedText + tokenQueue; },
    getCleanText() { return stripWikiBlocks(revealedText + tokenQueue); },
    destroy() {
      if (drainRAF) cancelAnimationFrame(drainRAF);
      if (renderTimer) clearTimeout(renderTimer);
    },
  };
}


// ---- Show the Wiki Update UI ----
function showWikiPropose(messageEl, metadata, originalQuery) {
    if (activeWikiPropose) {
        activeWikiPropose.remove();
    }

    const proposeDiv = document.createElement("div");
    proposeDiv.className = "wiki-propose-area";
    proposeDiv.innerHTML = `
        <div class="wiki-propose-header">
            <span>💡 Prof. Finn suggests updating the Wiki with this synthesis.</span>
        </div>
        <div class="wiki-synthesis-preview">${metadata.new_synthesis}</div>
        <div class="wiki-actions">
            <button class="action-btn up" id="wiki-approve">👍 Approve</button>
            <button class="action-btn down" id="wiki-reject">👎 Discard</button>
        </div>
        <div class="wiki-comment-box" id="wiki-comment-area" style="display:none;">
            <input type="text" id="wiki-comment-input" placeholder="Add a comment or correction...">
            <label class="wiki-pdf-label">
                <input type="file" id="wiki-pdf-input" accept=".pdf">
                <span id="wiki-pdf-name">📎 Attach PDF (optional)</span>
            </label>
            <button id="wiki-submit-comment">Submit with Comment</button>
        </div>
    `;

    // Append to the .message wrapper, not .msg-body — survives innerHTML resets.
    messageEl.appendChild(proposeDiv);
    activeWikiPropose = proposeDiv;
    scrollToBottom();

    // Event Listeners
    const approveBtn    = proposeDiv.querySelector("#wiki-approve");
    const rejectBtn     = proposeDiv.querySelector("#wiki-reject");
    const commentArea   = proposeDiv.querySelector("#wiki-comment-area");
    const commentSubmit = proposeDiv.querySelector("#wiki-submit-comment");
    const wikiPdfInput  = proposeDiv.querySelector("#wiki-pdf-input");
    const wikiPdfName   = proposeDiv.querySelector("#wiki-pdf-name");

    wikiPdfInput.addEventListener("change", () => {
        wikiPdfName.textContent = wikiPdfInput.files[0]
            ? `📄 ${wikiPdfInput.files[0].name}`
            : "📎 Attach PDF (optional)";
    });

    approveBtn.onclick = () => {
        commentArea.style.display = "flex";
        approveBtn.classList.add("selected");
    };

    rejectBtn.onclick = () => {
        proposeDiv.innerHTML = "<p class='wiki-status'>Update discarded.</p>";
        setTimeout(() => proposeDiv.remove(), 2000);
        activeWikiPropose = null;
    };

    commentSubmit.onclick = async () => {
        const comment = proposeDiv.querySelector("#wiki-comment-input").value;
        const pdfFile = wikiPdfInput.files[0] || null;
        let pdfBase64 = null;
        if (pdfFile) pdfBase64 = await readFileAsBase64(pdfFile);

        commentSubmit.disabled = true;
        commentSubmit.textContent = pdfFile ? "Processing PDF…" : "Updating...";

        const success = await submitWikiUpdate(metadata, originalQuery, comment, pdfBase64);

        if (success) {
            proposeDiv.innerHTML = "<p class='wiki-status'>✅ Wiki updated successfully!</p>";
            setTimeout(() => proposeDiv.remove(), 3000);
        } else {
            commentSubmit.disabled = false;
            commentSubmit.textContent = "Retry";
            alert("Failed to update wiki. Check console.");
        }
        activeWikiPropose = null;
    };
}

// ---- New Function: Call the Backend Commit Endpoint ----
async function submitWikiUpdate(metadata, originalQuery, userComment, pdfBase64 = null) {
    try {
        const response = await fetch(`${window.BACKEND_URL ?? ""}/api/wiki/commit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                synthesis:     metadata.new_synthesis,
                sources:       metadata.sources,
                original_query: originalQuery,
                user_comment:  userComment,
                pdf_base64:    pdfBase64 || undefined,
            }),
        });
        return response.ok;
    } catch (err) {
        console.error("Wiki commit error:", err);
        return false;
    }
}


async function streamResponse(userMessage, pdf = null) {
    isStreaming = true;
    sendBtn.disabled = true;
    setStatus("Thinking...", true);
    showThinking();

    // Clear any previous active prompts when a new message starts
    if (activeWikiPropose) {
        activeWikiPropose.remove();
        activeWikiPropose = null;
    }

    let renderer = null;
    let finalMetadata = null; // Store metadata here

    try {
        const response = await fetch(`${window.BACKEND_URL ?? ""}/api/chat-v2`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                message:    userMessage,
                history:    conversationHistory.slice(-10),
                pdf_base64: pdf ? pdf.base64 : undefined,
            }),
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.error || `Server error (${response.status})`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let sseBuffer = "";
        let streamDone = false;

        removeThinking();
        setStatus("Responding...", true);
        const bubble = appendMessage("bot", "");
        const messageEl = bubble.parentNode; // .message wrapper — survives innerHTML resets
        bubble.classList.add("streaming");
        renderer = createStreamRenderer(bubble, () => {
            if (finalMetadata && finalMetadata.should_wiki_update) {
                showWikiPropose(messageEl, finalMetadata, userMessage);
            }
        });

        while (!streamDone) {
            const { value, done } = await reader.read();
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });
            const lines = sseBuffer.split("\n");
            sseBuffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const data = line.slice(6).trim();

                if (data === "[DONE]") {
                    streamDone = true;
                    break;
                }

                try {
                    const parsed = JSON.parse(data);
                    if (parsed.text) {
                        renderer.push(parsed.text);
                    } 
                    // CAPTURE METADATA HERE
                    else if (parsed.should_wiki_update) {
                        finalMetadata = parsed; 
                    }
                    else if (parsed.error) {
                        renderer.push(`\n\n**Error:** ${parsed.error}`);
                    }
                } catch { /* skip malformed */ }
            }
        }

        renderer.finish();

        const fullText = renderer.getCleanText();
        if (fullText.trim()) {
            conversationHistory.push({ role: "assistant", content: fullText });
        }
    } catch (err) {
        removeThinking();
        if (renderer) renderer.destroy();
        appendMessage("bot", `**Something went wrong:** ${err.message}`);
    } finally {
        isStreaming = false;
        sendBtn.disabled = false;
        setStatus("Ready", false);
        messageInput.focus();
    }
}

// ---- Helpers ----
function scrollToBottom() {
  requestAnimationFrame(() => {
    chatContainer.scrollTop = chatContainer.scrollHeight;
  });
}

function setStatus(text, thinking) {
  statusText.textContent = text;
  statusDot.classList.toggle("thinking", thinking);
}

async function checkHealth() {
  try {
    const resp = await fetch(`${window.BACKEND_URL ?? ""}/api/health`);
    const data = await resp.json();
    if (data.wiki_pages === 0 && data.rag_chunks === 0) {
      setStatus("No data loaded", false);
    }
  } catch { }
}

checkHealth();
