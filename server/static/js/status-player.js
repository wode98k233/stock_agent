/* ─────────────────────────────────────────────
   选股雷达 Web — Pending 状态播放器
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.StatusPlayer = (() => {
  const MIN_STEP_DELAY = 260;
  const MAX_STEP_DELAY = 860;
  const IDLE_DELAY = 1400;
  const IDLE_MESSAGES = ["思考中…", "等待模型返回…", "整理数据…", "分析执行结果…"];

  class StatusPlayer {
    constructor(statusBubble, chat) {
      this.statusBubble = statusBubble;
      this.chat = chat;
      this.queue = [];
      this.running = false;
      this.stopped = false;
      this.idleIndex = 0;
      this.lastKey = "";
      this.sleepTimer = null;
      this.sleepResolve = null;
    }

    enqueue(event) {
      if (!event || this.stopped) return;
      const normalized = this._normalize(event);
      if (!normalized) return;
      this.queue.push(normalized);
      this._compactQueue();
      this._ensureRunning();
      this._wake();
    }

    start() {
      if (this.stopped) return;
      this._ensureRunning();
    }

    async finish(content) {
      this.stopped = true;
      this._wake();
      while (this.running) {
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      // pending-only playback：当前播放只是 pending 期间的临时效果；完成后会刷新历史消息，
      // 因此这里不保留过程动画状态，后续如需保留再做消息持久化设计。
      await this.chat.typewriterStatusBubble(this.statusBubble, content || "");
    }

    stop() {
      this.stopped = true;
      this.queue = [];
      this._wake();
    }

    _normalize(event) {
      const message = (event.message || "").trim();
      if (!message) return null;
      const data = event.data || {};
      return {
        type: event.type || "step_start",
        message,
        data,
        key: `${data.step_id || ""}:${data.status || ""}:${message}`,
      };
    }

    _compactQueue() {
      if (this.queue.length <= 8) return;
      const keep = [];
      const seen = new Set();
      for (let i = this.queue.length - 1; i >= 0; i -= 1) {
        const item = this.queue[i];
        const compactKey = `${item.data.step_type || item.type}:${item.data.status || item.type}:${item.message}`;
        if (seen.has(compactKey)) continue;
        seen.add(compactKey);
        keep.push(item);
        if (keep.length >= 8) break;
      }
      this.queue = keep.reverse();
    }

    _ensureRunning() {
      if (this.running) return;
      this.running = true;
      this._run();
    }

    async _run() {
      while (!this.stopped) {
        const item = this.queue.shift();
        if (item) {
          await this._playItem(item);
          await this._sleep(this._delayForBacklog());
          continue;
        }
        await this._playIdle();
        if (this.queue.length === 0) {
          await this._sleep(IDLE_DELAY);
        }
      }
      this.running = false;
    }

    async _playItem(item) {
      if (item.key === this.lastKey) return;
      this.lastKey = item.key;
      this._appendLogEntry(item);
      await this.typewriterStatusLine(item.message);
    }

    async _playIdle() {
      const message = IDLE_MESSAGES[this.idleIndex % IDLE_MESSAGES.length];
      this.idleIndex += 1;
      await this.typewriterStatusLine(message);
    }

    async typewriterStatusLine(message) {
      const line = this.statusBubble.statusLine;
      const safe = this.chat.esc(message);
      const textEl = line.querySelector(".status-text");
      if (!textEl) {
        line.innerHTML = `<span class="status-spinner"></span><span class="status-text">${safe}</span>`;
        return;
      }
      textEl.textContent = "";
      const chars = Array.from(message);
      const chunkSize = Math.max(2, Math.ceil(chars.length / 18));
      for (let i = 0; i < chars.length && !this.stopped; i += chunkSize) {
        textEl.textContent = chars.slice(0, i + chunkSize).join("");
        this.chat.scrollBottom(this.statusBubble.wrap.parentElement);
        await this._sleep(32);
      }
    }

    _appendLogEntry(item) {
      const logEntry = document.createElement("div");
      logEntry.className = `status-entry status-${item.type}`;
      logEntry.textContent = item.message;
      this.statusBubble.progressLog.appendChild(logEntry);
      const maxEntries = 8;
      while (this.statusBubble.progressLog.children.length > maxEntries) {
        this.statusBubble.progressLog.removeChild(this.statusBubble.progressLog.firstElementChild);
      }
      this.chat.scrollBottom(this.statusBubble.wrap.parentElement);
    }

    _delayForBacklog() {
      const backlog = this.queue.length;
      if (backlog >= 10) return MIN_STEP_DELAY;
      if (backlog >= 5) return 380;
      if (backlog >= 2) return 560;
      return MAX_STEP_DELAY;
    }

    _sleep(ms) {
      if (this.stopped) return Promise.resolve();
      return new Promise((resolve) => {
        this.sleepResolve = () => {
          this.sleepResolve = null;
          this.sleepTimer = null;
          resolve();
        };
        this.sleepTimer = setTimeout(this.sleepResolve, ms);
      });
    }

    _wake() {
      if (!this.sleepResolve) return;
      const resolve = this.sleepResolve;
      if (this.sleepTimer) {
        clearTimeout(this.sleepTimer);
      }
      resolve();
    }
  }

  return { StatusPlayer, MIN_STEP_DELAY, MAX_STEP_DELAY, IDLE_MESSAGES };
})();
