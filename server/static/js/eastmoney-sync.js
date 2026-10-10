/*
   东财持仓同步弹窗（持仓面板入口按钮挂载）
   交互镜像 watchlist「MX 同步」：粘贴报文 → 解析预览 → 确认导入
   三格式自动识别：PowerShell / fetch / JSON 响应体（后端 parse_paste 判别）
   凭据安全：validatekey/Cookie 仅在本弹窗内存中流转一次（preview→import 透传
   快照，不再回传凭据），不落 localStorage、不写日志。
*/
(function () {
  "use strict";

  window.StockRadar = window.StockRadar || {};

  /* ── 样式注入（em- 前缀，避免依赖 css 文件域外改动） ── */
  const EM_CSS = `
.em-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  z-index: 1100;
}
.em-overlay.open { display: block; }
.em-modal {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: min(640px, 94vw);
  max-height: 86vh;
  display: none;
  flex-direction: column;
  background: var(--panel, #fff);
  border: 1px solid var(--line, #e5e7eb);
  border-radius: var(--radius-md, 8px);
  box-shadow: var(--shadow-lg, 0 8px 30px rgba(0,0,0,0.12));
  z-index: 1101;
  overflow: hidden;
}
.em-modal.open { display: flex; }
.em-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 18px;
  border-bottom: 1px solid var(--line, #e5e7eb);
}
.em-title { font-size: 15px; font-weight: 600; color: var(--text, #1c2430); }
.em-sub { margin-left: 8px; font-size: 11.5px; font-weight: 400; color: var(--muted, #667085); }
.em-close-btn {
  width: 28px;
  height: 28px;
  font-size: 18px;
  line-height: 1;
  color: var(--text-secondary, #475569);
  background: none;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.em-close-btn:hover { background: var(--bg, #f6f7f9); color: var(--text, #1c2430); }
.em-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px 18px 18px;
  font-size: 12px;
  color: var(--text, #1c2430);
}
.em-hint { margin-bottom: 8px; color: var(--muted, #667085); line-height: 1.6; }
.em-hint b { color: var(--text, #1c2430); font-weight: 600; }
.em-formats { display: flex; gap: 14px; margin-bottom: 8px; }
.em-formats label { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; }
.em-paste {
  width: 100%;
  box-sizing: border-box;
  min-height: 150px;
  padding: 8px 10px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 11px;
  color: var(--text, #1c2430);
  background: var(--detail-bg, #f8fafc);
  border: 1px solid var(--line-strong, #cbd5e1);
  border-radius: 4px;
  resize: vertical;
}
.em-paste:focus {
  outline: none;
  border-color: var(--brand, #176b5b);
  box-shadow: 0 0 0 2px var(--brand-glow, rgba(23, 107, 91, 0.12));
}
.em-actions { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
.em-btn {
  padding: 6px 16px;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  background: var(--brand, #176b5b);
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.em-btn:hover { background: var(--brand-hover, #145a4c); }
.em-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.em-btn-secondary {
  padding: 6px 16px;
  font-size: 12px;
  color: var(--text-secondary, #475569);
  background: none;
  border: 1px solid var(--line-strong, #cbd5e1);
  border-radius: 4px;
  cursor: pointer;
}
.em-btn-secondary:hover { color: var(--brand-strong, #176b5b); border-color: var(--brand, #176b5b); }
.em-msg { margin-left: auto; font-size: 11.5px; }
.em-msg.error { color: var(--danger, #dc2626); }
.em-msg.ok { color: var(--brand-strong, #176b5b); }
.em-section { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line, #e5e7eb); }
.em-section-title { margin-bottom: 8px; font-weight: 600; font-size: 12.5px; }
.em-account { display: flex; gap: 18px; flex-wrap: wrap; color: var(--text, #1c2430); }
.em-account span b { font-weight: 600; }
.em-diff { margin-top: 6px; color: var(--muted, #667085); }
.em-table { width: 100%; border-collapse: collapse; margin-top: 4px; }
.em-table th, .em-table td {
  padding: 5px 6px;
  text-align: left;
  border-bottom: 1px solid var(--border-light, #eef1f5);
  font-size: 11.5px;
  white-space: nowrap;
}
.em-table th { color: var(--muted, #667085); font-weight: 500; font-size: 11px; }
.em-badge {
  display: inline-block;
  padding: 0 6px;
  font-size: 10.5px;
  line-height: 16px;
  color: var(--brand-strong, #176b5b);
  background: var(--brand-light, #e6f2ef);
  border-radius: 8px;
}
.em-up { color: var(--up, #e74c3c); }
.em-down { color: var(--down, #176b5b); }
.em-skipped { margin-top: 6px; font-size: 11px; color: var(--muted, #667085); }
`;

  const EastmoneySyncDialog = class EastmoneySyncDialog {
    constructor(onImported) {
      this._onImported = onImported || null;   // 导入成功回调（刷新持仓面板）
      this._preview = null;                    // 预览结果 {snapshot, diff}
      this._styleInjected = false;
      this._buildDom();
    }

    _injectStyle() {
      if (this._styleInjected) return;
      const style = document.createElement("style");
      style.id = "emSyncStyle";
      style.textContent = EM_CSS;
      document.head.appendChild(style);
      this._styleInjected = true;
    }

    _buildDom() {
      this.overlayEl = document.createElement("div");
      this.overlayEl.className = "em-overlay";
      this.modalEl = document.createElement("div");
      this.modalEl.className = "em-modal";
      this.modalEl.setAttribute("role", "dialog");
      this.modalEl.setAttribute("aria-modal", "true");
      this.modalEl.setAttribute("aria-label", "同步东方财富持仓");
      this.modalEl.innerHTML = `
        <div class="em-header">
          <div class="em-title">同步东方财富持仓 <span class="em-sub">快照级初始化 · 凭据仅内存存活</span></div>
          <button class="em-close-btn" type="button" aria-label="关闭">×</button>
        </div>
        <div class="em-body">
          <div class="em-hint">
            在东财网页版「查询 → 持仓」页面打开 DevTools 网络面板，找到
            <b>queryAssetAndPositionV1</b> 请求，右键
            <b>Copy as fetch</b>（或用 PowerShell 抓包脚本完整复制），粘贴到下方。
            也可直接粘贴响应体 JSON。凭据 0.5~3 小时后过期需重新复制。
          </div>
          <div class="em-formats">
            <label><input type="radio" name="emFormat" value="auto" checked>自动识别</label>
            <label><input type="radio" name="emFormat" value="ps">PowerShell</label>
            <label><input type="radio" name="emFormat" value="fetch">fetch</label>
            <label><input type="radio" name="emFormat" value="json">JSON 响应体</label>
          </div>
          <textarea class="em-paste" placeholder="$session = New-Object ...&#10;或 fetch('https://jywg.18.cn/Com/queryAssetAndPositionV1?validatekey=...', {...})&#10;或直接粘贴 {&quot;Status&quot;:0,...} 响应体"></textarea>
          <div class="em-actions">
            <button class="em-btn-preview em-btn-secondary" type="button">解析并预览</button>
            <button class="em-btn-import em-btn" type="button" disabled>确认导入</button>
            <span class="em-msg"></span>
          </div>
          <div class="em-preview-area"></div>
        </div>`;
      document.body.appendChild(this.overlayEl);
      document.body.appendChild(this.modalEl);
      this._injectStyle();

      this.pasteEl = this.modalEl.querySelector(".em-paste");
      this.previewBtn = this.modalEl.querySelector(".em-btn-preview");
      this.importBtn = this.modalEl.querySelector(".em-btn-import");
      this.msgEl = this.modalEl.querySelector(".em-msg");
      this.previewAreaEl = this.modalEl.querySelector(".em-preview-area");
      this.closeBtn = this.modalEl.querySelector(".em-close-btn");

      this.closeBtn.onclick = () => this.close();
      this.overlayEl.onclick = () => this.close();
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this.modalEl.classList.contains("open")) this.close();
      });
      this.previewBtn.onclick = () => this._doPreview();
      this.importBtn.onclick = () => this._doImport();
    }

    open() {
      this.overlayEl.classList.add("open");
      this.modalEl.classList.add("open");
    }

    close() {
      this.overlayEl.classList.remove("open");
      this.modalEl.classList.remove("open");
    }

    _setMsg(text, kind) {
      this.msgEl.textContent = text || "";
      this.msgEl.className = `em-msg ${kind || ""}`;
    }

    /* ── 预览：parse + fetch + normalize + diff（后端，不入库） ── */
    async _doPreview() {
      const text = this.pasteEl.value.trim();
      if (!text) { this._setMsg("请先粘贴报文", "error"); return; }
      this.previewBtn.disabled = true;
      this.importBtn.disabled = true;
      this._setMsg("解析中…");
      try {
        const result = await window.StockRadar.api.eastmoneyPreview(text);
        if (result.code !== 0) {
          this._setMsg(result.msg || "解析失败", "error");
          this.previewAreaEl.innerHTML = "";
          this._preview = null;
          return;
        }
        this._preview = result.data;
        this._renderPreview(result.data);
        this.importBtn.disabled = false;
        this._setMsg("预览成功，请核对后确认导入", "ok");
      } catch (e) {
        this._setMsg(`预览失败：${e.message || e}`, "error");
      } finally {
        this.previewBtn.disabled = false;
      }
    }

    _renderPreview(data) {
      const snap = data.snapshot || {};
      const diff = data.diff || {};
      const fmt = (v, digits = 2) => v == null ? "—" :
        Number(v).toLocaleString("zh-CN", { maximumFractionDigits: digits });
      const acct = snap.account || {};
      const accountHtml = acct.total_asset != null ? `
        <div class="em-account">
          <span>总资产 <b>${fmt(acct.total_asset)}</b></span>
          <span>可用 <b>${fmt(acct.avail_cash)}</b></span>
          <span>持仓市值 <b>${fmt(acct.positions_market_value)}</b></span>
        </div>` : "";
      const rows = (snap.positions || []).map((p, i) => {
        const pct = p.float_pnl_pct != null ? p.float_pnl_pct * 100 : null;
        const pctCell = pct != null ?
          `<span class="${pct >= 0 ? "em-up" : "em-down"}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span>` : "—";
        return `<tr>
          <td>${i + 1}</td>
          <td>${this._esc(p.stock_name || p.stock_code)}</td>
          <td>${this._esc(p.stock_code)}${p.is_etf ? ' <span class="em-badge">ETF</span>' : ""}</td>
          <td>${p.qty}</td>
          <td>${p.avg_cost != null ? p.avg_cost.toFixed(3) : "—"}</td>
          <td>${p.last_price != null ? p.last_price.toFixed(3) : "—"}</td>
          <td>${pctCell}</td>
        </tr>`;
      }).join("");
      const diffText = `新增 ${diff.added?.length || 0} / 更新 ${diff.updated?.length || 0} / 清仓 ${diff.removed?.length || 0} / 不变 ${diff.unchanged || 0}`;
      this.previewAreaEl.innerHTML = `
        <div class="em-section">
          <div class="em-section-title">预览（${(snap.positions || []).length} 只持仓）</div>
          ${accountHtml}
          <div class="em-diff">${diffText}（与现有账本对比）</div>
          <table class="em-table">
            <thead><tr><th>#</th><th>名称</th><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>盈亏</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${snap.skipped_rows ? `<div class="em-skipped">已忽略 ${snap.skipped_rows} 行（数量为 0 或缺代码）</div>` : ""}
        </div>`;
    }

    /* ── 确认导入：快照原样回传（无凭据），后端重算 diff + 覆盖式落库 ── */
    async _doImport() {
      if (!this._preview) return;
      this.importBtn.disabled = true;
      this._setMsg("导入中…");
      try {
        const result = await window.StockRadar.api.eastmoneyImport(
          this._preview.snapshot);
        if (result.code !== 0) {
          this._setMsg(result.msg || "导入失败", "error");
          this.importBtn.disabled = false;
          return;
        }
        const d = result.data || {};
        this._setMsg(
          `导入完成：新增 ${d.added ?? 0} / 更新 ${d.updated ?? 0} / 清仓 ${d.removed ?? 0} / 不变 ${d.unchanged ?? 0}`, "ok");
        this._preview = null;
        this.importBtn.disabled = true;
        this.previewAreaEl.innerHTML = "";
        this.pasteEl.value = "";
        setTimeout(() => this.close(), 1200);
        if (this._onImported) this._onImported();
      } catch (e) {
        this._setMsg(`导入失败：${e.message || e}`, "error");
        this.importBtn.disabled = false;
      }
    }

    _esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    }
  };

  window.StockRadar.EastmoneySyncDialog = EastmoneySyncDialog;
})();
