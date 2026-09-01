/* ─────────────────────────────────────────────
   选股雷达 Web — Markdown / Mermaid 渲染
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.chatMarkdown = (() => {
  const { esc, loadScriptOnce } = window.StockRadar.utils;

  function renderMd(text) {
    if (typeof marked !== "undefined") {
      try { return marked.parse(text || ""); }
      catch { return esc(text || ""); }
    }
    return esc(text || "").replace(/\n/g, "<br>");
  }

  let _mermaidReady = false;
  async function _ensureMermaid() {
    if (_mermaidReady) return true;
    if (typeof mermaid === "undefined") {
      try {
        await loadScriptOnce("/static/lib/mermaid.min.js?v=1", "mermaid");
      } catch {
        return false;
      }
    }
    if (typeof mermaid === "undefined") return false;
    try {
      mermaid.initialize({
        startOnLoad: false,
        theme: "base",
        themeVariables: {
          primaryColor: "#176b5b",
          primaryTextColor: "#1c2430",
          primaryBorderColor: "#c8ddd6",
          lineColor: "#94a3b8",
          secondaryColor: "#f0f7f5",
          tertiaryColor: "#fafbfc",
          background: "#ffffff",
          mainBkg: "#ffffff",
          nodeBkg: "#f0f7f5",
          clusterBkg: "#fafbfc",
          titleColor: "#176b5b",
          edgeLabelBackground: "#fff",
          fontFamily: '"Microsoft YaHei", "PingFang SC", sans-serif',
        },
        flowchart: { htmlLabels: true, curve: "basis", padding: 12, nodeSpacing: 20, rankSpacing: 30 },
        securityLevel: "loose",
      });
      _mermaidReady = true;
      return true;
    } catch {}
    return false;
  }

  function _isMermaidSource(text) {
    const t = text.trim().toLowerCase();
    if (t.startsWith("flowchart") || t.startsWith("graph") || t.startsWith("sequencediagram") ||
        t.startsWith("classdiagram") || t.startsWith("statediagram") || t.startsWith("erdiagram") ||
        t.startsWith("gantt") || t.startsWith("pie") || t.startsWith("mindmap")) return true;
    const lines = text.split("\n").filter(l => l.trim() && !l.trim().startsWith("%") && !l.trim().startsWith("//"));
    if (lines.length >= 2 && /-->|\.\.-|-\.-|===|-->\{/.test(lines[0]) && /\[.*?\]|\(.*?\)|\{.*\}/.test(lines[0])) return true;
    if (/^\s*[A-Z]\[/.test(lines[0])) return true;
    return false;
  }

  async function _renderMermaidBlocks(container) {
    const preBlocks = container.querySelectorAll("pre");
    const targets = [];
    for (const pre of preBlocks) {
      const code = pre.querySelector("code");
      if (!code) continue;
      const lang = code.className.replace("language-", "").replace("hljs language-", "");
      const source = code.textContent.trim();

      const isMermaid = lang === "mermaid" || (!lang && _isMermaidSource(source));
      if (!isMermaid) continue;
      targets.push({ pre, source });
    }
    if (!targets.length) return;
    if (!await _ensureMermaid()) return;

    for (const { pre, source } of targets) {
      try {
        const id = `mermaid-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const { svg } = await mermaid.render(id, source);
        const viewer = document.createElement("div");
        viewer.className = "mermaid-viewer";
        viewer.innerHTML = `
          <div class="mermaid-toolbar">
            <button class="m-tool-btn m-zoom-out" title="缩小"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="8" y1="11" x2="14" y2="11"/></svg></button>
            <span class="m-zoom-level">100%</span>
            <button class="m-tool-btn m-zoom-in" title="放大"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg></button>
            <button class="m-tool-btn m-reset" title="重置"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg></button>
          </div>
          <div class="mermaid-viewport">
            <div class="mermaid-canvas">${svg}</div>
          </div>`;
        pre.replaceWith(viewer);

        const canvas = viewer.querySelector(".mermaid-canvas");
        const viewport = viewer.querySelector(".mermaid-viewport");
        const svgEl = canvas.querySelector("svg");
        const levelEl = viewer.querySelector(".m-zoom-level");

        let scale = 1;
        let defaultScale = 0.65;
        let panX = 0, panY = 0;
        let isDragging = false;
        let dragStartX, dragStartY;
        const MIN_SCALE = 0.15, MAX_SCALE = 4;

        function applyTransform() {
          canvas.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
          levelEl.textContent = Math.round(scale * 100) + "%";
        }
        applyTransform();

        viewer.querySelector(".m-zoom-in").onclick = (e) => {
          e.stopPropagation(); scale = Math.min(MAX_SCALE, scale * 1.35); applyTransform();
        };
        viewer.querySelector(".m-zoom-out").onclick = (e) => {
          e.stopPropagation(); scale = Math.max(MIN_SCALE, scale / 1.35); applyTransform();
        };
        viewer.querySelector(".m-reset").onclick = (e) => {
          e.stopPropagation(); scale = defaultScale; panX = 0; panY = 0; applyTransform();
        };

        viewport.addEventListener("wheel", (e) => {
          e.preventDefault();
          const rect = viewport.getBoundingClientRect();
          const mx = e.clientX - rect.left - rect.width / 2;
          const my = e.clientY - rect.top - rect.height / 2;
          const delta = e.deltaY > 0 ? 0.88 : 1.14;
          const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale * delta));
          panX -= mx * (newScale - scale); panY -= my * (newScale - scale);
          scale = newScale; applyTransform();
        }, { passive: false });

        viewport.addEventListener("mousedown", (e) => { if (e.button === 0 && !e.target.closest(".m-tool-btn")) { isDragging = true; dragStartX = e.clientX - panX; dragStartY = e.clientY - panY; viewport.style.cursor = "grabbing"; } });
        window.addEventListener("mousemove", (e) => { if (!isDragging) return; panX = e.clientX - dragStartX; panY = e.clientY - dragStartY; applyTransform(); });
        window.addEventListener("mouseup", () => { isDragging = false; viewport.style.cursor = ""; });

        svgEl.style.maxWidth = "none";
        svgEl.style.height = "auto";

        requestAnimationFrame(() => {
          const svgW = svgEl.viewBox?.baseVal?.width || svgEl.getBoundingClientRect().width;
          const svgH = svgEl.viewBox?.baseVal?.height || svgEl.getBoundingClientRect().height;
          const vpW = viewport.clientWidth - 40;
          const vpH = viewport.clientHeight - 40;

          if (svgW > 0 && svgH > 0 && vpW > 0 && vpH > 0) {
            const fitScaleX = vpW / svgW;
            const fitScaleY = vpH / svgH;
            let autoScale = Math.min(fitScaleX, fitScaleY);
            if (autoScale > 1) autoScale = Math.min(autoScale, 1.2);
            if (autoScale < 0.65) autoScale = 0.65;
            scale = autoScale;
            defaultScale = autoScale;
          } else {
            scale = 0.7;
          }
          applyTransform();
        });
      } catch {
        // 清理 mermaid 可能注入的错误 DOM 元素
        document.querySelectorAll('.error-icon, .error-text').forEach(el => {
          if (!el.closest('.msg-md, .md-content, .mermaid-viewer')) el.remove();
        });
      }
    }
  }

  async function _highlightCodeBlocks(container) {
    const blocks = container.querySelectorAll("pre code:not(.language-mermaid)");
    if (!blocks.length) return;
    if (typeof hljs === "undefined") {
      try {
        await loadScriptOnce("/static/lib/highlight.min.js?v=1", "hljs");
      } catch {
        return;
      }
    }
    if (typeof hljs === "undefined") return;
    blocks.forEach((block) => {
      try { hljs.highlightElement(block); } catch {}
    });
  }

  return { renderMd, _mermaidReady, _ensureMermaid, _isMermaidSource, _renderMermaidBlocks, _highlightCodeBlocks };
})();
