/* ─────────────────────────────────────────────
   选股雷达 Web — 图片导出
   能力：
   1. 复制为图片（原有，剪贴板优先）
   2. 单张高清导出（1080px 目标宽度，投稿清晰）
   3. 内容感知分割导出（按段落/表格/代码块等整块切分，不切断内容，
      9:16 竖屏比例适配手机，多张存文件夹或 zip）
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.chatExport = (() => {
  const { loadScriptOnce } = window.StockRadar.utils;

  // 导出目标输出宽度：1080px（抖音/手机全屏宽度，清晰且体积适中）
  const TARGET_WIDTH = 1080;
  // 单张图宽高比上限（高/宽）：16/9 ≈ 1.778，即 9:16 竖屏，手机一屏看全貌
  const MAX_RATIO = 16 / 9;
  // canvas 单边上限保护（Chrome 单边上限 32767，留内存余量）
  const MAX_CANVAS_SIDE = 8192;

  const JSZIP_CDN = "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js";

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* ── 命名：优先取该条消息对应的用户问题，回退对话标题，最终 {问题|标题}_{YYYYMMDD} ── */
  function getDialogBaseName(bubble) {
    let name = "";

    // 1. 从当前 AI 消息向上回溯最近一条用户消息，取其文本作为文件名
    if (bubble && bubble.closest) {
      const msgWrap = bubble.closest(".message");
      let prev = msgWrap && msgWrap.previousElementSibling;
      while (prev) {
        if (prev.classList && prev.classList.contains("message") && prev.classList.contains("user")) {
          const el = prev.querySelector(".bubble") || prev;
          const text = (el.textContent || "").trim();
          if (text) { name = text; break; }
        }
        prev = prev.previousElementSibling;
      }
    }

    // 2. 回退：对话标题（过滤默认的"新对话"）
    if (!name) {
      const el = document.getElementById("dialogTitle");
      name = (el && el.textContent ? el.textContent.trim() : "") || "";
      if (!name || name === "新对话") name = "对话";
    }

    name = name
      .replace(/[\\/:*?"<>|\s]+/g, "_")
      .replace(/_+/g, "_")
      .replace(/^_+|_+$/g, "");
    if (!name) name = "对话";
    if (name.length > 40) name = name.slice(0, 40);
    const d = new Date();
    const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
    return `${name}_${ymd}`;
  }

  async function ensureHtmlToImage() {
    if (typeof htmlToImage === "undefined" || !htmlToImage.toBlob) {
      await loadScriptOnce("/static/lib/html-to-image.js?v=1", "htmlToImage");
    }
    if (typeof htmlToImage === "undefined" || !htmlToImage.toBlob) {
      throw new Error("html-to-image 未加载");
    }
  }

  async function ensureJSZip() {
    if (window.JSZip) return window.JSZip;
    await loadScriptOnce(JSZIP_CDN, "JSZip");
    if (!window.JSZip) throw new Error("JSZip 未加载");
    return window.JSZip;
  }

  /* ── 渲染 pixelRatio：目标输出宽度 + canvas 边长上限保护 ── */
  function computeRatio(contentW, contentH, opts = {}) {
    const targetW = opts.targetWidth || TARGET_WIDTH;
    const maxSide = opts.maxCanvasSide || MAX_CANVAS_SIDE;
    const ideal = targetW / Math.max(1, contentW);
    const cap = Math.min(maxSide / Math.max(1, contentW), maxSide / Math.max(1, contentH));
    if (cap >= 1) return Math.max(1, Math.min(ideal, cap));
    return cap; // 超长内容：保成功优先，牺牲部分清晰度
  }

  /* ── 渲染过滤：排除状态气泡等干扰节点 ── */
  function defaultFilter(node) {
    if (node.classList && node.classList.contains("status-bubble")) return false;
    return true;
  }

  /* ── 临时样式：防止导出时 flex 换行破坏布局（与复制为图片保持一致） ── */
  function applyExportGuardStyle() {
    const style = document.createElement("style");
    style.textContent = `
      .dash-sector-tag, .dash-strength-label, .dash-checklist-item,
      .dash-signal-tag, .dash-action-tag, .dash-sector-tab,
      .dash-signal-item, .dash-action-item, .dash-check-item {
        white-space: nowrap !important;
      }
      .dash-sector-tabs, .dash-signal-list, .dash-action-list,
      .dash-checklist {
        flex-wrap: nowrap !important;
      }
    `;
    document.head.appendChild(style);
    return style;
  }

  /* ── 复制为图片（原有逻辑保留） ── */
  async function bubbleToPngBlob(bubble) {
    await ensureHtmlToImage();
    const style = applyExportGuardStyle();
    try {
      return await htmlToImage.toBlob(bubble, {
        backgroundColor: "#ffffff",
        pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
        cacheBust: true,
        filter: defaultFilter,
      });
    } finally {
      document.head.removeChild(style);
    }
  }

  async function copyBubbleAsImage(bubble) {
    const blob = await bubbleToPngBlob(bubble);
    if (!blob) throw new Error("图片生成失败");

    if (navigator.clipboard && navigator.clipboard.write && typeof ClipboardItem !== "undefined") {
      try {
        await navigator.clipboard.write([
          new ClipboardItem({ "image/png": blob }),
        ]);
        return "clipboard";
      } catch {}
    }

    downloadBlob(blob, `stock-radar-${Date.now()}.png`);
    return "download";
  }

  /* ── 单张高清导出：整条消息一张图，1080px 宽，保存为 {base}.png ── */
  async function exportBubbleAsImage(bubble, opts = {}) {
    await ensureHtmlToImage();
    const w = bubble.clientWidth || bubble.offsetWidth;
    const h = bubble.scrollHeight || bubble.offsetHeight;
    const ratio = computeRatio(w, h, opts);
    const style = applyExportGuardStyle();
    try {
      const blob = await htmlToImage.toBlob(bubble, {
        backgroundColor: "#ffffff",
        pixelRatio: ratio,
        cacheBust: true,
        filter: defaultFilter,
      });
      if (!blob) throw new Error("图片生成失败");
      const base = opts.baseName || getDialogBaseName(bubble);
      downloadBlob(blob, `${base}.png`);
      return { mode: "single", count: 1, blob };
    } finally {
      document.head.removeChild(style);
    }
  }

  /* ── 内容感知分割 ──
     收集 bubble 内不可切割的内容块：
     - .md-content 的直接块级子元素（段落/标题/列表/表格/代码块/引用/图表卡片…）
     - dashboard 卡片等其他直接子元素整体作为一个块 */
  function collectBlocks(bubble) {
    const blocks = [];
    const push = (el) => {
      if (!el || el.nodeType !== 1) return;
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") return;
      const rect = el.getBoundingClientRect();
      if (!rect.width || rect.height < 0.5) return;
      blocks.push({ el, top: rect.top, h: rect.height });
    };
    for (const child of bubble.children) {
      if (child.classList && child.classList.contains("md-content")) {
        for (const inner of child.children) push(inner);
      } else {
        push(child);
      }
    }
    return blocks;
  }

  /* ── 贪心分组：每张图（高/宽）≤ maxRatio，单块超高时单独成组（不切内容） ── */
  function sliceBlocks(bubble, maxRatio = MAX_RATIO) {
    const blocks = collectBlocks(bubble);
    if (!blocks.length) return [];
    const contentW = bubble.clientWidth || bubble.offsetWidth;
    // 预留容器 padding(12*2) + border(1*2)，保证渲染后整卡高/宽 ≤ maxRatio
    const reserved = 26;
    const maxH = Math.max(120, contentW * maxRatio - reserved);
    const groups = [];
    let cur = [];
    let curH = 0;
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      const prev = blocks[i - 1];
      const gap = cur.length && prev ? Math.max(0, b.top - (prev.top + prev.h)) : 0;
      const add = b.h + gap;
      if (cur.length && curH + add > maxH) {
        groups.push(cur);
        cur = [b];
        curH = b.h;
      } else {
        cur.push(b);
        curH += add;
      }
    }
    if (cur.length) groups.push(cur);
    return groups;
  }

  /* ── 渲染一组块为 PNG blob：克隆到独立卡片容器，保持 .bubble/.md-content 样式 ── */
  async function renderGroupToBlob(bubble, group, opts = {}) {
    await ensureHtmlToImage();
    const contentW = bubble.clientWidth || bubble.offsetWidth;
    const holder = document.createElement("div");
    holder.className = "bubble export-slice";
    // border-box：width 含 padding+border，内容区与原 bubble 完全一致（排版/高度不漂移）
    // 注意：不能用负坐标离屏（left:-99999px）——html-to-image 用 <svg><foreignObject>
    // 渲染克隆节点，负坐标内容会超出 foreignObject 视口被整体裁剪成空白图。
    // 改为 fixed 定位在视口 (0,0)，z-index 压底 + pointer-events:none 保持页面不可见。
    holder.style.cssText =
      `position:fixed;left:0;top:0;width:${contentW}px;box-sizing:border-box;` +
      `box-shadow:none;margin:0;z-index:-9999;pointer-events:none;`;

    // 克隆前：收集组内所有原始 canvas（ECharts 等）。
    // 坑：cloneNode(true) 不复制 canvas 位图，html-to-image 拿到空位图 → 图表导出空白。
    // 因此克隆后用「原 canvas 快照 img」替换克隆出的空 canvas。
    const srcCanvases = [];
    for (const item of group) {
      if (item.el.querySelectorAll) {
        item.el.querySelectorAll("canvas").forEach((c) => srcCanvases.push(c));
      }
    }

    let mdBox = null;
    for (const item of group) {
      if (item.el.parentElement && item.el.parentElement.classList.contains("md-content")) {
        if (!mdBox) {
          mdBox = document.createElement("div");
          mdBox.className = "md-content";
          holder.appendChild(mdBox);
        }
        mdBox.appendChild(item.el.cloneNode(true));
      } else {
        holder.appendChild(item.el.cloneNode(true));
      }
    }
    document.body.appendChild(holder);

    // 关键：克隆节点插入文档后，CSS 入场动画会重新播放（如 .dashboard-card 的
    // dashCardIn，from opacity:0 / translateY(16px)），截图瞬间卡片还在透明起始帧
    // → 仪表盘整卡空白。统一禁用克隆树内所有动画，让元素直接呈现最终静态样式。
    holder.style.animation = "none";
    holder.querySelectorAll("*").forEach((el) => { el.style.animation = "none"; });

    // 用原 canvas 的像素快照替换克隆出的空 canvas（文档顺序一一对应）
    const clonedCanvases = Array.from(holder.querySelectorAll("canvas"));
    clonedCanvases.forEach((ccv, i) => {
      const scv = srcCanvases[i];
      if (!scv) return;
      try {
        const url = scv.toDataURL("image/png");
        if (!url || url === "data:,") return;
        const img = document.createElement("img");
        img.src = url;
        img.style.cssText = ccv.getAttribute("style") || "";
        img.style.maxWidth = "100%";
        img.style.display = ccv.style.display || "block";
        ccv.replaceWith(img);
      } catch (err) {
        console.warn("[chatExport] 图表画布固化失败:", err);
      }
    });

    const style = applyExportGuardStyle();
    try {
      // 插入后读取真实排版尺寸（getBoundingClientRect/scrollHeight 触发 reflow）
      const w = holder.clientWidth || contentW;
      const h = holder.scrollHeight || 0;
      const ratio = computeRatio(w, h, opts);
      const blob = await htmlToImage.toBlob(holder, {
        backgroundColor: "#ffffff",
        pixelRatio: ratio,
        cacheBust: true,
        filter: defaultFilter,
      });
      if (!blob) throw new Error("图片生成失败");
      return blob;
    } finally {
      holder.remove();
      document.head.removeChild(style);
    }
  }

  /* ── 保存多张图：
     1 张 → 单文件 {base}.png
     多张 → 优先真文件夹（File System Access API，写入 {base}/ 目录）
            回退 zip（{base}.zip，内含 {base}_N.png）
            再回退逐张下载 */
  async function saveSlices(base, blobs) {
    if (blobs.length === 1) {
      downloadBlob(blobs[0], `${base}.png`);
      return { mode: "single", count: 1 };
    }
    if (window.showDirectoryPicker) {
      try {
        const dirHandle = await window.showDirectoryPicker({ mode: "readwrite" });
        const sub = await dirHandle.getDirectoryHandle(base, { create: true });
        for (let i = 0; i < blobs.length; i++) {
          const fh = await sub.getFileHandle(`${base}_${i + 1}.png`, { create: true });
          const w = await fh.createWritable();
          await w.write(blobs[i]);
          await w.close();
        }
        return { mode: "dir", count: blobs.length };
      } catch (err) {
        if (err && err.name === "AbortError") throw err; // 用户取消选择文件夹
        console.warn("[chatExport] 文件夹保存失败，回退 zip:", err);
      }
    }
    try {
      const JSZip = await ensureJSZip();
      const zip = new JSZip();
      const folder = zip.folder(base);
      blobs.forEach((blob, i) => folder.file(`${base}_${i + 1}.png`, blob));
      const blob = await zip.generateAsync({ type: "blob" });
      downloadBlob(blob, `${base}.zip`);
      return { mode: "zip", count: blobs.length };
    } catch (err) {
      console.warn("[chatExport] zip 打包失败，逐张下载:", err);
      for (let i = 0; i < blobs.length; i++) {
        downloadBlob(blobs[i], `${base}_${i + 1}.png`);
        await new Promise(r => setTimeout(r, 300)); // 错开下载，避免浏览器拦截
      }
      return { mode: "multi", count: blobs.length };
    }
  }

  /* ── 分割导出入口：内容感知切分 → 逐组高清渲染 → 保存 ── */
  async function exportBubbleSliced(bubble, opts = {}) {
    const groups = sliceBlocks(bubble, opts.maxRatio);
    if (!groups.length) {
      // 空内容兜底：退化为单张导出
      return exportBubbleAsImage(bubble, opts);
    }
    const base = opts.baseName || getDialogBaseName(bubble);
    const n = groups.length;
    const blobs = [];
    for (let i = 0; i < n; i++) {
      if (opts.onProgress) opts.onProgress(i + 1, n);
      blobs.push(await renderGroupToBlob(bubble, groups[i], opts));
    }
    if (opts.onProgress) opts.onProgress(n, n);
    const result = await saveSlices(base, blobs);
    return Object.assign({ groupCount: n }, result);
  }

  return {
    downloadBlob,
    bubbleToPngBlob,
    copyBubbleAsImage,
    exportBubbleAsImage,
    exportBubbleSliced,
    getDialogBaseName,
    collectBlocks,
    sliceBlocks,
    computeRatio,
  };
})();
