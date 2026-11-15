/*
   选股雷达 Web — 策略意图词表配置面板（设计文档 §5.2.3 / 需求单 R3）
   左：9 组词表 + 2 个常量（词表文件 agents/shared/strategy_intent_rules.json）
   右：一句话检测 —— 与闸门同一个函数，禁止复刻判定逻辑
*/

(function () {
  "use strict";

  window.StockRadar = window.StockRadar || {};

  /* key / 标题 / 类型(list|dict|num) / 说明；数组顺序 = JSON 键序 = 判定优先级 */
  const GROUPS = [
    ["imperatives", "祈使词", "list", "明确要求「跑一次验证」的说法，命中即判策略"],
    ["intent_words", "意图词", "list", "指向策略验证结果的说法（回测 / 胜率 / 靠不靠谱）"],
    ["condition_words", "条件词", "list", "指标与形态名；单独出现只算「疑似策略」"],
    ["entry_words", "买入动作词", "list", "只参与「可疑带」判断（配合 LLM 兜底）"],
    ["exit_words", "卖出动作词", "list", "同上"],
    ["timing_words", "成交口径词", "dict", "一行一个 键=值；值只能是 same_close（尾盘）或 next_open（次日开盘）"],
    ["question_markers", "疑问词", "list", "命中且无意图词时判「疑似策略」，先问一句"],
    ["period_words", "区间词", "dict", "一行一个 键=值（天数），如 近三年=1095"],
    ["universe_defaults", "标的默认词", "dict", "一行一个 键=值（6 位代码），如 茅台=600519"],
  ];

  const CONSTS = [
    ["min_confidence", "判定阈值", "num", "低于它不进策略会话（默认 0.5）"],
    ["recommended_days", "推荐区间天数", "num", "澄清卡里推荐的回测区间（默认 1095 = 近三年）"],
  ];

  const HIT_LABELS = {
    imperatives: "祈使词",
    intent_words: "意图词",
    condition_words: "条件词",
    entry_words: "买入动作词",
    exit_words: "卖出动作词",
  };

  const SLOT_LABELS = {
    universe: "标的",
    universe_source: "标的来源",
    period_start: "开始日期",
    period_end: "结束日期",
    period_days: "区间天数",
    entry: "买入条件",
    exit: "卖出条件",
    entry_timing: "成交口径",
  };

  window.StockRadar.StrategyIntentConfigPanel = class StrategyIntentConfigPanel {
    constructor() {
      this.rules = null;
      this.path = "";
      this._("siCloseBtn").onclick = () => this.close();
      this._("siOverlay").onclick = () => this.close();
      this._("siSaveBtn").onclick = () => this.save();
      this._("siTestRunBtn").onclick = () => this.runTest();
      this._("siTestInput").onkeydown = (e) => { if (e.key === "Enter") this.runTest(); };
    }

    /* ── DOM 快捷方式 ── */
    _(id) { return document.getElementById(id); }

    /* ── HTML 转义 ── */
    _esc(s) {
      const d = document.createElement("div");
      d.textContent = s == null ? "" : String(s);
      return d.innerHTML;
    }

    /* ═══ 面板开关（与模板管理 / 路由配置互斥） ═══ */
    open() {
      ["templateManagerPanel", "templateManagerOverlay", "rcPanel", "rcOverlay"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.classList.remove("open");
      });
      this._("siPanel").classList.add("open");
      this._("siOverlay").classList.add("open");
      this.loadRules();
    }

    close() {
      this._("siPanel").classList.remove("open");
      this._("siOverlay").classList.remove("open");
    }

    /* ═══ 读词表 ═══ */
    async loadRules() {
      this._("siGroups").innerHTML = "<div class='rc-empty-hint'>正在加载词表...</div>";
      try {
        const res = await fetch("/api/strategy-intent/rules");
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
        this.rules = data.rules || {};
        this.path = data.path || "";
        this.render();
      } catch (err) {
        this._("siGroups").innerHTML =
          "<div class='rc-empty-hint'>词表加载失败：" + this._esc(err.message) + "</div>";
      }
    }

    /* ═══ 渲染编辑器 ═══ */
    render() {
      const rules = this.rules || {};
      let html = "<div class='si-hint'>词表文件：" + this._esc(this.path) +
        "<br>保存后免重启即时生效（发送前预览 / 对话闸门 / 本页检测三处同源）</div>";
      GROUPS.forEach((g) => { html += this._groupHtml(g, rules[g[0]]); });
      html += "<div class='si-group'><div class='si-group-head'>" +
        "<span class='si-group-title'>常量</span></div>" +
        "<div class='si-group-desc'>判定阈值与推荐区间（数字）</div>";
      CONSTS.forEach((c) => {
        const step = c[0] === "min_confidence" ? "0.05" : "1";
        html += "<div class='si-row'><label class='si-row-label' for='si-" + c[0] + "'>" + c[1] +
          "<em>" + c[0] + "</em></label>" +
          "<input id='si-" + c[0] + "' class='si-num' type='number' step='" + step +
          "' data-si-key='" + c[0] + "' data-si-kind='num' value='" +
          this._esc(rules[c[0]] == null ? "" : rules[c[0]]) + "'>" +
          "<span class='si-row-desc'>" + c[3] + "</span></div>";
      });
      html += "</div>";
      this._("siGroups").innerHTML = html;
      this._setStatus("", false);
    }

    _groupHtml(g, value) {
      const key = g[0], title = g[1], kind = g[2], desc = g[3];
      let text = "";
      if (kind === "list") {
        text = (Array.isArray(value) ? value : []).join("\n");
      } else {
        const obj = value && typeof value === "object" ? value : {};
        text = Object.keys(obj).map((k) => k + "=" + obj[k]).join("\n");
      }
      const hint = kind === "list" ? "（一行一个词，顺序即优先级）" : "（一行一个 键=值）";
      return "<div class='si-group'><div class='si-group-head'>" +
        "<span class='si-group-title'>" + title + "</span>" +
        "<span class='si-group-key'>" + key + "</span></div>" +
        "<div class='si-group-desc'>" + desc + "　" + hint + "</div>" +
        "<textarea class='si-ta' data-si-key='" + key + "' data-si-kind='" + kind +
        "' spellcheck='false'>" + this._esc(text) + "</textarea></div>";
    }

    /* ═══ 收集编辑器内容（保留未知键与原键序） ═══ */
    _collect() {
      const out = Object.assign({}, this.rules || {});
      const prev = this.rules || {};
      GROUPS.concat(CONSTS).forEach((g) => {
        const key = g[0], kind = g[2];
        const el = document.querySelector("[data-si-key='" + key + "']");
        if (!el) return;
        if (kind === "num") {
          const n = Number(el.value);
          out[key] = isFinite(n) ? n : prev[key];
          return;
        }
        const lines = String(el.value || "").split("\n").map((s) => s.trim()).filter((s) => s.length > 0);
        if (kind === "list") { out[key] = lines; return; }
        const obj = {};
        const before = prev[key] && typeof prev[key] === "object" ? prev[key] : {};
        lines.forEach((line) => {
          const i = line.indexOf("=");
          if (i <= 0) return;
          const k = line.slice(0, i).trim(), v = line.slice(i + 1).trim();
          obj[k] = (typeof before[k] === "number" && isFinite(Number(v))) ? Number(v) : v;
        });
        out[key] = obj;
      });
      return out;
    }

    /* ═══ 保存（校验在服务端；非法结构原样回显中文错误清单） ═══ */
    async save() {
      const btn = this._("siSaveBtn");
      btn.disabled = true;
      this._setStatus("保存中...", false);
      try {
        const res = await fetch("/api/strategy-intent/rules", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rules: this._collect() }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          this._setStatus("保存失败：" + (data.detail || ("HTTP " + res.status)), true);
          return;
        }
        this.rules = this._collect();
        this._setStatus("已保存并即时生效" + (data.backup ? "（已备份：" + data.backup + "）" : ""), false);
      } catch (err) {
        this._setStatus("保存失败：" + err.message, true);
      } finally {
        btn.disabled = false;
      }
    }

    _setStatus(msg, isErr) {
      const el = this._("siStatus");
      if (!el) return;
      el.textContent = msg;
      el.className = isErr ? "si-status err" : "si-status";
    }

    /* ═══ 检测一句话（与闸门同源） ═══ */
    async runTest() {
      const text = (this._("siTestInput").value || "").trim();
      const box = this._("siTestResults");
      if (!text) {
        box.innerHTML = "<div class='rc-empty-hint'>先输入一句话，如：尾盘买入第二天卖出这个策略如何？</div>";
        return;
      }
      box.innerHTML = "<div class='rc-empty-hint'>检测中...</div>";
      try {
        const res = await fetch("/api/strategy-intent/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
        this._renderTest(data);
      } catch (err) {
        box.innerHTML = "<div class='rc-empty-hint'>检测失败：" + this._esc(err.message) + "</div>";
      }
    }

    _renderTest(d) {
      const isStrategy = !!d.is_strategy, dis = !!d.disambiguate;
      const badge = isStrategy ? "<span class='si-badge yes'>策略会话</span>"
        : (dis ? "<span class='si-badge hint'>疑似策略</span>"
               : "<span class='si-badge'>普通提问</span>");
      const flow = isStrategy
        ? "发送后会先进策略会话：先确认标的 / 区间 / 买入 / 卖出四个槽位，确认完才跑回测。"
        : (dis ? "发送后由 LLM 兜底判别归属（开关 STRATEGY_INTENT_LLM_FALLBACK 关闭时改问一句分流题）。"
               : "不进策略会话，走原有报告流程。");
      let html = "<div class='si-verdict'>" + badge +
        "<span class='si-meta'>reason=" + this._esc(d.reason || "-") +
        "　confidence=" + this._esc(d.confidence) +
        (d.question_marker ? "　命中疑问词" : "") + "</span></div>";
      html += "<div class='si-note'>" + flow + "</div>";
      const hits = d.hits || {};
      const hitRows = Object.keys(HIT_LABELS).map((k) => {
        const arr = hits[k] || [];
        return "<div class='si-row'><span class='si-row-label'>" + HIT_LABELS[k] + "</span>" +
          (arr.length ? arr.map((w) => "<span class='si-hit'>" + this._esc(w) + "</span>").join("")
                      : "<span class='si-none'>—</span>") + "</div>";
      }).join("");
      html += "<div class='si-block'><div class='si-block-title'>命中词</div>" + hitRows + "</div>";
      const slots = d.slots || {};
      const keys = Object.keys(slots);
      html += "<div class='si-block'><div class='si-block-title'>解析到的槽位</div>" +
        (keys.length ? keys.map((k) => {
          const v = slots[k];
          const val = v && typeof v === "object" ? (v.value == null ? "" : v.value) : v;
          const src = v && typeof v === "object" && v.source ? v.source : "";
          return "<div class='si-row'><span class='si-row-label'>" + this._esc(SLOT_LABELS[k] || k) +
            "</span><span class='si-slot'>" + this._esc(val) +
            (src ? " <em>" + this._esc(src) + "</em>" : "") + "</span></div>";
        }).join("") : "<div class='si-none'>未解析到槽位</div>") + "</div>";
      this._("siTestResults").innerHTML = html;
    }
  };
})();
