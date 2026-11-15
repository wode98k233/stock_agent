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
  const _ECHARTS_CDN = "/static/vendor/echarts.min.js";  // 本地 vendor 优先：jsdelivr 在部分网络（含本机）不可达，会让全站图表静默不渲染
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
  // ⚠️ 2026-11-03 修：万量级原来除以 1e6 却标「万」（1e4），整轴读数放大 100 倍
  //    （某用户报告里 1267 万被画成「12.67万」、-9755 万画成「-97.55万」）。
  function _fmtChartNum(v) {
    const n = Number(v);
    if (!isFinite(n)) return String(v == null ? "" : v);
    const abs = Math.abs(n);
    if (abs >= 1e12) return (n / 1e12).toFixed(2).replace(/\.?0+$/, "") + "万亿";
    if (abs >= 1e8) return (n / 1e8).toFixed(2).replace(/\.?0+$/, "") + "亿";
    if (abs >= 1e4) return (n / 1e4).toFixed(2).replace(/\.?0+$/, "") + "万";
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

  // 主题色：统一取 chart-theme.js 的 token，取不到时回退旧色
  function _tc(key, fallback) {
    const c = window.SRChartTheme && window.SRChartTheme.colors;
    return (c && c[key]) || fallback;
  }

  // 取数据点的数值（兼容裸数字与 {value} 对象）
  function _num(v) {
    const n = (v && typeof v === "object") ? Number(v.value) : Number(v);
    return Number.isFinite(n) ? n : null;
  }

  // 图表副标题：静态图没人会悬停，把"时间范围 + 区间涨跌"直接写在标题下面
  function _fragmentSubtitle(raw) {
    const dates = (raw && raw.xAxis && raw.xAxis[0] && raw.xAxis[0].data) || [];
    if (dates.length < 10) return "";
    const s0 = ((raw.series || []).find(s => s && s.data && s.data.length)) || null;
    if (!s0) return `${dates[0]} → ${dates[dates.length - 1]}`;
    const vals = (s0.data || []).map(_num).filter(v => v !== null);
    if (vals.length < 2) return `${dates[0]} → ${dates[dates.length - 1]}`;
    const first = vals[0], last = vals[vals.length - 1];
    let tail = "";
    if (first !== 0) {
      const pct = (last / first - 1) * 100;
      tail = ` · 区间 ${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
    }
    return `${dates[0]} → ${dates[dates.length - 1]}${tail}`;
  }

  // 哪些指标是"涨跌量"，颜色必须红涨绿跌；成交额 / PE / 市值这类是中性的，单色即可。
  // 判据放在指标名与标题上，因为同一个图表类型（comparison）两种语义都会出现。
  const _SIGNED_METRIC_RE = /涨跌|涨幅|跌幅|净流入|净流出|净额|超额收益|收益率/;
  function _isSignedMetric(series, title) {
    const name = (series && series.name) || "";
    return _SIGNED_METRIC_RE.test(name) || _SIGNED_METRIC_RE.test(title || "");
  }

  // 涨跌类柱状图改横条（借 lieflat G10 的语法）：
  //   横向条 + 零轴实线贯穿 + 数值标在条外侧 + 朝外一端胶囊圆角
  // 竖向柱在类目 >10 时要把类目名转 38°，而横条天生就能横排；零轴也更显眼。
  // 只对「涨跌幅 / 净流入」这类有正负语义的指标启用——成交额、PE 是中性的，竖柱更合适。
  function _prepareSignedBars(raw, title) {
    const s0 = (raw && raw.series && raw.series[0]) || null;
    if (!s0 || s0.type !== "bar" || !_isSignedMetric(s0, title)) return null;
    const labels = ((raw.xAxis && raw.xAxis[0] && raw.xAxis[0].data) || []);
    const vals = s0.data || [];
    let rows = labels.map((lab, i) => ({ lab, v: _num(vals[i]) }))
      .filter(r => r.v !== null && Math.abs(r.v) > 0.0001);   // 0.00% 的条长为零，白占一行
    if (rows.length < 2) return null;
    rows.sort((a, b) => b.v - a.v);                            // 谁强谁弱，不是代码顺序
    return { rows, name: s0.name || "涨跌幅" };
  }

  function _buildSignedBarOption(prep) {
    const up = _tc("up", "#e74c3c");
    const down = _tc("down", "#176b5b");
    const rows = prep.rows;
    return {
      animation: false,
      grid: { left: 8, right: 66, top: 10, bottom: 10, containLabel: true },
      xAxis: {
        type: "value", axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { show: false }, splitLine: { show: false },
      },
      yAxis: {
        type: "category", data: rows.map(r => r.lab), inverse: true,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { fontSize: 10.5, color: _tc("axis", "#6b7280"), width: 96, overflow: "truncate" },
      },
      tooltip: {
        trigger: "axis", axisPointer: { type: "shadow" },
        backgroundColor: _tc("tooltipBg", "rgba(255,255,255,.96)"),
        borderColor: _tc("axisLine", "#e5e7eb"),
        textStyle: { color: _tc("text", "#1c2430"), fontSize: 12 },
        valueFormatter: (v) => ((v > 0 ? "+" : "") + Number(v).toFixed(2) + "%"),
      },
      series: [{
        name: prep.name,
        type: "bar",
        barWidth: 13,
        data: rows.map(r => ({
          value: r.v,
          itemStyle: {
            color: r.v >= 0 ? up : down,
            borderRadius: r.v >= 0 ? [0, 7, 7, 0] : [7, 0, 0, 7],
          },
        })),
        label: {
          // 正值标在条右端外，负值标在零轴右侧——都用 'right'，避免负值标签
          // 压到左边的类目名（2026-11-05 实测：短负条 + 长类目名会粘成一行）
          show: true, position: "right", distance: 6, fontSize: 10.5, fontWeight: 600,
          color: _tc("text", "#1c1c2e"),
          formatter: (p) => (p.value > 0 ? "+" : "") + Number(p.value).toFixed(2),
        },
        // 零轴：整条贯穿，读者不用在图里找 0 在哪
        markLine: {
          symbol: "none", silent: true, label: { show: false },
          lineStyle: { color: "#8f8e88", width: 1.5 },
          data: [{ xAxis: 0 }],
        },
      }],
    };
  }

  function _buildFragmentOption(dataJson, title) {
    let data;
    try { data = JSON.parse(dataJson); } catch (e) { return null; }
    // 涨跌语义的柱状图走横条（G10 语法），其余继续走通用竖柱
    const signedBars = _prepareSignedBars(data, title);
    if (signedBars) return _buildSignedBarOption(signedBars);
    const xData = ((data.xAxis && data.xAxis[0] && data.xAxis[0].data) || []);
    const nCat = xData.length;
    const series = _withBarLabels(data.series) || [];
    const hasBar = series.some(s => s && s.type === "bar");
    const nSeries = series.length;
    const option = {
      animation: false,
      grid: {
        left: 12, right: 46, bottom: nCat > 14 ? 20 : 8,
        top: nSeries > 1 ? 46 : 24, containLabel: true,
      },
      xAxis: Object.assign({ type: "category" }, (data.xAxis && data.xAxis[0]) || {},
        {
          axisLabel: {
            fontSize: 10, color: _tc("axis", "#64748b"), hideOverlap: true,
            // 自动 interval 在 50+ 类目时仍会挤；密度超过 16 就按比例抽稀，
            // 再加旋转，保证交易日轴看得清（2026-11-03 实测 120 个点糊成一片）
            interval: nCat > 16 ? Math.max(0, Math.ceil(nCat / 10) - 1) : "auto",
            rotate: nCat > 14 ? 38 : 0,
          },
        }),
      yAxis: {
        type: "value", scale: true,
        name: data.yUnit || "",
        nameTextStyle: { fontSize: 10, color: _tc("axis", "#64748b"), padding: [0, 0, 4, 0] },
        axisLabel: { formatter: _fmtChartNum, fontSize: 10, color: _tc("axis", "#64748b") },
        splitLine: { lineStyle: { color: _tc("grid", "#eef2f7") } },
      },
      tooltip: { trigger: "axis", valueFormatter: _fmtChartNum },
      series: series.map((s, si) => {
        if (!s) return s;
        if (s.type === "bar") {
          // 类目很少（2~3 个）时不限宽会撑成两块巨砖，限宽后居中更接近常规柱状图
          let bar = Object.assign({}, s, { barMaxWidth: 48, barGap: "20%" });
          // 涨跌语义的指标（涨跌幅 / 净流入…）按符号上色：红涨绿跌。
          // 判据是「指标本身是不是涨跌量」，不是「这组数据里有没有负数」——
          // 2026-11-05 实测：一批「涨跌幅对比」全是正数，按"有负数才上色"的旧规则
          // 全部走了默认品牌绿，A 股用户看到的是一排绿柱=全在跌，语义完全反了。
          if (_isSignedMetric(s, title)) {
            bar.itemStyle = Object.assign({}, s.itemStyle, {
              color: (p) => ((_num(p.value) || 0) >= 0
                ? _tc("up", "#e74c3c") : _tc("down", "#176b5b")),
            });
          }
          return bar;
        }
        // 主角（第一条线）加粗一档：报告图是静态的，靠粗细而不是色相区分主次
        const color = si === 0 ? _tc("brand", "#176b5b") : _tc("axis", "#6b7280");
        return Object.assign({}, s, {
          // 点数过多时不画圆点，否则折线变成一串珠子
          showSymbol: nCat <= 40, symbolSize: 4,
          lineStyle: Object.assign({ width: si === 0 ? 1.7 : 1.1 }, s.lineStyle || {}),
          // 端点直接标最后一个值：静态图没有悬停，右端读数是刚需
          endLabel: {
            show: true,
            distance: 3,
            formatter: (p) => _fmtChartNum(p.value),
            color,
            fontSize: 10,
            fontWeight: si === 0 ? 600 : 400,
          },
          labelLayout: { moveOverlap: "shiftY" },
        });
      }),
    };
    if (nSeries > 1) {
      option.legend = {
        top: 4, itemWidth: 12, itemHeight: 8, itemGap: 12,
        textStyle: { fontSize: 11, color: _tc("legend", "#475569") },
      };
    }
    if (data.markLine) {
      option.series = option.series.map(s => Object.assign({}, s, { markLine: { data: data.markLine } }));
    }
    return option;
  }

  // 图表容器高度：默认 300px 是给「满屏时序」的，类目很少的柱状图撑满 300px 会显得空。
  function _chartBodyHeight(raw, title) {
    // 横条按类目数长：15 行挤在 300px 里，标签会和类目名粘成一行
    const prep = _prepareSignedBars(raw, title);
    if (prep) return Math.min(560, Math.max(220, prep.rows.length * 22 + 44));
    const nCat = ((raw && raw.xAxis && raw.xAxis[0] && raw.xAxis[0].data) || []).length;
    const isBar = ((raw && raw.series) || []).some(s => s && s.type === "bar");
    if (isBar && nCat > 0 && nCat <= 4) return 190;
    if (isBar && nCat <= 10) return 240;
    if (nCat > 80) return 320;
    return 300;
  }

  // 单张图表导出为 PNG：报告里的图经常要贴到微信/文档里，截图会带上周围空白和页面背景。
  // ECharts 的 getDataURL 直接从 canvas 取图，白底、2 倍清晰度，标题文件名也用图表的标题。
  function _exportChartImage(chart, title) {
    try {
      const url = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
      const a = document.createElement("a");
      a.href = url;
      a.download = ((title || "图表").replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 60)) + ".png";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) {
      console.error("图表导出失败:", e);
    }
  }

  // 服务重启/预热期间接口会短暂 503（detail: "服务正在启动预热，请稍后重试"）。
  // 图表卡片原来只 fetch 一次，这一下失败就永久停在「图表加载失败」，用户只能刷新整页；
  // 所以对**瞬时错误**（网络失败 / 5xx / 503）退避重试，约 90 秒覆盖一次 restart 的
  // 预热窗口；4xx（片段真不存在 / 标的无本地日线）不重试，别把真错误藏掉。
  const _chartSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function _fetchJsonRetry(url, { attempts = 8, base = 2000, cap = 30000 } = {}) {
    let lastErr = null;
    for (let i = 0; i < attempts; i += 1) {
      let resp = null;
      try {
        resp = await fetch(url);
      } catch (err) {
        lastErr = err;                 // 连接被拒/中断：重启瞬间就是这种
      }
      if (resp) {
        if (resp.ok) return resp.json();
        if (resp.status !== 503 && resp.status < 500) {
          throw new Error("HTTP " + resp.status);
        }
        lastErr = new Error("HTTP " + resp.status);
      }
      // 最后一次不再等，直接抛
      if (i < attempts - 1) await _chartSleep(Math.min(cap, base * Math.pow(1.7, i)));
    }
    throw lastErr || new Error("fetch failed");
  }

  // 图表区的失败占位：可点击重试（重试成功就地把占位换回图）
  function _showChartError(container, label, onRetry) {
    container.innerHTML = "";
    const box = document.createElement("div");
    box.className = "ccf-error";
    box.textContent = label || "图表加载失败";
    box.title = "服务重启/预热或网络抖动时会出现，点击重新加载";
    box.style.cssText = [
      "cursor:pointer", "display:flex", "align-items:center", "justify-content:center",
      "min-height:60px", "font-size:12px", "color:#8a8b86",
      "border:1px dashed #dfe4ea", "border-radius:8px", "background:#fbfbfa",
    ].join(";");
    box.onclick = () => {
      box.textContent = "图表重新加载中…";
      box.style.cursor = "wait";
      Promise.resolve(onRetry()).catch(() => {
        box.textContent = (label || "图表加载失败") + "（仍失败）";
        box.style.cursor = "pointer";
      });
    };
    container.appendChild(box);
  }

  function _renderOneFragment(el, frag) {
    const render = (f) => {
      const option = _buildFragmentOption(f.data_json, f.title);
      if (!option) throw new Error("图表数据解析失败");
      let raw = null;
      try { raw = JSON.parse(f.data_json); } catch (e) { raw = null; }
      return _ensureEcharts().then(echarts => {
        if (window.SRChartTheme) window.SRChartTheme.register(echarts);
        el.dataset.state = "ok";
        el.innerHTML = "";
        let exportBtn = null;
        if (f.title) {
          const head = document.createElement("div");
          head.style.cssText = "display:flex;align-items:center;gap:8px";
          const t = document.createElement("div");
          t.className = "ccf-title";
          t.style.flex = "1";
          t.style.minWidth = "0";
          t.textContent = f.title;
          head.appendChild(t);
          exportBtn = document.createElement("button");
          exportBtn.type = "button";
          exportBtn.textContent = "导出图片";
          exportBtn.title = "把这张图存成 PNG（白底 2 倍图）";
          exportBtn.style.cssText =
            "flex:none;font-size:11px;line-height:18px;padding:0 8px;border:1px solid #dfe4ea;"
            + "border-radius:6px;background:#fff;color:#6b7280;cursor:pointer";
          head.appendChild(exportBtn);
          el.appendChild(head);
        }
        const subText = _fragmentSubtitle(raw);
        if (subText) {
          const sd = document.createElement("div");
          sd.className = "ccf-sub";
          sd.style.cssText = "font-size:11px;color:#8a8b86;margin:-2px 0 6px;letter-spacing:.01em";
          sd.textContent = subText;
          el.appendChild(sd);
        }
        const body = document.createElement("div");
        body.className = "ccf-body";
        body.style.height = _chartBodyHeight(raw, f.title) + "px";
        el.appendChild(body);
        const chart = echarts.init(body, (window.SRChartTheme && window.SRChartTheme.NAME) || undefined);
        chart.setOption(option);
        if (exportBtn) {
          exportBtn.onclick = () => _exportChartImage(chart, f.title);
        }
        // 懒加载 Tab 里的卡片初始化时容器可能还没有宽度，首帧会按 0 宽排版
        // （表现为图表挤在左边、标题被截断）。下一帧补一次 resize，并持续跟随容器尺寸。
        requestAnimationFrame(() => chart.resize());
        if (window.ResizeObserver) {
          const ro = new ResizeObserver(() => chart.resize());
          ro.observe(body);
          el.addEventListener("ccf-dispose", () => ro.disconnect());
        }
        window.addEventListener("resize", () => chart.resize());
      });
    };
    const fail = (err) => {
      el.dataset.state = "error";
      console.warn("[chart] 片段渲染失败", el.dataset.chartId, err);
      _showChartError(el, "图表加载失败", () => {
        el.dataset.state = "loading";
        return _renderOneFragment(el, null);   // frag=null → 重新走 fetch
      });
    };
    if (frag) {
      render(frag).catch(fail);
      return;
    }
    _fetchJsonRetry(`/api/chart/${encodeURIComponent(el.dataset.chartId)}`)
      .then(f => render(f))
      .catch(fail);
  }

  // 图表类型分组键：按标题关键词（与 chart_extractor 的 title 对齐），
  // 命中不了就用**中文类型名**——直接在 Tab 上显示 time_series / tech_panel 太难看了。
  const _GROUP_LABELS = {
    time_series: "走势", comparison: "对比", indicator: "指标", tech_panel: "K线图板",
  };

  function _groupKey(frag) {
    const t = frag.title || "";
    for (const kw of ["均线", "RSI", "MACD", "财务", "资金流向"]) {
      if (t.includes(kw)) return kw;
    }
    return _GROUP_LABELS[frag.chart_type] || frag.chart_type || "图表";
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

  /* ── 技术指标读数：图板此前只有图、没有任何数值/结论 ──
     2026-11-09 用户反馈「K 线图怎么没技术指标分析」。副图（MACD/RSI/KDJ/BOLL/DPO）
     和信号字母一直都在，但**没有一处告诉你现在是什么状态**——尤其指数没有策略扫描行时
     更像「什么分析都没有」。这里把已加载的指标压成一行结论，个股/指数通用。 */
  function _lastVal(arr) {
    return (Array.isArray(arr) && arr.length) ? arr[arr.length - 1] : null;
  }

  function _n2(v) {
    return (typeof v === "number" && isFinite(v)) ? v.toFixed(2) : "—";
  }

  function _indicatorReadout(res) {
    const ind = (res && res.indicators) || {};
    const kline = (res && res.kline) || [];
    const close = kline.length ? Number(kline[kline.length - 1].close) : null;
    const ma5 = _lastVal(ind.ma5), ma10 = _lastVal(ind.ma10);
    const ma20 = _lastVal(ind.ma20), ma60 = _lastVal(ind.ma60);
    const rsi6 = _lastVal(ind.rsi6);
    const macd = ind.macd || {};
    const dif = _lastVal(macd.dif), dea = _lastVal(macd.dea), hist = _lastVal(macd.hist);
    const histPrev = (Array.isArray(macd.hist) && macd.hist.length > 1)
      ? macd.hist[macd.hist.length - 2] : null;
    const kdj = ind.kdj || {};
    const K = _lastVal(kdj.k), D = _lastVal(kdj.d), J = _lastVal(kdj.j);
    const boll = ind.boll || {};
    const bu = _lastVal(boll.upper), bm = _lastVal(boll.mid), bl = _lastVal(boll.lower);

    const items = [];
    if (ma5 != null && ma10 != null && ma20 != null && ma60 != null) {
      const up = ma5 > ma10 && ma10 > ma20 && ma20 > ma60;
      const down = ma5 < ma10 && ma10 < ma20 && ma20 < ma60;
      items.push({
        label: "均线",
        text: up ? "多头排列" : down ? "空头排列" : "纠缠",
        tone: up ? "up" : down ? "down" : "",
      });
    }
    if (rsi6 != null) {
      // A 股口径：超买提示风险、超卖提示机会，所以超买用 down 色
      const tone = rsi6 >= 70 ? "down" : rsi6 <= 30 ? "up" : "";
      const word = rsi6 >= 70 ? "超买" : rsi6 <= 30 ? "超卖" : "中性";
      items.push({ label: "RSI", text: `RSI6 ${_n2(rsi6)}（${word}）`, tone });
    }
    if (dif != null && dea != null) {
      const golden = dif > dea;
      const growing = (hist != null && histPrev != null && Math.abs(hist) > Math.abs(histPrev));
      items.push({
        label: "MACD",
        text: `${golden ? "DIF 在 DEA 上方" : "DIF 在 DEA 下方"} · 柱 ${_n2(hist)}${growing ? " 放大" : " 缩小"}`,
        tone: golden ? "up" : "down",
      });
    }
    if (K != null && D != null) {
      items.push({
        label: "KDJ",
        text: `${K > D ? "金叉" : "死叉"} · K ${_n2(K)} / D ${_n2(D)} / J ${_n2(J)}`,
        tone: K > D ? "up" : "down",
      });
    }
    if (close != null && bm != null) {
      let word = "中轨附近", tone = "";
      if (bu != null && close > bu) { word = "上轨之上（超买区）"; tone = "down"; }
      else if (bl != null && close < bl) { word = "下轨之下（超卖区）"; tone = "up"; }
      else if (close > bm) { word = "中轨上方"; tone = "up"; }
      else { word = "中轨下方"; tone = "down"; }
      items.push({ label: "BOLL", text: word, tone });
    }

    const parts = [];
    if (ma5 != null && ma20 != null) parts.push(ma5 > ma20 ? "短期均线在中期之上" : "短期均线在中期之下");
    if (dif != null && dea != null) parts.push(dif > dea ? "MACD 多头" : "MACD 空头");
    if (rsi6 != null) parts.push(rsi6 >= 70 ? "RSI 超买需防回落" : rsi6 <= 30 ? "RSI 超卖有反抽" : "RSI 中性");
    return { items, summary: parts.join("；") };
  }

  function _renderReadout(container, res) {
    if (!container) return;
    const { items, summary } = _indicatorReadout(res);
    if (!items.length) { container.hidden = true; return; }
    /* 内容全部由本地数字与固定词拼成，不含后端自由文本，可安全用 innerHTML */
    const chips = items.map((it) => {
      const cls = it.tone === "up" ? "tpr-up" : it.tone === "down" ? "tpr-down" : "tpr-muted";
      return `<span class="tpr-item"><b>${it.label}</b><span class="${cls}">${it.text}</span></span>`;
    }).join("");
    container.innerHTML = chips
      + (summary ? `<div class="tpr-sum">📐 ${summary}</div>` : "");
    container.hidden = false;
  }

  function _renderTechPanel(el, frag) {
    let meta;
    try { meta = JSON.parse(frag.data_json); } catch (e) { meta = null; }
    const code = meta && meta.code;
    // 可变的：工具栏的「1月/2月/半年/1年/2年」会改它并重新取数
    let days = (meta && meta.days) || 250;
    // 标的市场类型由后端显式携带（INDEX/STOCK）。必须透传给数据接口：
    // 裸 6 位码在不同表里是不同标的（000001 = 上证指数 / 平安银行），
    // 不传 market 时后端默认按个股取数。
    const market = (meta && meta.market) || "";
    const isIndex = market === "INDEX";
    const marketQ = market ? "&market=" + encodeURIComponent(market) : "";
    if (!code || !window.StockRadar || !window.StockRadar.KlineChart) {
      el.textContent = "技术面图板暂不可用";
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

    /* 工具栏：周期 / 技术面图层 / 信号布局——对齐独立 K 线弹窗的能力（2026-11-10）。
       此前图板只能看固定 250 天、固定 MACD、固定贴图，用户反馈「不能选时间、不能选
       斐波那契、不能分道」。三者都只是 KlineChart 已有的 setter，这里补 UI 与联动。 */
    const toolbar = document.createElement("div");
    toolbar.className = "tech-panel-toolbar";
    toolbar.innerHTML = `
      <span class="tpt-group" title="时间范围">
        <button type="button" class="tpt-btn" data-days="30">1月</button>
        <button type="button" class="tpt-btn" data-days="60">2月</button>
        <button type="button" class="tpt-btn" data-days="120">半年</button>
        <button type="button" class="tpt-btn" data-days="250">1年</button>
        <button type="button" class="tpt-btn" data-days="500">2年</button>
      </span>
      <select class="tpt-select tpt-ta" title="技术面图层（算法生成的测量线，非交易信号）">
        <option value="off">技术面：关</option>
        <option value="retracement">斐波那契回撤</option>
        <option value="channel">斐波那契通道</option>
        <option value="fan">斐波那契扇形</option>
        <option value="trend">自动趋势线</option>
        <option value="bands">支撑阻力带</option>
        <option value="structure">波段结构 HH/HL</option>
        <option value="regression">回归通道 ±2σ</option>
        <option value="gaps">跳空缺口</option>
        <option value="all">全显示</option>
      </select>
      <span class="tpt-group" title="信号展示方式">
        <button type="button" class="tpt-btn" data-layout="pinned">贴图</button>
        <button type="button" class="tpt-btn" data-layout="lanes">分道</button>
      </span>`;
    el.appendChild(toolbar);

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

    // 技术指标读数（加载完成后填充；个股/指数通用）
    const readout = document.createElement("div");
    readout.className = "tech-panel-readout";
    readout.hidden = true;
    el.appendChild(readout);

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
    // 指数也支持策略扫描了（2026-11-09）：后端 /api/chart/strategy-signals 增加 market，
    // 'INDEX' 时从 index_daily 取数。此前这里 `scanRow.hidden = true`，因为扫描走个股
    // 通道，000001 会被当成平安银行，算出来的买卖点毫无意义。
    el.appendChild(scanRow);

    // 懒加载策略列表（backtest/strategies 的 8 个内置策略；指数同样可用）
    if (window.StockRadar.api && window.StockRadar.api.getBacktestStrategies) {
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
      // ⚠️ 兜底：扫描结果必须与图板是同一个标的。服务端未重启（旧进程不认 market）时，
      // 指数会被当成同码个股扫（000001 上证指数 → 平安银行），结果依然「成功」返回，
      // 画出来就是张冠李戴。用最新收盘价量级做交叉校验，不一致就拒绝叠加并明确提示。
      let panelClose = null;
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
            // 指数必须带 market，否则后端按个股读 stock_daily，
            // 000001 会取到平安银行（2026-11-09 前指数直接禁用扫描就是这个原因）
            market: isIndex ? "INDEX" : undefined,
          });
          const sig = result.signals || [];
          const scanKline = result.kline || [];
          const scanClose = scanKline.length
            ? Number(scanKline[scanKline.length - 1].close) : null;
          if (panelClose && scanClose && Math.abs(scanClose - panelClose) / panelClose > 0.05) {
            scanStatus.textContent =
              `扫描数据与图表不一致（图 ${panelClose.toFixed(2)} vs 扫描 ${scanClose.toFixed(2)}），`
              + "疑似同码不同标的：请重启服务后重试";
            scanStatus.classList.add("err");
            return;
          }
          chart.setOverlay({ type: "scan", signals: sig });
          const buys = sig.filter(s => s.buy).length;
          const sells = sig.filter(s => s.sell).length;
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

      // ── 工具栏联动：周期 / 技术面图层 / 信号布局 ──
      const TA_VALUES = ["off", "retracement", "channel", "fan", "trend", "bands",
                         "structure", "regression", "gaps", "all"];
      const LAYOUT_VALUES = ["pinned", "lanes"];
      const pickOne = (v, arr, dft) => (arr.indexOf(v) >= 0 ? v : dft);
      let savedTa = "off";
      let savedLayout = "pinned";
      try {
        savedTa = pickOne(localStorage.getItem("kline_ta_layer"), TA_VALUES, "off");
        savedLayout = pickOne(localStorage.getItem("kline_signal_layout"), LAYOUT_VALUES, "pinned");
      } catch (e) { /* 隐私模式下 localStorage 可能抛错 */ }
      const taSel = toolbar.querySelector(".tpt-ta");
      if (taSel) taSel.value = savedTa;
      toolbar.querySelectorAll("[data-layout]").forEach((b) => {
        b.classList.toggle("active", b.dataset.layout === savedLayout);
      });
      // setter 只记录状态、不要求已有数据，先设好再取数，首帧就是用户选的样子
      try {
        chart.setTaMode(savedTa);
        chart.setSignalLayout(savedLayout);
      } catch (e) { /* 老版本 KlineChart 没有这两个 setter，忽略 */ }

      /* 技术面图层不是纯前端绘制：几何线由后端 /api/chart/ta/{code} 算好再喂给
         chart.setTaData()。弹窗里由它的 _loadTa() 负责，图板必须自己做同一件事，
         否则切了图层什么都不会出现（2026-11-10 首版漏了这一步）。按 code:days:market
         缓存一份，来回切层不重复请求。 */
      let taMode = savedTa;
      let taCache = { key: "", data: null };
      async function ensureTaAnnotations() {
        if (taMode === "off") {
          try { chart.setTaData(null); } catch (e) { /* ignore */ }
          return;
        }
        // 后端 /api/chart/ta 要求 days>=60（少于 60 根摆点不够，算不出几何线）；
        // 「1月(30)」窗口先按 60 根算，前端再按日期映射回图上的 bar。
        const taDays = Math.max(60, Number(days) || 250);
        const key = `${code}:${taDays}:${market}`;
        if (taCache.key === key) {
          try { chart.setTaData(taCache.data); } catch (e) { /* ignore */ }
          return;
        }
        try {
          if (!window.StockRadar.api || !window.StockRadar.api.getTaAnnotations) return;
          const data = await window.StockRadar.api.getTaAnnotations(code, taDays, market);
          taCache = { key, data };
          chart.setTaData(data);
        } catch (e) {
          taCache = { key: "", data: null };
          try { chart.setTaData(null); } catch (_) { /* ignore */ }
        }
      }

      if (taSel) {
        taSel.addEventListener("change", async () => {
          taMode = pickOne(taSel.value, TA_VALUES, "off");
          try { chart.setTaMode(taMode); } catch (e) { /* ignore */ }
          try { localStorage.setItem("kline_ta_layer", taMode); } catch (e) { /* ignore */ }
          await ensureTaAnnotations();
        });
      }
      toolbar.querySelectorAll("[data-layout]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const v = pickOne(btn.dataset.layout, LAYOUT_VALUES, "pinned");
          toolbar.querySelectorAll("[data-layout]").forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          try { chart.setSignalLayout(v); } catch (e) { /* 同上 */ }
          try { localStorage.setItem("kline_signal_layout", v); } catch (e) { /* ignore */ }
        });
      });

      // 取数（周期切换时复用）：图 + 信号 + 读数条一起刷新
      async function loadKline() {
        chart.showLoading("K线数据加载中...");
        const [klineRes, signalsRes] = await Promise.all([
          // 重启/预热窗口内 K 线接口也是 503：必须重试，否则图板会一直停在错误态
          _fetchJsonRetry(`/api/chart/kline/${encodeURIComponent(code)}?days=${days}${marketQ}`,
                          { attempts: 6 }),
          fetch(`/api/chart/signals/${encodeURIComponent(code)}?days=${days}${marketQ}`)
            .then(r => (r.ok ? r.json() : null))
            .catch(() => null),
        ]);
        if (!klineRes || !klineRes.kline || !klineRes.kline.length) {
          throw new Error("empty kline");
        }
        klineRes.signals = (signalsRes && signalsRes.signals) || [];
        await chart.setData(klineRes);
        // 记下图板最新收盘价：策略扫描后用它校验「扫的到底是不是同一个标的」
        try {
          const kl = klineRes.kline || [];
          panelClose = kl.length ? Number(kl[kl.length - 1].close) : null;
        } catch (e) { panelClose = null; }
        // 技术面图层按新周期重算（缓存 key 含 days，切周期会自动重取）
        await ensureTaAnnotations();
        // 指标读数与图一起出：数值/结论在这里，图里只有形状
        try { _renderReadout(readout, klineRes); } catch (e) { readout.hidden = true; }
      }

      toolbar.querySelectorAll("[data-days]").forEach((btn) => {
        if (Number(btn.dataset.days) === days) btn.classList.add("active");
        btn.addEventListener("click", async () => {
          const next = Number(btn.dataset.days);
          if (!next || next === days) return;
          toolbar.querySelectorAll("[data-days]").forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          days = next;
          try { await loadKline(); }
          catch (e) { try { chart.showError("K线数据暂不可用（本地未同步该标的日线）"); } catch (_) { /* ignore */ } }
        });
      });

      chart.ready.then(() => loadKline()).catch(() => {
        try { chart.showError("K线数据暂不可用（本地未同步该标的日线）"); }
        catch (e) { el.textContent = "技术面图板数据加载失败"; }
      });
    });
  }

  // 渲染单张图表卡片（frag 已 fetch）
  function _buildFragEl(frag) {
    const el = document.createElement("div");
    el.className = "chat-chart-frag";
    el.dataset.chartId = frag.id;
    el.dataset.state = "loading";
    el.textContent = "图表加载中...";
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

    const _fetchAllFrags = (list) => Promise.all(list.map(id =>
      _fetchJsonRetry(`/api/chart/${encodeURIComponent(id)}`).catch(() => null)));

    const paint = (frags) => {
      const ok = frags.filter(Boolean);
      if (ok.length === 0) {
        // 服务重启时整段图表都可能拉不到：给可点击重试，而不是让用户刷新整页
        _showChartError(wrap, "图表加载失败", () => {
          wrap.innerHTML = "";
          return _fetchAllFrags(ids).then(paint);
        });
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
    };

    _fetchAllFrags(ids).then(paint);
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
    title.textContent = `图表总览（${frags.length} 张）`;
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
    let deepReportData = null;
    let cleanText = text;
    /* 一份回答里出现多个仪表盘块时（2026-11-06 事故：report-fast 把上下文里
       上一份报告的尾巴续写进正文，dashboard 节点又追加了一个）：
       - 只渲染**最后一个**（后端正常路径就是往末尾追加的，最后一个才是本轮的）；
       - 正文里**所有**块都要删干净，否则残留的会原样裸露成一坨 JSON。
       旧实现用 .match() 取第一个、.replace() 只删第一处，恰好两个都做错。 */
    const dashRe = /@@DASHBOARD_START@@([\s\S]*?)@@DASHBOARD_END@@/g;
    const dashBlocks = [];
    let dashMatch;
    while ((dashMatch = dashRe.exec(text)) !== null) {
      dashBlocks.push(dashMatch[1]);
    }
    if (dashBlocks.length) {
      // 倒序尝试：最后一个能解析就用它（前面的多半是 LLM 抄来的旧块）
      for (let i = dashBlocks.length - 1; i >= 0; i -= 1) {
        try {
          dashboardData = JSON.parse(dashBlocks[i]);
          break;
        } catch (e) {
          console.warn("[Dashboard] JSON parse failed (#" + (i + 1) + "):",
            e.message, String(dashBlocks[i]).substring(0, 200));
        }
      }
      cleanText = text.replace(/@@DASHBOARD_START@@[\s\S]*?@@DASHBOARD_END@@/g, "");
    }
    // 深度报告哨兵块：卡片由 deep-report-card.js 渲染，消息里只存 rid 等最小信息
    const drMatch = cleanText.match(/@@DEEP_REPORT_START@@([\s\S]*?)@@DEEP_REPORT_END@@/);
    if (drMatch) {
      try {
        deepReportData = JSON.parse(drMatch[1]);
        cleanText = cleanText.replace(/@@DEEP_REPORT_START@@[\s\S]*?@@DEEP_REPORT_END@@/g, "");
      } catch (e) {
        console.warn("[DeepReport] JSON parse failed:", e.message, drMatch[1].substring(0, 200));
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
    if (deepReportData && window.StockRadar && window.StockRadar.deepReportCard) {
      try {
        const card = window.StockRadar.deepReportCard.render(deepReportData);
        if (card) {   // rid 缺失/非法时卡片模块返回 null，不能 insertBefore(null)
          const mdContent = bubble.querySelector(".md-content");
          if (mdContent) mdContent.parentNode.insertBefore(card, mdContent.nextSibling);
          else bubble.appendChild(card);
        }
      } catch (e) {
        console.warn("[DeepReport] render failed:", e.message);
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
      .replace(/@@DEEP_REPORT_START@@[\s\S]*?@@DEEP_REPORT_END@@/g, "")
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

    // 图片附件（v1：图片转文字进对话；这里只做展示）
    const atts = meta && Array.isArray(meta.attachments) ? meta.attachments : [];
    if (atts.length) {
      const box = document.createElement("div");
      box.className = "msg-images";
      atts.forEach((att) => {
        if (!att || !att.id) return;
        const img = document.createElement("img");
        img.src = `/api/uploads/${att.id}`;
        img.alt = att.name || "图片";
        img.loading = "lazy";
        img.onerror = () => {
          img.classList.add("msg-image-expired");
          img.alt = "图片已过期";
        };
        box.appendChild(img);
      });
      if (box.children.length) bubble.appendChild(box);
    }

    wrap.appendChild(contentCol);
    container.appendChild(wrap);

    if (animate) scrollBottom(container);
    return bubble;
  }

  /* 贴底。⚠️ #messageList 自身没有 overflow-y（真正滚动的是 .chat-pane），
     直接写 container.scrollTop 无效；且「用户已上滚暂停跟随」时不该再抢滚动位置。 */
  function scrollBottom(container) {
    const follow = window.StockRadar && window.StockRadar.chatFollow;
    if (follow && !follow.isAtBottom()) return;   // 用户已上滚 → 不打断阅读
    requestAnimationFrame(() => {
      const el = _scrollParent(container);
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  /* 只按 overflowY 判定滚动容器：加上「当前是否溢出」的条件会在空/短对话时
     跳过 .chat-pane 而落到 body，之后所有贴底都静默失效。 */
  function _scrollParent(el) {
    let n = el;
    while (n && n !== document.body && n !== document.documentElement) {
      const s = window.getComputedStyle(n);
      if (/(auto|scroll|overlay)/.test(s.overflowY)) return n;
      n = n.parentElement;
    }
    return el;
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
