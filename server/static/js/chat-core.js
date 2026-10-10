/* ─────────────────────────────────────────────
   选股雷达 Web — 聊天区：消息列表、气泡、状态气泡
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.chat = (() => {
  const { esc, TYPE_LABELS } = window.StockRadar.utils;
  const chatMarkdown = window.StockRadar.chatMarkdown;
  const chatDashboard = window.StockRadar.chatDashboard;
  const chatExport = window.StockRadar.chatExport;

  function setBubbleContent(bubble, text, isMarkdown = true, skipMermaid = false) {
    const mdDiv = bubble.querySelector(".md-content") || (() => {
      const d = document.createElement("div");
      d.className = "md-content";
      bubble.innerHTML = "";
      bubble.appendChild(d);
      return d;
    })();
    if (isMarkdown) {
      mdDiv.innerHTML = chatMarkdown.renderMd(text);
      // 清理 marked 未转义的 tool_call/function_call 标签（LLM 原始输出泄漏到 HTML）
      mdDiv.querySelectorAll("tool_call, function, parameter, invoke").forEach(el => {
        el.replaceWith(document.createTextNode(el.outerHTML));
      });
      chatMarkdown._highlightCodeBlocks(mdDiv);
      if (!skipMermaid) {
        chatMarkdown._renderMermaidBlocks(mdDiv);
      }
      renderChartFragments(mdDiv);
    } else {
      mdDiv.textContent = text;
    }
  }

  /* ── Chart Fragments：报告中的 @@CHART:uuid@@ 标记 → ECharts 图表卡片 ──
     异步加载 + 失败降级为占位，绝不影响 markdown 正文展示 */
  const _ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js";
  let _echartsPromise = null;

  function _ensureEcharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    if (!_echartsPromise) {
      _echartsPromise = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = _ECHARTS_CDN;
        s.onload = () => resolve(window.echarts);
        s.onerror = () => { _echartsPromise = null; reject(new Error("ECharts 加载失败")); };
        document.head.appendChild(s);
      });
    }
    return _echartsPromise;
  }

  // 图表数值展示：基准单位（元/点）按量级压缩为 万/亿，避免轴标签与 tooltip 出现长数字
  function _fmtChartNum(v) {
    const n = Number(v);
    if (!isFinite(n)) return String(v == null ? "" : v);
    const abs = Math.abs(n);
    if (abs >= 1e8) return (n / 1e8).toFixed(2).replace(/\.?0+$/, "") + "亿";
    if (abs >= 1e6) return (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + "万";
    return String(+n.toFixed(2));
  }

  // 柱状图数值标签：量级差异悬殊时小柱也能直接读到数值（不依赖柱高）；
  // 正数标柱顶、负数标柱底，格式化复用 _fmtChartNum（万/亿）。
  function _withBarLabels(series) {
    return (series || []).map(s => {
      if (!s || s.type !== "bar") return s;
      const data = (s.data || []).map(v => {
        if (v == null) return v;
        const item = (typeof v === "object") ? Object.assign({}, v) : { value: v };
        const num = Number(item.value);
        if (!isFinite(num)) return item;
        item.label = Object.assign({}, item.label, {
          position: num < 0 ? "bottom" : "top",
          distance: 4,
        });
        return item;
      });
      return Object.assign({}, s, {
        data,
        label: Object.assign({
          show: true,
          fontSize: 10,
          formatter: (p) => _fmtChartNum(p.value),
        }, s.label || {}),
      });
    });
  }

  function _buildFragmentOption(dataJson) {
    let data;
    try { data = JSON.parse(dataJson); } catch (e) { return null; }
    const option = {
      animation: false,
      grid: { left: 60, right: 30, top: 30, bottom: 40 },
      xAxis: Object.assign({ type: "category" }, (data.xAxis && data.xAxis[0]) || { data: [] }),
      yAxis: { type: "value", scale: true, axisLabel: { formatter: _fmtChartNum } },
      tooltip: { trigger: "axis", valueFormatter: _fmtChartNum },
      series: _withBarLabels(data.series),
    };
    if (data.markLine) {
      option.series = option.series.map(s => Object.assign({}, s, { markLine: { data: data.markLine } }));
    }
    return option;
  }

  function _renderOneFragment(el, frag) {
    const render = (f) => {
      const option = _buildFragmentOption(f.data_json);
      if (!option) throw new Error("图表数据解析失败");
      return _ensureEcharts().then(echarts => {
        el.dataset.state = "ok";
        el.innerHTML = "";
        if (f.title) {
          const t = document.createElement("div");
          t.className = "ccf-title";
          t.textContent = "📊 " + f.title;
          el.appendChild(t);
        }
        const body = document.createElement("div");
        body.className = "ccf-body";
        el.appendChild(body);
        const chart = echarts.init(body);
        chart.setOption(option);
        window.addEventListener("resize", () => chart.resize());
      });
    };
    const fail = () => {
      el.dataset.state = "error";
      el.textContent = "📊 图表加载失败";
    };
    if (frag) {
      render(frag).catch(fail);
      return;
    }
    fetch(`/api/chart/${encodeURIComponent(el.dataset.chartId)}`)
      .then(r => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
      .then(f => render(f))
      .catch(fail);
  }

  // 图表类型分组键：按标题关键词（与 chart_extractor 的 title 对齐）
  function _groupKey(frag) {
    const t = frag.title || "";
    for (const kw of ["均线", "RSI", "MACD", "财务", "资金流向"]) {
      if (t.includes(kw)) return kw;
    }
    return frag.chart_type || "图表";
  }

  /* ── tech_panel：技术面图板（本地 K 线 + 全指标 + 信号标注）──
     复用 chart-kline.js 的 KlineChart（K线+成交量+副图三联布局），
     Tab 切换副图指标（MACD/RSI/KDJ/BOLL/DPO），自动叠加金叉死叉/
     布林带突破信号点。数据来自 /api/chart/kline 与 /api/chart/signals。 */
  const _TECH_TABS = [
    { type: "macd", label: "MACD" },
    { type: "rsi",  label: "RSI" },
    { type: "kdj",  label: "KDJ" },
    { type: "boll", label: "BOLL" },
    { type: "dpo",  label: "DPO" },
  ];

  function _renderTechPanel(el, frag) {
    let meta;
    try { meta = JSON.parse(frag.data_json); } catch (e) { meta = null; }
    const code = meta && meta.code;
    const days = (meta && meta.days) || 250;
    // 标的市场类型由后端显式携带（INDEX/STOCK）。必须透传给数据接口：
    // 裸 6 位码在不同表里是不同标的（000001 = 上证指数 / 平安银行），
    // 不传 market 时后端默认按个股取数。
    const market = (meta && meta.market) || "";
    const isIndex = market === "INDEX";
    const marketQ = market ? "&market=" + encodeURIComponent(market) : "";
    if (!code || !window.StockRadar || !window.StockRadar.KlineChart) {
      el.textContent = "📊 技术面图板暂不可用";
      el.dataset.state = "error";
      return;
    }

    el.dataset.state = "ok";
    el.classList.add("tech-panel");
    el.innerHTML = "";

    const title = document.createElement("div");
    title.className = "ccf-title";
    title.textContent = "📈 " + (frag.title || "技术面图板");
    el.appendChild(title);

    // 副图指标 Tab 行
    const tabs = document.createElement("div");
    tabs.className = "ccf-tabs tech-panel-tabs";
    _TECH_TABS.forEach((t, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ccf-tab" + (i === 0 ? " active" : "");
      btn.textContent = t.label;
      btn.dataset.sub = t.type;
      tabs.appendChild(btn);
    });
    el.appendChild(tabs);

    const body = document.createElement("div");
    body.className = "tech-panel-body";
    el.appendChild(body);

    // 策略扫描行：选择策略 → 扫描买卖点 → 叠加到 K 线（复用 backtest 策略引擎）
    const scanRow = document.createElement("div");
    scanRow.className = "tech-panel-scan";
    scanRow.innerHTML = `
      <select class="tech-scan-select" title="选择策略">
        <option value="">选择策略…</option>
      </select>
      <button type="button" class="tech-scan-btn" disabled>扫描买卖点</button>
      <button type="button" class="tech-scan-clear" hidden>清除标记</button>
      <span class="tech-scan-status"></span>`;
    const scanSelect = scanRow.querySelector(".tech-scan-select");
    const scanBtn = scanRow.querySelector(".tech-scan-btn");
    const clearBtn = scanRow.querySelector(".tech-scan-clear");
    const scanStatus = scanRow.querySelector(".tech-scan-status");
    if (isIndex) {
      // 指数（上证指数等）没有「策略买卖点」语义：策略扫描走个股通道
      // （backtest/data_adapter._load_stock_daily），对指数会取到同码个股数据
      scanRow.hidden = true;
    }
    el.appendChild(scanRow);

    // 懒加载策略列表（backtest/strategies 的 8 个内置策略）
    if (!isIndex && window.StockRadar.api && window.StockRadar.api.getBacktestStrategies) {
      window.StockRadar.api.getBacktestStrategies().then(data => {
        const strategies = (data && (data.strategies || data.items)) || [];
        strategies.forEach(s => {
          const id = s.id || s.strategy_id;
          if (!id) return;
          const opt = document.createElement("option");
          opt.value = id;
          opt.textContent = s.name || id;
          scanSelect.appendChild(opt);
        });
      }).catch(() => { scanSelect.disabled = true; });
    } else {
      scanSelect.disabled = true;
    }

    const foot = document.createElement("div");
    foot.className = "tech-panel-foot";
    foot.textContent = "信号标注（近60日）：MACD/KDJ/MA 金叉死叉、布林带上下轨突破；点击 K 线可查看任意日期至今的收益";
    el.appendChild(foot);

    // KlineChart 构造即 echarts.init（需要容器已有尺寸），而本卡片此刻
    // 可能尚未插入 DOM（_buildFragEl 先返回、调用方后 append），因此延迟
    // 一帧到 DOM 渲染后再初始化。
    requestAnimationFrame(() => {
      const chart = new window.StockRadar.KlineChart(body);

      tabs.addEventListener("click", (e) => {
        const btn = e.target.closest(".tech-panel-tabs .ccf-tab");
        if (!btn) return;
        tabs.querySelectorAll(".ccf-tab").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        chart.setSubIndicator(btn.dataset.sub);
      });

      // 策略扫描交互
      scanSelect.addEventListener("change", () => {
        scanBtn.disabled = !scanSelect.value || scanBtn.classList.contains("running");
      });
      scanBtn.addEventListener("click", async () => {
        const strategyId = scanSelect.value;
        if (!strategyId || scanBtn.classList.contains("running")) return;
        if (!window.StockRadar.api || !window.StockRadar.api.scanStrategySignals) {
          scanStatus.textContent = "扫描接口不可用";
          return;
        }
        scanBtn.classList.add("running");
        scanBtn.disabled = true;
        scanBtn.textContent = "扫描中…";
        scanStatus.textContent = "";
        try {
          const result = await window.StockRadar.api.scanStrategySignals({
            code: code,
            strategy_id: strategyId,
            adjust_type: "qfq",
            days: days,
          });
          const signals = result.signals || [];
          chart.setOverlay({ type: "scan", signals: signals });
          const buys = signals.filter(s => s.buy).length;
          const sells = signals.filter(s => s.sell).length;
          let tip = `${buys} 个买点 / ${sells} 个卖点`;
          if (result.latest_is_buy) tip += " · 今日出现买点";
          else if (result.latest_is_sell) tip += " · 今日出现卖点";
          scanStatus.textContent = tip;
          scanStatus.classList.remove("err");
          clearBtn.hidden = false;
        } catch (e) {
          scanStatus.textContent = "扫描失败：" + (e.message || e);
          scanStatus.classList.add("err");
        } finally {
          scanBtn.classList.remove("running");
          scanBtn.textContent = "扫描买卖点";
          scanBtn.disabled = !scanSelect.value;
        }
      });
      clearBtn.addEventListener("click", () => {
        chart.clearOverlay();
        clearBtn.hidden = true;
        scanStatus.textContent = "";
      });

      chart.ready.then(async () => {
        chart.showLoading("K线数据加载中...");
        const [klineRes, signalsRes] = await Promise.all([
          fetch(`/api/chart/kline/${encodeURIComponent(code)}?days=${days}${marketQ}`)
            .then(r => (r.ok ? r.json() : Promise.reject(new Error("kline HTTP " + r.status)))),
          fetch(`/api/chart/signals/${encodeURIComponent(code)}?days=${days}${marketQ}`)
            .then(r => (r.ok ? r.json() : null))
            .catch(() => null),
        ]);
        if (!klineRes || !klineRes.kline || !klineRes.kline.length) {
          throw new Error("empty kline");
        }
        klineRes.signals = (signalsRes && signalsRes.signals) || [];
        await chart.setData(klineRes);
      }).catch(() => {
        try { chart.showError("K线数据暂不可用（本地未同步该标的日线）"); }
        catch (e) { el.textContent = "📊 技术面图板数据加载失败"; }
      });
    });
  }

  // 渲染单张图表卡片（frag 已 fetch）
  function _buildFragEl(frag) {
    const el = document.createElement("div");
    el.className = "chat-chart-frag";
    el.dataset.chartId = frag.id;
    el.dataset.state = "loading";
    el.textContent = "📊 图表加载中...";
    if (frag.chart_type === "tech_panel") {
      _renderTechPanel(el, frag);
      return el;
    }
    _renderOneFragment(el, frag);
    return el;
  }

  // 报告图表区：按类型分组 Tab 展示（多组时 Tab 切换 + 懒加载；单组直接渲染）
  function renderChartFragments(mdDiv) {
    if (!mdDiv) return;

    // 兜底：历史报告可能存有「丢壳」的图表标记（裸 UUID，旧版归集 bug 所致）。
    // 摘完整标记后，若正文残留独立 UUID，逐个向 /api/chart/{id} 验证，
    // 是图表则补壳后重走本流程（bareChecked 防递归死循环）。
    if (!mdDiv.dataset.bareChecked) {
      const probe = mdDiv.innerHTML.replace(/@@CHART:[0-9a-f-]+@@/g, "");
      const bareIds = [...new Set(probe.match(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g) || [])];
      if (bareIds.length > 0) {
        mdDiv.dataset.bareChecked = "1";
        Promise.all(bareIds.map(id =>
          fetch(`/api/chart/${encodeURIComponent(id)}`)
            .then(r => (r.ok ? id : null))
            .catch(() => null)
        )).then(validIds => {
          const ok = validIds.filter(Boolean);
          if (ok.length === 0) return;
          // 先把完整标记替换为占位符，避免补壳时误改其内部的 id
          const marks = [];
          let html = mdDiv.innerHTML;
          html = html.replace(/@@CHART:[0-9a-f-]+@@/g, m => { marks.push(m); return `\u0000${marks.length - 1}\u0000`; });
          ok.forEach(id => { html = html.split(id).join(`@@CHART:${id}@@`); });
          html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => marks[+i]);
          mdDiv.innerHTML = html;
          renderChartFragments(mdDiv);
        });
        return; // 验证完成后重走本函数，此处不继续
      }
    }

    if (!mdDiv.innerHTML.includes("@@CHART:")) return;
    const ids = [];
    mdDiv.innerHTML = mdDiv.innerHTML.replace(/@@CHART:([0-9a-f-]+)@@/g, (m, id) => { ids.push(id); return ""; });
    if (ids.length === 0) return;

    const wrap = document.createElement("div");
    wrap.className = "ccf-wrap";
    mdDiv.appendChild(wrap);

    Promise.all(ids.map(id =>
      fetch(`/api/chart/${encodeURIComponent(id)}`)
        .then(r => (r.ok ? r.json() : null))
        .catch(() => null)
    )).then(frags => {
      const ok = frags.filter(Boolean);
      if (ok.length === 0) {
        wrap.textContent = "📊 图表加载失败";
        return;
      }
      // 按类型分组（保序）
      const groups = new Map();
      ok.forEach(f => {
        const key = _groupKey(f);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(f);
      });
      const keys = [...groups.keys()];

      // 单组：不显示 Tab 栏，直接渲染（仍保留大屏按钮）
      if (keys.length === 1) {
        const bar = _buildFragBar(ok);
        wrap.appendChild(bar);
        groups.get(keys[0]).forEach(f => wrap.appendChild(_buildFragEl(f)));
        return;
      }

      // 多组：Tab 栏 + 面板（懒加载，切到才渲染）
      const tabs = document.createElement("div");
      tabs.className = "ccf-tabs";
      const panels = document.createElement("div");
      panels.className = "ccf-panels";
      keys.forEach((key, i) => {
        const btn = document.createElement("button");
        btn.className = "ccf-tab" + (i === 0 ? " active" : "");
        btn.textContent = `${key}(${groups.get(key).length})`;
        btn.onclick = () => {
          tabs.querySelectorAll(".ccf-tab").forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          panels.querySelectorAll(".ccf-panel").forEach(p => (p.style.display = "none"));
          const panel = panels.querySelector(`.ccf-panel[data-group="${key.replace(/"/g, "")}"]`);
          if (panel) {
            panel.style.display = "block";
            if (!panel.dataset.rendered) {
              panel.dataset.rendered = "1";
              groups.get(key).forEach(f => panel.appendChild(_buildFragEl(f)));
            }
          }
        };
        tabs.appendChild(btn);
        const panel = document.createElement("div");
        panel.className = "ccf-panel";
        panel.dataset.group = key;
        panel.style.display = i === 0 ? "block" : "none";
        panels.appendChild(panel);
      });
      // 默认渲染第一组
      groups.get(keys[0]).forEach(f => panels.querySelector(".ccf-panel").appendChild(_buildFragEl(f)));
      wrap.appendChild(tabs);
      wrap.appendChild(_buildFragBar(ok));
      wrap.appendChild(panels);
    });
  }

  // 图表操作栏：平铺大屏按钮
  function _buildFragBar(frags) {
    const bar = document.createElement("div");
    bar.className = "ccf-bar";
    const btn = document.createElement("button");
    btn.className = "ccf-fullscreen-btn";
    btn.textContent = "🖥 平铺大屏";
    btn.title = "所有图表平铺展示，一眼看全部走势";
    btn.onclick = () => _openTiledView(frags);
    bar.appendChild(btn);
    return bar;
  }

  // 平铺大屏：全屏 overlay，所有图表网格展示（2 列）
  function _openTiledView(frags) {
    const overlay = document.createElement("div");
    overlay.className = "ccf-overlay";
    const header = document.createElement("div");
    header.className = "ccf-overlay-header";
    const title = document.createElement("div");
    title.className = "ccf-overlay-title";
    title.textContent = `📊 图表总览（${frags.length} 张）`;
    const close = document.createElement("button");
    close.className = "ccf-overlay-close";
    close.textContent = "✕ 关闭";
    close.onclick = () => overlay.remove();
    header.appendChild(title);
    header.appendChild(close);
    const grid = document.createElement("div");
    grid.className = "ccf-grid";
    frags.forEach(f => {
      const card = _buildFragEl(f);
      card.classList.add("ccf-grid-item");
      grid.appendChild(card);
    });
    overlay.appendChild(header);
    overlay.appendChild(grid);
    document.body.appendChild(overlay);
    const onKey = (e) => { if (e.key === "Escape") overlay.remove(); };
    document.addEventListener("keydown", onKey);
    // overlay 移除时清理 ESC 监听
    const origRemove = overlay.remove.bind(overlay);
    overlay.remove = () => {
      document.removeEventListener("keydown", onKey);
      origRemove();
    };
  }

  function setBubbleContentWithDashboard(bubble, text, skipMermaid = false) {
    let dashboardData = null;
    let cleanText = text;
    const match = text.match(/@@DASHBOARD_START@@([\s\S]*?)@@DASHBOARD_END@@/);
    if (match) {
      try {
        dashboardData = JSON.parse(match[1]);
        cleanText = text.replace(/@@DASHBOARD_START@@[\s\S]*?@@DASHBOARD_END@@/, "");
      } catch (e) {
        console.warn("[Dashboard] JSON parse failed:", e.message, match[1].substring(0, 200));
      }
    }
    setBubbleContent(bubble, cleanText, true, skipMermaid);
    if (dashboardData) {
      try {
        const card = chatDashboard.renderDashboardCard(dashboardData);
        const mdContent = bubble.querySelector(".md-content");
        if (mdContent) {
          mdContent.parentNode.insertBefore(card, mdContent.nextSibling);
        } else {
          bubble.appendChild(card);
        }
      } catch (e) {
        console.warn("[Dashboard] renderDashboardCard failed:", e.message);
      }
    }
  }

  function setButtonDone(button, className = "copied", delay = 1500) {
    button.classList.add(className);
    setTimeout(() => button.classList.remove(className), delay);
  }

  let notifyMenuCloserBound = false;

  function cleanNotificationContent(content) {
    return String(content || "")
      .replace(/@@DASHBOARD_START@@[\s\S]*?@@DASHBOARD_END@@/g, "")
      .trim();
  }

  function buildNotificationTitle(content) {
    const cleanText = cleanNotificationContent(content);
    const firstLine = cleanText
      .split(/\r?\n/)
      .map(line => line.trim())
      .find(line => line && !line.startsWith("|") && !/^[-*_]{3,}$/.test(line));
    const rawTitle = (firstLine || "选股雷达报告")
      .replace(/^#{1,6}\s*/, "")
      .replace(/^\*\*(.*?)\*\*$/, "$1")
      .replace(/[`*_>#]/g, "")
      .trim() || "选股雷达报告";
    return rawTitle.length > 48 ? `${rawTitle.slice(0, 48)}...` : rawTitle;
  }

  function formatNotificationResults(results) {
    const items = results || [];
    if (!items.length) return "后端未返回发送结果";
    const ok = items.filter(item => item.success).length;
    const failed = items.length - ok;
    return `成功 ${ok} / ${items.length}` + (failed ? `，失败 ${failed}` : "");
  }

  function closeNotifyMenus(exceptMenu = null) {
    document.querySelectorAll(".msg-notify-menu.open, .msg-export-menu.open").forEach(menu => {
      if (menu !== exceptMenu) menu.classList.remove("open");
    });
  }

  function ensureNotifyMenuCloser() {
    if (notifyMenuCloserBound) return;
    notifyMenuCloserBound = true;
    document.addEventListener("click", () => closeNotifyMenus());
  }

  async function sendMarkdownNotification(content, button) {
    const cleanText = cleanNotificationContent(content);
    if (!cleanText) throw new Error("报告内容为空");
    if (!window.StockRadar.api || !window.StockRadar.api.sendNotification) {
      throw new Error("通知接口未加载");
    }

    const data = await window.StockRadar.api.sendNotification({
      title: buildNotificationTitle(content),
      content: cleanText,
      type: "report",
      channels: null,
      force: true,
    });
    const message = formatNotificationResults(data.results);
    button.title = `通知已发送：${message}`;
    setButtonDone(button, "copied", 1500);
    setTimeout(() => { button.title = "发送报告通知"; }, 3000);
  }

  async function sendImageNotification(bubble, content, button) {
    if (!window.StockRadar.api || !window.StockRadar.api.sendNotification) {
      throw new Error("通知接口未加载");
    }

    // 生成图片
    button.title = "正在生成报告图片...";
    const blob = await chatExport.bubbleToPngBlob(bubble);
    if (!blob) throw new Error("图片生成失败");

    // 转为 base64
    const reader = new FileReader();
    const imageData = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });

    // 发送通知
    button.title = "正在发送图片通知...";
    const cleanText = cleanNotificationContent(content);
    const data = await window.StockRadar.api.sendNotification({
      title: buildNotificationTitle(content),
      content: cleanText,
      type: "report",
      channels: null,
      force: true,
      image_data: imageData,
    });

    // 处理结果
    const results = data.results || [];
    const supported = results.filter(r => r.success);
    const unsupported = results.filter(r => !r.success && r.error && r.error.includes("不支持图片"));

    let message = "";
    if (supported.length > 0) {
      message += `已发送到 ${supported.map(r => r.channel).join(", ")}`;
    }
    if (unsupported.length > 0) {
      message += `${supported.length > 0 ? "；" : ""}${unsupported.map(r => r.channel).join(", ")} 不支持图片`;
    }
    if (!message) {
      message = formatNotificationResults(results);
    }

    button.title = `通知已发送：${message}`;
    setButtonDone(button, "copied", 1500);
    setTimeout(() => { button.title = "发送报告通知"; }, 3000);
  }

  /* ── 报告内联「数据来源」条（回答溯源轻量版）──
     复用 /attribution 聚合的 sources，展示本回答真实用到的数据域/提供商。
     点击来源标签展开该来源的支撑查询证据（query + 结果摘要），
     并在报告正文中高亮其涉及的股票代码。
     无 trace_run_id / 无来源 / 接口失败时静默降级，绝不影响正文展示。 */
  async function _loadMsgSources(contentCol, meta) {
    if (contentCol.querySelector(".msg-sources")) return;
    const api = window.StockRadar && window.StockRadar.api;
    if (!api || !api.getAttribution) return;
    const dialogUuid = meta.dialog_uuid ||
      (window.StockRadar.app && window.StockRadar.app.state && window.StockRadar.app.state.currentId);
    if (!dialogUuid || !meta.trace_run_id) return;

    const el = document.createElement("div");
    el.className = "msg-sources";
    el.hidden = true;
    contentCol.appendChild(el);
    try {
      const data = await api.getAttribution(dialogUuid, meta.trace_run_id);
      // 过滤「推理」域：LLM 是分析引擎不是数据源，展示给终端用户会误导
      const sources = ((data && data.sources) || []).filter((s) => s.domain !== "推理");
      if (!sources.length) { el.remove(); return; }
      const label = document.createElement("span");
      label.className = "msg-sources-label";
      label.textContent = "数据来源";
      el.appendChild(label);
      sources.forEach((s) => {
        const tag = document.createElement("button");
        tag.type = "button";
        tag.className = "msg-sources-tag";
        tag.textContent = s.label || s.domain || "未知";
        const n = (s.step_ids && s.step_ids.length) || 0;
        tag.title = (s.provider && s.provider !== "—" ? s.provider + " · " : "") + n + " 个数据步骤，点击查看支撑证据";
        tag.addEventListener("click", () => _toggleMsgSourceDetail(tag, s, contentCol));
        el.appendChild(tag);
      });
      el.hidden = false;
    } catch (e) {
      el.remove();
    }
  }

  // 从支撑查询里提取股票代码（6 位数字，且像股票代码：600/601/603/000/001/002/300 开头）
  function _stockCodesFromQueries(steps) {
    const codes = [];
    (steps || []).forEach((st) => {
      const m = String(st.query || "").match(/\b((?:600|601|603|605|000|001|002|003|300|301|688|689)\d{3})\b/g);
      if (m) codes.push(...m);
    });
    return [...new Set(codes)];
  }

  function escapeRegExp(str) {
    return String(str).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // 正文中高亮指定股票代码（用 <mark> 包裹文本节点，重复调用先清理）
  function _highlightMdCodes(mdContent, codes) {
    _clearMdHighlights(mdContent);
    if (!mdContent || !codes.length) return;
    const walker = document.createTreeWalker(mdContent, NodeFilter.SHOW_TEXT, null);
    const targets = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const found = codes.filter((c) => node.nodeValue.includes(c));
      if (found.length) targets.push([node, found]);
    }
    targets.forEach(([node, found]) => {
      const frag = document.createDocumentFragment();
      let rest = node.nodeValue;
      // 按出现位置切分（简单起见：对每个 code 用占位拆分，一次性处理）
      const parts = rest.split(new RegExp("(" + found.map(escapeRegExp).join("|") + ")", "g"));
      parts.forEach((p) => {
        if (found.includes(p)) {
          const m = document.createElement("mark");
          m.className = "msg-src-mark";
          m.textContent = p;
          frag.appendChild(m);
        } else if (p) {
          frag.appendChild(document.createTextNode(p));
        }
      });
      node.parentNode.replaceChild(frag, node);
    });
  }

  function _clearMdHighlights(mdContent) {
    if (!mdContent) return;
    mdContent.querySelectorAll("mark.msg-src-mark").forEach((m) => {
      const t = document.createTextNode(m.textContent);
      m.parentNode.replaceChild(t, m);
    });
  }

  // 切换某来源标签的证据面板（展开/收起），并同步正文高亮
  function _toggleMsgSourceDetail(tag, source, contentCol) {
    const el = contentCol.querySelector(".msg-sources");
    if (!el) return;
    const mdContent = contentCol.querySelector(".md-content");
    const active = tag.classList.toggle("active");
    // 收起其他来源的展开面板
    el.querySelectorAll(".msg-sources-tag.active").forEach((t) => {
      if (t !== tag) { t.classList.remove("active"); t.nextElementSibling && t.nextElementSibling.remove(); }
    });
    el.querySelectorAll(".msg-sources-detail").forEach((d) => d.remove());

    if (!active) { _clearMdHighlights(mdContent); return; }

    // 展开证据面板
    const steps = (source && source.steps) || [];
    const total = ((source && source.step_ids) || []).length;
    const panel = document.createElement("div");
    panel.className = "msg-sources-detail";
    if (!steps.length) {
      panel.innerHTML = '<div class="msg-sources-empty">该来源无支撑查询记录</div>';
    } else {
      const rows = steps.map((st) => `
        <div class="msg-sources-step ${st.status === "error" || (st.summary && st.summary.indexOf("失败") >= 0) ? "err" : ""}">
          <div class="msg-sources-step-q">${esc(st.query || "(无查询语句)")}</div>
          <div class="msg-sources-step-s">${esc(st.summary || "")}</div>
        </div>`).join("");
      panel.innerHTML = rows +
        (total > steps.length
          ? `<div class="msg-sources-more">共 ${total} 个查询，展示前 ${steps.length} 条</div>` : "");
    }
    tag.insertAdjacentElement("afterend", panel);

    // 正文高亮：该来源查询涉及的股票代码
    const codes = _stockCodesFromQueries(steps);
    _highlightMdCodes(mdContent, codes);
  }

  function appendMessage(container, role, content, animate = true, meta = null) {
    const wrap = document.createElement("div");
    wrap.className = `message ${role} message-enter`;
    wrap.addEventListener("animationend", () => wrap.classList.remove("message-enter"), { once: true });
    if (meta && meta.message_uuid) wrap.id = "msg-" + meta.message_uuid;

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "U" : "SR";

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    wrap.appendChild(avatar);

    const contentCol = document.createElement("div");
    contentCol.className = "message-content-col";

    contentCol.appendChild(bubble);

    if (role === "assistant") {
      setBubbleContentWithDashboard(bubble, content);

      // 报告底部「数据来源」条：真实执行记录（attribution sources）内联展示
      if (meta && meta.trace_run_id) {
        _loadMsgSources(contentCol, meta);
      }

      const actionBar = document.createElement("div");
      actionBar.className = "msg-action-bar";

      const copyBtn = document.createElement("button");
      copyBtn.className = "msg-copy-btn";
      copyBtn.title = "复制";
      copyBtn.innerHTML = `
        <svg class="icon-copy" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        <svg class="icon-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      `;
      copyBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(content);
          setButtonDone(copyBtn);
        } catch {}
      });
      actionBar.appendChild(copyBtn);

      const copyImageBtn = document.createElement("button");
      copyImageBtn.className = "msg-copy-image-btn";
      copyImageBtn.title = "复制为图片";
      copyImageBtn.innerHTML = `
        <svg class="icon-image" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
        <svg class="icon-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      `;
      copyImageBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await chatExport.copyBubbleAsImage(bubble);
          setButtonDone(copyImageBtn);
        } catch (err) {
          console.error("图片导出失败:", err);
          copyImageBtn.title = "导出失败: " + (err.message || "未知错误");
          setButtonDone(copyImageBtn, "failed", 1500);
          setTimeout(() => { copyImageBtn.title = "复制为图片"; }, 3000);
        }
      });
      actionBar.appendChild(copyImageBtn);

      // 导出为图片按钮 + 菜单（单张高清 / 内容感知分割）
      const exportWrap = document.createElement("div");
      exportWrap.className = "msg-export-wrap";

      const exportBtn = document.createElement("button");
      exportBtn.className = "msg-export-btn";
      exportBtn.title = "导出为图片";
      exportBtn.innerHTML = `
        <svg class="icon-download" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        <svg class="icon-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      `;

      const exportMenu = document.createElement("div");
      exportMenu.className = "msg-export-menu";
      exportMenu.innerHTML = `
        <button class="msg-notify-menu-item" type="button" data-export-single>
          <span>导出单张图片</span>
          <small>整条消息一张高清 PNG（1080px）</small>
        </button>
        <button class="msg-notify-menu-item" type="button" data-export-sliced>
          <span>分割导出（9:16 竖屏）</span>
          <small>按段落/表格整块切分，多张存文件夹/zip</small>
        </button>
      `;

      exportBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const nextOpen = !exportMenu.classList.contains("open");
        closeNotifyMenus(exportMenu);
        exportMenu.classList.toggle("open", nextOpen);
      });

      const restoreExportTitle = () => {
        setTimeout(() => { exportBtn.title = "导出为图片"; }, 3000);
      };

      exportMenu.querySelector("[data-export-single]").addEventListener("click", async (e) => {
        e.stopPropagation();
        exportMenu.classList.remove("open");
        exportBtn.title = "正在导出高清图片...";
        try {
          const r = await chatExport.exportBubbleAsImage(bubble);
          exportBtn.title = `已导出 ${r.count} 张图片`;
          setButtonDone(exportBtn, "copied", 1800);
          restoreExportTitle();
        } catch (err) {
          console.error("图片导出失败:", err);
          exportBtn.title = "导出失败: " + (err.message || "未知错误");
          setButtonDone(exportBtn, "failed", 1500);
          restoreExportTitle();
        }
      });

      exportMenu.querySelector("[data-export-sliced]").addEventListener("click", async (e) => {
        e.stopPropagation();
        exportMenu.classList.remove("open");
        exportBtn.title = "正在分割导出...";
        try {
          const r = await chatExport.exportBubbleSliced(bubble, {
            onProgress: (i, n) => { exportBtn.title = `分割导出中 ${i}/${n}...`; },
          });
          const modeText = {
            single: "单张",
            dir: `已存文件夹（${r.count} 张）`,
            zip: `已存 zip（${r.count} 张）`,
            multi: `已下载 ${r.count} 张`,
          }[r.mode] || `已导出 ${r.count} 张`;
          exportBtn.title = modeText;
          setButtonDone(exportBtn, "copied", 2000);
          restoreExportTitle();
        } catch (err) {
          console.error("分割导出失败:", err);
          if (err && err.name === "AbortError") {
            exportBtn.title = "已取消导出";
          } else {
            exportBtn.title = "导出失败: " + (err.message || "未知错误");
            setButtonDone(exportBtn, "failed", 1500);
          }
          restoreExportTitle();
        }
      });

      exportWrap.appendChild(exportBtn);
      exportWrap.appendChild(exportMenu);
      actionBar.appendChild(exportWrap);

      ensureNotifyMenuCloser();
      const notifyWrap = document.createElement("div");
      notifyWrap.className = "msg-notify-wrap";

      const notifyBtn = document.createElement("button");
      notifyBtn.className = "msg-notify-btn";
      notifyBtn.title = "发送报告通知";
      notifyBtn.innerHTML = `
        <svg class="icon-notify" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9L22 2Z"/></svg>
        <svg class="icon-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      `;

      const notifyMenu = document.createElement("div");
      notifyMenu.className = "msg-notify-menu";
      notifyMenu.innerHTML = `
        <button class="msg-notify-menu-item" type="button" data-notify-markdown>
          <span>Markdown 文案</span>
          <small>发送当前报告正文</small>
        </button>
        <button class="msg-notify-menu-item" type="button" data-notify-image>
          <span>报告图片</span>
          <small>发送报告截图（含仪表盘）</small>
        </button>
      `;

      notifyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const nextOpen = !notifyMenu.classList.contains("open");
        closeNotifyMenus(notifyMenu);
        notifyMenu.classList.toggle("open", nextOpen);
      });

      const markdownNotifyBtn = notifyMenu.querySelector("[data-notify-markdown]");
      markdownNotifyBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        notifyMenu.classList.remove("open");
        notifyBtn.title = "正在发送报告通知...";
        try {
          await sendMarkdownNotification(content, notifyBtn);
        } catch (err) {
          console.error("通知发送失败:", err);
          notifyBtn.title = "发送失败: " + (err.message || "未知错误");
          setButtonDone(notifyBtn, "failed", 1500);
          setTimeout(() => { notifyBtn.title = "发送报告通知"; }, 3000);
        }
      });

      const imageNotifyBtn = notifyMenu.querySelector("[data-notify-image]");
      imageNotifyBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        notifyMenu.classList.remove("open");
        notifyBtn.title = "正在生成报告图片...";
        try {
          await sendImageNotification(bubble, content, notifyBtn);
        } catch (err) {
          console.error("图片通知发送失败:", err);
          notifyBtn.title = "发送失败: " + (err.message || "未知错误");
          setButtonDone(notifyBtn, "failed", 1500);
          setTimeout(() => { notifyBtn.title = "发送报告通知"; }, 3000);
        }
      });

      notifyWrap.appendChild(notifyBtn);
      notifyWrap.appendChild(notifyMenu);
      actionBar.appendChild(notifyWrap);

      if (meta && meta.log_file) {
        const logBtn = document.createElement("button");
        logBtn.className = "msg-log-btn";
        logBtn.title = "查看日志";
        logBtn.innerHTML = `
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        `;
        logBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const baseName = meta.log_file.replace(/^(logs[\/\\])+/, "");
          window.open(`/logs/${encodeURIComponent(baseName)}`, "_blank");
        });
        actionBar.appendChild(logBtn);
      }

      if (meta && (meta.trace_run_id || meta.task_id)) {
        const detailBtn = document.createElement("button");
        detailBtn.className = "msg-detail-btn";
        detailBtn.textContent = "查看详情";
        detailBtn.dataset.traceRunId = meta.trace_run_id || "";
        detailBtn.dataset.dialogUuid = meta.dialog_uuid || "";
        actionBar.appendChild(detailBtn);
      }

      contentCol.appendChild(actionBar);
    } else {
      bubble.textContent = content;
    }

    wrap.appendChild(contentCol);
    container.appendChild(wrap);

    if (animate) scrollBottom(container);
    return bubble;
  }

  function scrollBottom(container) {
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  }

  function createStatusBubble(container) {
    const wrap = document.createElement("div");
    wrap.className = "message assistant";

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = "SR";

    const bubble = document.createElement("div");
    bubble.className = "bubble status-bubble";

    const statusLine = document.createElement("div");
    statusLine.className = "status-line";
    statusLine.innerHTML = '<span class="status-spinner"></span><span class="status-text">准备中…</span>';

    const progressLog = document.createElement("div");
    progressLog.className = "status-log";

    bubble.appendChild(statusLine);
    bubble.appendChild(progressLog);
    wrap.appendChild(avatar);
    wrap.appendChild(bubble);
    container.appendChild(wrap);
    scrollBottom(container);

    return { wrap, bubble, statusLine, progressLog };
  }

  function updateStatusBubble(statusBubble, event) {
    if (!statusBubble) return;

    // 记忆上下文：渲染为三段卡片（用户原始输入 / 用户画像 / 用户相关记忆片段）
    if (event.type === "memory_context" && event.data) {
      const d = event.data;
      const block = (label, val, empty) => {
        const isEmpty = !val || !String(val).trim();
        return `<div class="mem-ctx-block"><div class="mem-ctx-label">${esc(label)}</div>` +
          `<div class="mem-ctx-body">${esc(isEmpty ? (empty || "（空）") : String(val))}</div></div>`;
      };
      const logEntry = document.createElement("div");
      logEntry.className = "status-entry status-memory_context";
      logEntry.innerHTML = `<div class="mem-ctx-inner">` +
        block("用户原始输入", d.raw_input, "（空）") +
        block("用户画像", d.profile, "（暂无用户画像）") +
        block("用户相关记忆片段", d.memory_fragments, "（无相关记忆）") +
        `</div>`;
      statusBubble.progressLog.appendChild(logEntry);
      scrollBottom(statusBubble.wrap.parentElement);
      return;
    }

    const icon = event.type === "step_complete" ? "✅"
      : event.type === "error" ? "❌"
      : event.type === "warning" ? "⚠️"
      : event.type === "final" ? "✅"
      : "";

    const logEntry = document.createElement("div");
    logEntry.className = `status-entry status-${event.type}`;
    logEntry.textContent = `${icon} ${event.message || TYPE_LABELS[event.type] || event.type}`;
    statusBubble.progressLog.appendChild(logEntry);

    const currentLabel = TYPE_LABELS[event.type] || event.type;
    statusBubble.statusLine.innerHTML =
      `<span class="status-spinner"></span><span class="status-text">${esc(event.message || currentLabel)}</span>`;

    scrollBottom(statusBubble.wrap.parentElement);
  }

  function finalizeStatusBubble(statusBubble, content) {
    if (!statusBubble) return;
    setBubbleContentWithDashboard(statusBubble.bubble, content);
    statusBubble.bubble.classList.remove("status-bubble");
  }

  /* ── 记忆上下文卡片（渲染进主对话 dialog） ──
     用户原始输入 / 用户画像 / 用户相关记忆片段 三段，作为对话内卡片展示。
     beforeNode 指定时插入其前面（用于 reload 时贴合对应轮次）；
     未指定时插入到最后一条用户消息之后（用于实时流式）。 */
  function appendMemoryContext(container, event, beforeNode) {
    if (!container || !event || !event.data) return;
    const key = String(event.task_id || event.dialog_uuid || "memory");
    const safeKey = (window.CSS && CSS.escape) ? CSS.escape(key) : key.replace(/["\\]/g, "\\$&");
    const existing = container.querySelector(`.memory-context-card[data-mc-key="${safeKey}"]`);
    if (existing) existing.remove();

    const d = event.data;
    const block = (label, val, empty) => {
      const isEmpty = !val || !String(val).trim();
      return `<div class="mem-ctx-block"><div class="mem-ctx-label">${esc(label)}</div>` +
        `<div class="mem-ctx-body">${esc(isEmpty ? (empty || "（空）") : String(val))}</div></div>`;
    };

    const card = document.createElement("div");
    card.className = "memory-context-card";
    card.dataset.mcKey = key;
    card.innerHTML = `
      <div class="mcc-header">
        <span class="mcc-icon">🧠</span>
        <span class="mcc-title">记忆上下文</span>
        <span class="mcc-sub">用户原始输入 / 用户画像 / 用户相关记忆片段</span>
      </div>
      <div class="mem-ctx-inner">
        ${block("用户原始输入", d.raw_input, "（空）")}
        ${block("用户画像", d.profile, "（暂无用户画像）")}
        ${block("用户相关记忆片段", d.memory_fragments, "（无相关记忆）")}
      </div>`;

    if (beforeNode && beforeNode.parentNode === container) {
      container.insertBefore(card, beforeNode);
    } else {
      const lastUser = container.querySelector(".message.user:last-of-type");
      if (lastUser && lastUser.nextSibling) {
        container.insertBefore(card, lastUser.nextSibling);
      } else if (lastUser) {
        container.appendChild(card);
      } else {
        container.insertBefore(card, container.firstChild);
      }
    }
    scrollBottom(container);
  }

  async function typewriterStatusBubble(statusBubble, content, options = {}) {
    if (!statusBubble) return;

    statusBubble.bubble.classList.remove("status-bubble");
    const text = content || "";
    const delay = options.delay || 24;
    const maxFrames = options.maxFrames || 120;
    const chunkSize = options.chunkSize || Math.max(24, Math.ceil(text.length / maxFrames));
    let index = 0;

    while (index < text.length) {
      index = Math.min(text.length, index + chunkSize);
      setBubbleContentWithDashboard(statusBubble.bubble, text.slice(0, index), true);
      scrollBottom(statusBubble.wrap.parentElement);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }

    // 最终渲染：完整文本 + mermaid
    setBubbleContentWithDashboard(statusBubble.bubble, text, false);
  }

  function showEmptyState(container) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📊</div>
        <div class="empty-title">选股雷达</div>
        <div class="empty-desc">输入股票名称或问题，获取 AI 分析</div>
        <div class="empty-suggestions">
          <button class="empty-suggestion-btn" data-q="分析贵州茅台">分析贵州茅台</button>
          <button class="empty-suggestion-btn" data-q="今日大盘走势">今日大盘走势</button>
          <button class="empty-suggestion-btn" data-q="推荐几只高股息股票">推荐几只高股息股票</button>
        </div>
      </div>`;
    container.querySelectorAll(".empty-suggestion-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const q = btn.getAttribute("data-q");
        if (q && typeof sendMessage === "function") sendMessage(q);
      });
    });
  }

  /* ── 溯源定位：双向高亮（被 attribution-panel 调用）── */
  function highlightMessage(uuid) {
    if (!uuid) return;
    clearLocate();
    const el = document.getElementById("msg-" + uuid);
    if (!el) return;
    const list = el.parentElement;
    if (list) list.classList.add("locating");
    el.classList.add("msg-locate");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => clearLocate(), 2400);
  }

  function clearLocate() {
    document.querySelectorAll(".message.msg-locate").forEach((e) => e.classList.remove("msg-locate"));
    document.querySelectorAll(".locating").forEach((e) => e.classList.remove("locating"));
  }

  return {
    esc, renderMd: chatMarkdown.renderMd, setBubbleContent, setBubbleContentWithDashboard,
    renderDashboardCard: chatDashboard.renderDashboardCard, appendMessage, scrollBottom,
    appendMemoryContext,
    createStatusBubble, updateStatusBubble, finalizeStatusBubble, typewriterStatusBubble,
    showEmptyState, copyBubbleAsImage: chatExport.copyBubbleAsImage,
    downloadBlob: chatExport.downloadBlob, TYPE_LABELS,
    highlightMessage, clearLocate,
  };
})();
