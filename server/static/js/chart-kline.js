/*
   选股雷达 Web - K 线图组件
   配色方案：与项目整体淡绿色风格保持一致
*/

window.StockRadar = window.StockRadar || {};

(function () {
  "use strict";

  const ECHARTS_CDN = "/static/vendor/echarts.min.js";  // 本地 vendor 优先：jsdelivr 在部分网络（含本机）不可达，会让全站图表静默不渲染

  // 配色方案 - 与项目整体淡绿色风格一致
  const COLORS = {
    // 涨跌颜色 - 中国A股标准：红涨绿跌
    up: "#e74c3c",        // 涨 - 红色
    down: "#176b5b",      // 跌 - 品牌绿色
    upBg: "rgba(231, 76, 60, 0.08)",
    downBg: "rgba(23, 107, 91, 0.08)",

    // 文字颜色 - 与项目一致
    text: "#1c2430",      // 主文字
    muted: "#667085",     // 次要文字
    light: "#9ca3af",     // 浅色文字

    // 网格和边框 - 与项目一致
    grid: "#e5e7eb",      // 网格线
    border: "#dfe4ea",    // 边框
    bg: "#ffffff",        // 背景
    bgHover: "#f6f7f9",   // 悬停背景

    // 均线颜色 - 柔和配色
    ma5: "#f59e0b",       // MA5 - 琥珀色
    ma10: "#176b5b",      // MA10 - 品牌绿色
    ma20: "#8b5cf6",      // MA20 - 紫色
    ma60: "#06b6d4",      // MA60 - 青色

    // 成交量颜色
    volumeUp: "rgba(231, 76, 60, 0.5)",
    volumeDown: "rgba(23, 107, 91, 0.5)",

    // MACD 颜色：快线品牌绿 = 视线落点，慢线中灰；柱体保留红涨绿跌
    macdDif: "#176b5b",   // DIF - 快线
    macdDea: "#8f8e88",   // DEA - 慢线（中灰）
    macdHistUp: "rgba(231, 76, 60, 0.6)",
    macdHistDown: "rgba(23, 107, 91, 0.6)",

    // KDJ 颜色：K 品牌绿 / D 中灰实线 / J 浅灰点线
    // 第三通道是"线型"而不是"色相"——色觉异常或灰度打印时仍能分出三条线
    kdjK: "#176b5b",      // K线 - 快线（主角）
    kdjD: "#8f8e88",      // D线 - 慢线（中灰）
    kdjJ: "#a9a8a1",      // J线 - 第三线（浅灰 + 点线）

    // RSI 颜色：6 快 / 12 中 / 24 浅，同一条明度梯
    rsi6: "#176b5b",
    rsi12: "#8f8e88",
    rsi24: "#a9a8a1",

    // DPO 颜色：快线品牌绿，慢线中灰
    dpo: "#176b5b",      // DPO - 快线（主角）
    madpo: "#8f8e88",    // MADPO - 慢线（中灰）

    // 布林带画在主图上，红绿会和蜡烛抢语义，改为中性灰 + 虚线轨
    bollUpper: "#c2c1ba",
    bollMid: "#8f8e88",
    bollLower: "#c2c1ba",

    // 信号颜色
    signalBuy: "#e74c3c",
    signalSell: "#176b5b",

    // 指标降权色 —— 均线等"背景信息"统一用它，身份交给末端数字
    indicatorMuted: "#c2c1ba",
    indicatorLabel: "#a9a8a1",
    labelHalo: "#ffffff",
  };

  // 信号统一元表：每个指标用单独字母标识（M=MACD, K=KDJ, A=均线, B=布林, D=DPO），
  // 形状区分信号类型（star=金叉, cross=死叉, circle=穿0轴, square=突破），颜色区分好/坏（红=好/绿=坏，A股习惯）。
  // 图上只显示字母，不显示冗长文字；悬停 tooltip 才显示完整名称。

  const SIGNAL_META = {
    macd_golden:     { letter: "M", shape: "star",   good: true  },
    macd_death:      { letter: "M", shape: "cross",  good: false },
    kdj_golden:      { letter: "K", shape: "star",   good: true  },
    kdj_death:       { letter: "K", shape: "cross",  good: false },
    ma_golden:       { letter: "A", shape: "star",   good: true  },
    ma_death:        { letter: "A", shape: "cross",  good: false },
    boll_break_up:   { letter: "B", shape: "square", good: true  },
    boll_break_down: { letter: "B", shape: "square", good: false },
    dpo_golden:      { letter: "D", shape: "star",   good: true  },
    dpo_death:       { letter: "D", shape: "cross",  good: false },
    dpo_bull:        { letter: "D", shape: "circle", good: true  },
    dpo_bear:        { letter: "D", shape: "circle", good: false },
    /* AI 信号评测跳转标记（openKlineChart(code, {markRange})） */
    buy_mark:        { letter: "B", shape: "star",   good: true  },
    sell_mark:       { letter: "S", shape: "cross",  good: false },
  };

  // 信号形状 -> ECharts symbol（星/叉用 SVG path，圆/方用内置图形）
  const SHAPE_SYMBOL = {
    star:   "path://M500 80 L610 380 L920 380 L670 580 L770 880 L500 690 L230 880 L330 580 L80 380 L390 380 Z",
    cross:  "path://M220 140 L420 340 L620 140 L860 140 L620 400 L860 660 L620 860 L420 660 L220 860 L80 660 L320 400 L80 140 Z",
    circle: "circle",
    square: "rect",
  };

  // 信号布局（贴图 / 分道）：
  //   pinned = 信号贴着 K 线（D1 的布局，默认，也是回退路径——不能有任何像素变化）
  //   lanes  = 按指标族分五条轨道，画在价格区与成交量之间
  const SIGNAL_LAYOUT_KEY = "kline_signal_layout";
  const SIGNAL_LAYOUTS = ["pinned", "lanes"];

  // 技术面图层（斐波那契三种 / 自动趋势线 / 支撑阻力带）
  // ⚠️ 这些线是**算法生成的测量工具**，不是交易信号；默认关，跨会话记住选择。
  // 全部由后端 tools/ta_annotations.py 算纯几何，前端只负责画。
  const TA_KEY = "kline_ta_layer";
  const TA_MODES = ["off", "retracement", "channel", "fan", "trend", "bands",
                    "structure", "regression", "gaps", "all"];
  const TA_LABELS = {
    retracement: "斐波那契回撤",
    channel: "斐波那契通道",
    fan: "斐波那契扇形",
    trend: "自动趋势线",
    bands: "支撑阻力带",
    structure: "波段结构",
    regression: "回归通道",
    gaps: "跳空缺口",
    all: "全显示",
  };
  // 标注线一律走「发丝线」语法：1px 上下、不上色说方向，身份交给末端文字。
  const TA_STYLE = {
    retracement: { color: "#667085", type: "dashed", width: 1 },
    channel: { color: "#176b5b", type: "solid", width: 1.2 },
    fan: { color: "#8f8e88", type: "dashed", width: 1 },
    trend: { color: "#1c2430", type: "solid", width: 1.2 },
    support: "rgba(23, 107, 91, 0.10)",
    resistance: "rgba(245, 158, 11, 0.12)",
  };

  function normalizeTaMode(value) {
    return TA_MODES.indexOf(value) >= 0 ? value : "off";
  }

  // 分道行位：行 = 指标族（字母即族），y 取每条道的中心（0.5 .. 4.5）。
  // 按指标族分道是零碰撞的——同一指标同一天不会既金叉又死叉，所以同一格里
  // 永远不会出现两个信号，字母不用做横向避让。
  const LANE_Y = { M: 0.5, K: 1.5, A: 2.5, B: 3.5, D: 4.5 };
  const LANE_Y_FALLBACK = 2.5;  // 未知族回退到均线道
  const LANE_LABELS = ["M", "K", "A", "B", "D"];  // 轨道左侧行标（自上而下）

  function normalizeSignalLayout(value) {
    return SIGNAL_LAYOUTS.indexOf(value) >= 0 ? value : "pinned";
  }

  function readSignalLayout() {
    return normalizeSignalLayout(localStorage.getItem(SIGNAL_LAYOUT_KEY));
  }

  function ensureECharts() {
    return new Promise((resolve, reject) => {
      if (window.echarts) {
        resolve(window.echarts);
        return;
      }
      const script = document.createElement("script");
      script.src = ECHARTS_CDN;
      script.onload = () => resolve(window.echarts);
      script.onerror = () => reject(new Error("ECharts 加载失败"));
      document.head.appendChild(script);
    });
  }

  function esc(value) {
    const el = document.createElement("div");
    el.textContent = value == null ? "" : String(value);
    return el.innerHTML;
  }

  class KlineChart {
    constructor(container) {
      this.container = container;
      this.chart = null;
      this.data = null;
      this.overlay = null;  // {type:'scan'|'backtest', signals/trades/equityCurve/stats}
      this.currentIndicator = "macd";
      // 信号布局：pinned（贴图，默认）/ lanes（按指标族分道），跨会话记住选择
      this.signalLayout = readSignalLayout();
      // 技术面图层：模式 + 后端返回的几何结果（off 时不拉数据）
      this.taMode = "off";
      this.taData = null;
      this.ready = this.init();
    }

    async init() {
      const echartsLib = await ensureECharts();
      this.chart = echartsLib.init(this.container);
      this._setupResize();
      this._setupClickInfo();
      this.showPlaceholder("输入股票代码后按回车查看 K 线");
    }

    // 点击 K 线任意位置 → 显示"该日收盘 → 最新收盘"的收益卡片（按收盘价计，未计费用）
    _setupClickInfo() {
      this.chart.on("click", (params) => {
        if (params.seriesType === "candlestick") {
          this._showClickInfo(params.dataIndex);
        } else {
          this._hideClickInfo();
        }
      });
    }

    _showClickInfo(kIdx) {
      const kline = this.data && this.data.kline;
      if (!kline || kline.length < 2 || kIdx == null) return;
      const item = kline[kIdx];
      const latest = kline[kline.length - 1];
      if (!item || !latest || !item.close || !latest.close) return;
      const pct = (latest.close / item.close - 1) * 100;
      const days = kline.length - 1 - kIdx;
      const el = this._clickInfoEl();
      el.innerHTML =
        `<div class="kci-date">📍 ${esc(item.date)} → ${esc(latest.date)}</div>` +
        `<div class="kci-row">收盘 <b>${this._fmtPrice(item.close)}</b> → <b>${this._fmtPrice(latest.close)}</b></div>` +
        `<div class="kci-pct ${pct >= 0 ? "up" : "down"}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</div>` +
        `<div class="kci-note">持有 ${days} 个交易日 · 按收盘价计，未计费用</div>`;
      el.style.display = "block";
    }

    _hideClickInfo() {
      const el = this._clickInfoEl();
      el.style.display = "none";
    }

    _clickInfoEl() {
      if (!this._clickInfoDom) {
        const el = document.createElement("div");
        el.className = "kline-click-info";
        el.style.display = "none";
        this.container.appendChild(el);
        this._clickInfoDom = el;
      }
      return this._clickInfoDom;
    }

    _setupResize() {
      window.addEventListener("resize", () => this.resize());
      if (window.ResizeObserver) {
        new ResizeObserver(() => this.resize()).observe(this.container);
      }
    }

    resize() {
      if (this.chart) this.chart.resize();
    }

    async setData(data) {
      await this.ready;
      this.data = data;
      this.render();
    }

    /* 持仓成本线（openKlineChart(code, {costLine})），load 后消费一次 */
    setCostLine(costLine) {
      this.costLine = costLine != null ? Number(costLine) : null;
      if (this.data) this.render();
    }

    async showPlaceholder(text) {
      await this.ready;
      this.chart.setOption({
        backgroundColor: "#fff",
        title: {
          text,
          left: "center",
          top: "center",
          textStyle: { color: COLORS.muted, fontSize: 14, fontWeight: "normal" },
        },
        series: [],
      }, true);
    }

    async showLoading(text) {
      await this.ready;
      this.chart.setOption({
        backgroundColor: "#fff",
        title: {
          text: text || "加载中...",
          left: "center",
          top: "center",
          textStyle: { color: COLORS.muted, fontSize: 14, fontWeight: "normal" },
        },
        series: [],
      }, true);
    }

    async showError(text) {
      await this.ready;
      this.chart.setOption({
        backgroundColor: "#fff",
        title: {
          text,
          left: "center",
          top: "center",
          textStyle: { color: COLORS.up, fontSize: 14, fontWeight: "normal" },
        },
        series: [],
      }, true);
    }

    setSubIndicator(type) {
      this.currentIndicator = type;
      if (this.data) this.render();
    }

    setChartType(type) {
      this.chartType = type;
      if (this.data) this.render();
    }

    // 切换信号布局（贴图 / 分道）。只重绘，不重新取数。
    setSignalLayout(layout) {
      this.signalLayout = normalizeSignalLayout(layout);
      if (this.data) this.render();
    }

    // 切换技术面图层（off / 五种 / all）。只重绘，不重新取数。
    setTaMode(mode) {
      this.taMode = normalizeTaMode(mode);
      if (this.data) this.render();
    }

    // 喂入后端算好的几何（tools/ta_annotations.py 的输出）。
    // ok=false（本地日线不足/接口异常）时清空，图不画半截线。
    setTaData(data) {
      this.taData = data && data.ok ? data : null;
      if (this.data && this.taMode !== "off") this.render();
    }

    // 叠加策略信号/回测结果（scan 信号点 或 backtest 买卖点+净值曲线）
    setOverlay(overlay) {
      this.overlay = overlay || null;
      if (this.data) this.render();
    }

    clearOverlay() {
      if (!this.overlay) return;
      this.overlay = null;
      if (this.data) this.render();
    }

    render() {
      if (!this.chart || !this.data || !this.data.kline.length) return;

      const { kline, indicators, code, name, signals = [] } = this.data;
      const dates = kline.map((item) => item.date);
      const ohlc = kline.map((item) => [item.open, item.close, item.low, item.high]);
      const volumes = kline.map((item) => item.volume);

      const subTitleData = this._getSubTitleData(kline);

      // 回测 overlay 含净值曲线时启用第 4 个 grid（净值与 MACD 量纲悬殊，独立副图）
      const overlay = this.overlay;
      const hasEquity = overlay && overlay.type === "backtest"
        && overlay.equityCurve && overlay.equityCurve.length > 0;
      const gridCount = hasEquity ? 4 : 3;
      const allGrids = Array.from({ length: gridCount }, (_, i) => i);

      // 信号分道：轨道 grid 追加到数组末尾（grid 数组顺序与视觉位置无关，
      // 位置只由 top/height 决定），靠 top 摆到价格区与成交量之间。这样不必改动
      // 任何现有 series 的 xAxisIndex/yAxisIndex（成交量仍是 1、副图 2、净值 3）。
      const lanes = this.signalLayout === "lanes";
      const lanesIdx = lanes ? gridCount : null;   // 轨道拿到的新索引（数组末尾）
      const lanesTop = hasEquity ? 47 : 54;        // 轨道顶边（%）
      const lanesHeight = hasEquity ? 8.5 : 9.5;   // 轨道高度（%，五条道均分）
      // 缩放必须带上轨道索引，否则拖动/滚轮时轨道不跟随
      const zoomAxes = lanes ? allGrids.concat(lanesIdx) : allGrids;

      // 技术面标注：索引按日期映射回图上的 bar（见 _buildTaSeries 注释）
      const ta = this._buildTaSeries(dates);
      // 标注线常常落在价格区间之外（平行轨、支撑带上沿），放任它们参与 Y 轴刻度
      // 计算会把蜡烛压扁——主图刻度锁定在价格区间，超出的线自然裁掉。
      const taRange = ta.series.length ? this._taAxisRange() : null;

      this.chart.setOption({
        animation: false,
        backgroundColor: COLORS.bg,
        // 标题区域
        title: {
          text: `${name || code} ${code}`,
          left: 12,
          top: 8,
          textStyle: {
            color: COLORS.text,
            fontSize: 16,
            fontWeight: "700",
            fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
          },
          subtext: `{price|${subTitleData.price}}  {change|${subTitleData.pctChange}}  {vol|${subTitleData.volume}}`,
          subtextStyle: {
            fontSize: 12,
            rich: {
              price: { color: COLORS.text, fontSize: 13, fontWeight: "600", fontFamily: "'DIN Alternate', 'Tabular Nums', 'Courier New', monospace" },
              change: { color: subTitleData.color, fontSize: 13, fontWeight: "700", fontFamily: "'DIN Alternate', 'Tabular Nums', 'Courier New', monospace" },
              vol: { color: COLORS.muted, fontSize: 11, fontFamily: "'DIN Alternate', 'Tabular Nums', 'Courier New', monospace" },
            },
          },
        },
        // 提示框 - 东财风格
        tooltip: {
          trigger: "axis",
          axisPointer: {
            type: "cross",
            crossStyle: { color: COLORS.light },
            lineStyle: { color: COLORS.light, type: "dashed" },
          },
          backgroundColor: "rgba(255, 255, 255, 0.96)",
          borderColor: COLORS.border,
          borderWidth: 1,
          padding: [8, 12],
          textStyle: { color: COLORS.text, fontSize: 12 },
          confine: true,
          formatter: (params) => this._formatTooltip(params, kline, signals),
        },
        // 图例
        legend: {
          data: ["MA5", "MA10", "MA20", "MA60"],
          top: 10,
          right: 12,
          itemWidth: 14,
          itemHeight: 2,
          textStyle: { color: COLORS.muted, fontSize: 11 },
          inactiveColor: COLORS.light,
        },
        // 十字光标
        axisPointer: {
          link: [{ xAxisIndex: "all" }],
          label: {
            backgroundColor: COLORS.text,
            color: "#fff",
            fontSize: 11,
            padding: [4, 8],
            borderRadius: 2,
          },
        },
        // 分道轨道行标（M/K/A/B/D）——只做只读标注，不做交互
        // 技术面图层的角落说明跟分道行标共用 graphic（数组拼接，互不影响）
        graphic: [
          ...(lanes ? this._buildLaneLabels(lanesTop, lanesHeight) : []),
          ...ta.graphic,
        ],
        // 网格布局 - 回测有净值时多一栏；分道时价格区按规格 §3.3 让出轨道
        grid: lanes
          ? (hasEquity
              ? [
                  { left: "8%", right: "6%", top: "12%", height: "34%" },    // K线主图
                  { left: "8%", right: "6%", top: "57.5%", height: "7.5%" }, // 成交量
                  { left: "8%", right: "6%", top: "67%", height: "10%" },    // 技术指标
                  { left: "8%", right: "6%", top: "79%", height: "10%" },    // 净值曲线
                  { left: "8%", right: "6%", top: `${lanesTop}%`, height: `${lanesHeight}%` },  // 信号轨道（末尾索引 = 4）
                ]
              : [
                  { left: "8%", right: "6%", top: "12%", height: "41%" },   // K线主图
                  { left: "8%", right: "6%", top: "65.5%", height: "9%" },  // 成交量
                  { left: "8%", right: "6%", top: "77%", height: "13%" },   // 技术指标
                  { left: "8%", right: "6%", top: `${lanesTop}%`, height: `${lanesHeight}%` },  // 信号轨道（末尾索引 = 3）
                ])
          : hasEquity
          ? [
              { left: "8%", right: "6%", top: "12%", height: "42%" },  // K线主图
              { left: "8%", right: "6%", top: "58%", height: "8%" },   // 成交量
              { left: "8%", right: "6%", top: "70%", height: "12%" },  // 技术指标
              { left: "8%", right: "6%", top: "86%", height: "10%" },  // 净值曲线
            ]
          : [
              { left: "8%", right: "6%", top: "12%", height: "50%" },  // K线主图
              { left: "8%", right: "6%", top: "66%", height: "10%" },  // 成交量
              { left: "8%", right: "6%", top: "80%", height: "14%" },  // 技术指标
            ],
        // X轴 - 动态数量（分道时追加轨道的时间轴）
        xAxis: [
          ...allGrids.map((gridIndex) => ({
            type: "category",
            data: dates,
            gridIndex,
            axisLine: { lineStyle: { color: COLORS.border } },
            axisTick: { show: false },
            axisLabel: {
              color: COLORS.muted,
              show: gridIndex === gridCount - 1,
              rotate: 0,
              fontSize: 10,
              margin: 8,
            },
            // 多 grid 联动时每个轴都会弹一枚 axisPointer 标签，右缘会叠出
            // 三四个重复日期；只在主图那根轴上显示。
            axisPointer: { label: { show: gridIndex === 0 } },
            splitLine: { show: false },
          })),
          // 轨道时间轴：与其余 grid 共用同一份 category，只隐藏刻度文字
          ...(lanes ? [{
            type: "category",
            data: dates,
            gridIndex: lanesIdx,
            axisLine: { lineStyle: { color: COLORS.border } },
            axisTick: { show: false },
            axisLabel: { show: false },
            axisPointer: { label: { show: false } },
            splitLine: { show: false },
          }] : []),
        ],
        // Y轴
        yAxis: [
          { ...this._axisConfig(0, true), ...(taRange || {}) },
          this._axisConfig(1, false, (value) => this._formatVolume(value)),
          this._axisConfig(2, true),
          ...(hasEquity ? [this._axisConfig(3, true)] : []),
          // 分道轨道：0-5 的隐藏数值轴，每条道 y = 0.5 / 1.5 / … / 4.5。
          // inverse 让 MACD(0.5) 排在最上面，行序与行标 M/K/A/B/D 一致。
          ...(lanes ? [{
            type: "value",
            gridIndex: lanesIdx,
            min: 0,
            max: 5,
            inverse: true,
            show: false,
          }] : []),
        ],
        // 缩放控件
        dataZoom: [
          { type: "inside", xAxisIndex: zoomAxes, start: 0, end: 100 },
          {
            type: "slider",
            xAxisIndex: zoomAxes,
            bottom: 4,
            height: 18,
            borderColor: "#dfe4ea",
            backgroundColor: "#f8fafc",
            fillerColor: "rgba(23, 107, 91, 0.12)",
            handleStyle: { color: "#94a3b8" },
            textStyle: { color: COLORS.muted },
          },
        ],
        series: [
          // K线 - 根据 chartType 渲染不同样式
          ...this._buildMainSeries(kline, ohlc, dates, name, code),
          ...this._buildMASeries(indicators),
          // 技术面标注画在蜡烛/均线之上、信号之下（信号才是要抢视线的）
          ...ta.series,
          ...this._buildSignalSeries(signals, dates, kline, lanesIdx),
          // 结构/缺口这类「点上带字」的标注必须排在信号之后，否则字母被信号盖住
          ...ta.top,
          // 策略 overlay：scan 信号点 / 回测买卖点
          ...(overlay ? this._buildOverlaySeries(overlay, dates, kline) : []),
          // 持仓成本线（紫色虚线，避开红涨绿跌语义；symbol:none 不画端点）
          ...(this.costLine != null && isFinite(this.costLine)
            ? [this._buildCostLineSeries(this.costLine)] : []),
          // 成交量 - 涨跌配色
          {
            name: "成交量",
            type: "bar",
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: volumes,
            itemStyle: {
              color: (params) => {
                const k = kline[params.dataIndex];
                return k && k.close >= k.open ? COLORS.volumeUp : COLORS.volumeDown;
              },
            },
            barWidth: "60%",
            barMaxWidth: 20,
          },
          ...this._buildSubIndicatorSeries(indicators),
          // 回测净值曲线（第 4 栏）
          ...(hasEquity ? this._buildEquitySeries(overlay.equityCurve, dates, overlay.initialCash) : []),
        ],
      }, true);
    }

    // ========= 技术面标注 =========
    // 后端给的索引是「它自己取的 N 根窗口」内的相对序号；这里先按日期映射回图上的 bar，
    // 再画。窗口差一两根（增量同步 / 停牌）时直接复用 index 会让整层线横向漂移。
    // 所有标注 series 名统一以 "TA·" 开头，tooltip 里据此过滤掉。
    _buildTaSeries(dates) {
      const empty = { series: [], top: [], graphic: [] };
      const mode = this.taMode;
      const payload = this.taData;
      if (mode === "off" || !payload || !payload.layers) return empty;

      const barCount = dates.length;
      const taDates = Array.isArray(payload.dates) ? payload.dates : [];
      const dateToBar = new Map(dates.map((d, i) => [String(d), i]));
      const toBar = (taIdx) => {
        const dt = taDates[taIdx];
        if (dt != null && dateToBar.has(String(dt))) return dateToBar.get(String(dt));
        return (taIdx >= 0 && taIdx < barCount) ? taIdx : null;
      };
      const fmtDate = (taIdx) => (taDates[taIdx] ? String(taDates[taIdx]).slice(5) : "?");

      const num = (v) => (v == null ? null : Number(v));
      const blank = () => new Array(barCount).fill(null);
      const lastBar = barCount - 1;
      const want = (key) => mode === "all" || mode === key;
      const series = [];
      // 点标注（结构字母/缺口文字）要画在信号字母之后：ECharts 的 series z 只管
      // 图形，文字的层叠跟着数组顺序走，混在 series 里会被 K/M/A 压住。
      const top = [];
      const markLines = [];
      const markAreas = [];
      const span = this._taVisibleSpan();

      const lineSeries = (name, data, style) => ({
        name: `TA·${name}`,
        type: "line",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data,
        symbol: "none",
        smooth: false,
        silent: true,
        z: 2,
        lineStyle: {
          color: style.color,
          width: style.width || 1,
          type: style.type || "solid",
          opacity: style.opacity == null ? 1 : style.opacity,
        },
      });

      // 水平线走 markLine；price 相邻太近就只画线不标字，避免标签叠死。
      // 阈值按"视窗价格跨度"算：9px 字高在 50% 高的价格区里约等于 5%~6% 的跨度，
      // 取 6%——7 档回撤全标时（600519 只相距 12~15 元）会糊成一团。
      let prevLabelPrice = null;
      const pushLevel = (price, label, style) => {
        const p = num(price);
        if (p == null) return;
        const dense = prevLabelPrice != null && span > 0
          && Math.abs(p - prevLabelPrice) / span < 0.06;
        if (!dense) prevLabelPrice = p;
        markLines.push({
          yAxis: p,
          lineStyle: {
            color: style.color,
            width: style.width || 1,
            type: style.type || "dashed",
            opacity: style.opacity == null ? 0.85 : style.opacity,
          },
          label: {
            show: !dense && !!label,
            formatter: label || "",
            color: style.color,
            fontSize: 9,
            // 标在左端：右端是均线末端数字和最新价标签的地盘，会叠字
            position: "insideStartTop",
            // 半年窗口里回撤位常常落在蜡烛上，加一层纸色底保证能读
            backgroundColor: "rgba(255, 255, 255, 0.82)",
            padding: [1, 3],
            borderRadius: 2,
          },
        });
      };

      const fib = payload.layers.fib || {};

      // ① 斐波那契水平回撤
      if (want("retracement") && fib.retracement && Array.isArray(fib.retracement.levels)) {
        const r = fib.retracement;
        r.levels.forEach((lv) => {
          const ratio = Number(lv.ratio);
          // 只标比例不标价格：价格由线的纵向位置从 Y 轴读，标两遍只会叠字
          pushLevel(lv.price, `${(ratio * 100).toFixed(1)}%`, TA_STYLE.retracement);
        });
      }

      // ② 斐波那契通道：主段两端定基准线，平行轨按振幅比例外推
      if (want("channel") && fib.channel && fib.channel.from && fib.channel.to) {
        const ch = fib.channel;
        const i1 = toBar(ch.from.i), i2 = toBar(ch.to.i);
        const p1 = num(ch.from.price), p2 = num(ch.to.price);
        if (i1 != null && i2 != null && i1 !== i2) {
          const slope = (p2 - p1) / (i2 - i1);
          const startBar = Math.min(i1, i2);
          (ch.rails || []).forEach((rail) => {
            const ratio = Number(rail.ratio);
            const off = num(rail.offset) || 0;
            const data = blank();
            for (let x = startBar; x <= lastBar; x++) {
              data[x] = p1 + slope * (x - i1) + off;
            }
            series.push(lineSeries(
              `通道 ${(ratio * 100).toFixed(1)}%`,
              data,
              {
                color: TA_STYLE.channel.color,
                width: ratio === 0 ? 1.3 : 0.9,
                type: (ratio === 0.382 || ratio === 0.618) ? "dashed" : "solid",
                opacity: mode === "all" ? 0.7 : 1,
              }
            ));
          });
        }
      }

      // ③ 斐波那契扇形：从锚点按比例放射
      if (want("fan") && fib.fan && fib.fan.anchor && fib.fan.target) {
        const fan = fib.fan;
        const xa = toBar(fan.anchor.i), xt = toBar(fan.target.i);
        const pa = num(fan.anchor.price), pt = num(fan.target.price);
        if (xa != null && xt != null && xt !== xa) {
          (fan.rays || []).forEach((ray) => {
            const ratio = Number(ray.ratio);
            if (!(ratio > 0)) return;
            const data = blank();
            for (let x = xa; x <= lastBar; x++) {
              data[x] = pa + (pt - pa) * ratio * (x - xa) / (xt - xa);
            }
            series.push(lineSeries(
              `扇形 ${(ratio * 100).toFixed(1)}%`,
              data,
              {
                color: TA_STYLE.fan.color,
                width: ratio === 1 ? 1.1 : 0.8,
                type: "dashed",
                opacity: mode === "all" ? 0.55 : 0.9,
              }
            ));
          });
        }
      }

      // ④ 自动趋势线（最近窗口内被最多摆动点验证的那条），延伸到最新一根
      const trend = payload.layers.trend || {};
      const drawTrend = (line, label) => {
        if (!line) return;
        const x1 = toBar(line.x1), x2 = toBar(line.x2);
        const k = num(line.k), y1 = num(line.y1);
        if (x1 == null || k == null || y1 == null) return;
        const data = blank();
        const startBar = Math.min(x1, x2 == null ? x1 : x2);
        for (let x = startBar; x <= lastBar; x++) data[x] = y1 + k * (x - x1);
        series.push(lineSeries(label, data, {
          color: TA_STYLE.trend.color,
          width: 1.2,
          type: "solid",
          opacity: mode === "all" ? 0.6 : 0.9,
        }));
      };
      if (want("trend")) {
        drawTrend(trend.upper, `上轨 ${trend.upper ? trend.upper.hits : 0}次`);
        drawTrend(trend.lower, `下轨 ${trend.lower ? trend.lower.hits : 0}次`);
      }

      // ⑤ 支撑阻力带：区间填充 + 中线标触碰次数
      const bands = Array.isArray(payload.layers.bands) ? payload.layers.bands : [];
      if (want("bands") && bands.length) {
        const sorted = bands.slice().sort((a, b) => Number(b.touches) - Number(a.touches));
        let prevBandPrice = null;
        // 全显示时八层叠在一起，四条带的文字会互相压住——每侧只标触碰最多的那条，
        // 其余的带保留区间底色和细中线，信息不丢、字不糊。
        const labelBudget = { support: 1, resistance: 1 };
        sorted.forEach((b) => {
          const lo = num(b.lo), hi = num(b.hi), mid = num(b.mid);
          if (lo == null || hi == null || mid == null) return;
          const isSupport = b.kind === "support";
          const tint = isSupport ? TA_STYLE.support : TA_STYLE.resistance;
          const accent = isSupport ? "#176b5b" : "#b7791f";
          markAreas.push([
            { yAxis: lo, itemStyle: { color: tint } },
            { yAxis: hi },
          ]);
          let dense = prevBandPrice != null && span > 0
            && Math.abs(mid - prevBandPrice) / span < 0.05;
          if (mode === "all") {
            if (labelBudget[b.kind] > 0) labelBudget[b.kind] -= 1;
            else dense = true;
          }
          if (!dense) prevBandPrice = mid;
          markLines.push({
            yAxis: mid,
            lineStyle: { color: accent, width: 0.8, type: "dotted", opacity: 0.5 },
            label: {
              show: !dense,
              formatter: `${isSupport ? "支撑" : "压力"} ${this._taFmt(lo)}–${this._taFmt(hi)} · ${b.touches}次`,
              color: accent,
              fontSize: 9,
              position: "insideStartTop",
              backgroundColor: "rgba(255, 255, 255, 0.82)",
              padding: [1, 3],
              borderRadius: 2,
            },
          });
        });
      }

      // ⑥ 波段结构：HH/HL/LH/LL 标在摆动点上（道氏理论的结构，比数浪客观）
      const structure = Array.isArray(payload.layers.structure) ? payload.layers.structure : [];
      if (want("structure") && structure.length) {
        const pts = [];
        structure.forEach((s) => {
          const x = toBar(s.i);
          if (x == null) return;
          pts.push({ value: [x, Number(s.price)], kind: s.kind });
        });
        if (pts.length) {
          top.push({
            name: "TA·波段结构",
            type: "scatter",
            xAxisIndex: 0,
            yAxisIndex: 0,
            data: pts,
            symbolSize: 4,
            silent: true,
            // 信号散点 z=30，结构字母压在下面会被 K/M/A 盖住（实测 L/LL 直接看不见）
            z: 32,
            itemStyle: { color: "#1c2430", opacity: 0.75 },
            label: {
              show: true,
              position: "top",
              distance: 3,
              formatter: (params) => (params.data && params.data.kind) || "",
              color: "#1c2430",
              fontSize: 9,
              backgroundColor: "rgba(255, 255, 255, 0.82)",
              padding: [1, 2],
              borderRadius: 2,
            },
          });
        }
      }

      // ⑦ 线性回归通道 ±2σ：斜率与带宽都由数据定，不依赖人工选点
      const reg = payload.layers.regression;
      if (want("regression") && reg && reg.from && reg.to) {
        const i1 = toBar(reg.from.i), i2 = toBar(reg.to.i);
        const p1 = num(reg.from.price), p2 = num(reg.to.price);
        const sigma = num(reg.sigma) || 0;
        if (i1 != null && i2 != null && i1 !== i2 && sigma > 0) {
          const k = (p2 - p1) / (i2 - i1);
          [[0, "中轨", 1.3, "solid", "#176b5b"],
           [2, "+2σ", 0.9, "dashed", "#9ca3af"],
           [-2, "-2σ", 0.9, "dashed", "#9ca3af"]].forEach(([mult, text, width, type, color]) => {
            const data = blank();
            const off = mult * sigma;
            for (let x = i1; x <= lastBar; x++) data[x] = p1 + k * (x - i1) + off;
            series.push(lineSeries(`回归${text}`, data, {
              color, width, type, opacity: mode === "all" ? 0.6 : 0.95,
            }));
          });
        }
      }

      // ⑧ 跳空缺口：标在缺口区间中点，未补的才写字（A 股"缺口必补"）
      const gapList = Array.isArray(payload.layers.gaps) ? payload.layers.gaps : [];
      if (want("gaps") && gapList.length) {
        const pts = [];
        gapList.forEach((g) => {
          const x = toBar(g.i);
          if (x == null) return;
          pts.push({ value: [x, (Number(g.lo) + Number(g.hi)) / 2], gap: g });
        });
        if (pts.length) {
          top.push({
            name: "TA·缺口",
            type: "scatter",
            xAxisIndex: 0,
            yAxisIndex: 0,
            data: pts,
            symbol: "diamond",
            symbolSize: 7,
            silent: true,
            // 同结构点：缺口文字要能压住信号字母，否则"缺口 0.21%"只剩半截
            z: 32,
            itemStyle: {
              color: (params) => {
                const g = params.data && params.data.gap;
                return g && g.kind === "up" ? COLORS.up : COLORS.down;
              },
              opacity: 0.85,
            },
            label: {
              // 全显示时缺口标签会跟信号字母、支撑带标签叠在一起，只留菱形标记
              show: mode !== "all",
              position: "right",
              distance: 4,
              formatter: (params) => {
                const g = params.data && params.data.gap;
                if (!g) return "";
                return g.filled ? `缺口 ${g.size_pct}%` : `未补 ${g.size_pct}%`;
              },
              color: COLORS.muted,
              fontSize: 9,
              backgroundColor: "rgba(255, 255, 255, 0.82)",
              padding: [1, 2],
              borderRadius: 2,
            },
          });
        }
      }

      // 水平线/区间挂在一个空 line 系列上（与持仓成本线同一套写法）
      if (markLines.length || markAreas.length) {
        series.push({
          name: "TA·标注",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: [],
          silent: true,
          z: 2,
          markLine: markLines.length
            ? { silent: true, symbol: "none", animation: false, data: markLines }
            : undefined,
          markArea: markAreas.length
            ? { silent: true, animation: false, data: markAreas }
            : undefined,
        });
      }

      return { series, top, graphic: [this._taNoteGraphic(mode, payload, fmtDate)] };
    }

    // 角落说明：用的哪种、依据哪几个摆动点——让人能判断这线值不值得信
    _taNoteGraphic(mode, payload, fmtDate) {
      const fib = (payload.layers && payload.layers.fib) || {};
      const trend = (payload.layers && payload.layers.trend) || {};
      const bands = payload.layers && payload.layers.bands;
      const parts = [];
      const label = TA_LABELS[mode] || mode;

      if (mode === "retracement" && fib.retracement) {
        const r = fib.retracement;
        parts.push(`锚 ${fmtDate(r.from.i)}→${fmtDate(r.to.i)}（${r.up ? "上涨" : "下跌"}段）`);
      } else if ((mode === "channel" || mode === "fan") && fib.channel) {
        const c = fib.channel;
        parts.push(`基线 ${fmtDate(c.from.i)}→${fmtDate(c.to.i)}`);
        if (c.unit != null) parts.push(`轨距 ${this._fmtPrice(c.unit)}`);
      } else if (mode === "trend") {
        if (trend.upper) parts.push(`上轨 ${trend.upper.hits}次触碰`);
        if (trend.lower) parts.push(`下轨 ${trend.lower.hits}次触碰`);
        if (trend.wedge) {
          const w = trend.wedge;
          parts.push(`楔形${w.type === "converging" ? "收敛" : "发散"} ${w.gap_change_pct >= 0 ? "+" : ""}${w.gap_change_pct}%`);
        } else {
          parts.push("当前未识别出楔形");
        }
      } else if (mode === "bands" && Array.isArray(bands)) {
        const sup = bands.filter((b) => b.kind === "support").length;
        parts.push(`${bands.length} 条带（压力 ${bands.length - sup} / 支撑 ${sup}）`);
      } else if (mode === "structure") {
        const list = (payload.layers && payload.layers.structure) || [];
        const head = list.slice(-4).map((s) => s.kind).join(" → ");
        parts.push(`${list.length} 个结构点${head ? `（最近：${head}）` : ""}`);
        parts.push("HH/HL = 上行结构，LH/LL = 下行结构");
      } else if (mode === "regression") {
        const reg = payload.layers && payload.layers.regression;
        if (reg) {
          parts.push(`${reg.window} 日拟合 · R² ${reg.r2}`);
          parts.push(`带宽 ±2σ（σ=${this._taFmt(reg.sigma)}）`);
        }
      } else if (mode === "gaps") {
        const list = (payload.layers && payload.layers.gaps) || [];
        const unfilled = list.filter((g) => !g.filled).length;
        parts.push(list.length ? `${list.length} 个跳空（未补 ${unfilled}）` : "当前窗口无跳空");
      } else if (mode === "all") {
        parts.push("八层叠加：回撤 + 通道 + 扇形 + 趋势线 + 支撑阻力带 + 结构 + 回归 + 缺口");
      }
      parts.push("算法生成，非交易信号");

      return {
        type: "text",
        // 贴右边：左边是 Y 轴刻度，写在那会跟"最高/最低价"的轴标签叠字
        right: 12,
        top: 60,
        silent: true,
        // 信号散点的 z 是 30/35/36，说明文字必须压过它们，否则"K/M"字母会盖在字上
        z: 50,
        style: {
          text: `技术面 · ${label}｜${parts.join("｜")}`,
          fill: COLORS.muted,
          fontSize: 10,
          fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
          // 文字压在 1,500 网格线/信号字母上会被"划掉"，垫一层纸色底
          backgroundColor: "rgba(255, 255, 255, 0.92)",
          padding: [2, 5],
          borderRadius: 3,
        },
      };
    }

    // 标注用价格格式：≥10 元取 2 位小数（1290.50），低价 ETF 保留 3 位（0.697）
    _taFmt(value) {
      const n = Number(value);
      if (!Number.isFinite(n)) return "-";
      return Math.abs(n) >= 10 ? n.toFixed(2) : n.toFixed(3);
    }

    // 主图 Y 轴锁定在价格区间。
    // 不改的话，斐波那契平行轨/支撑带这类落在价格区外的线会把刻度拉大：
    // 实测 600519 开「通道」时价格区从满高被压到 1/3，蜡烛挤成一条带。
    // 上下界要取"整刻度"——直接给原始 hi/lo，ECharts 会把轴标签打成 1560.1917999999998。
    _taAxisRange() {
      const k = this.data && this.data.kline;
      if (!k || !k.length) return null;
      let hi = -Infinity, lo = Infinity;
      for (let i = 0; i < k.length; i++) {
        const b = k[i];
        if (b.high != null) hi = Math.max(hi, Number(b.high));
        if (b.low != null) lo = Math.min(lo, Number(b.low));
      }
      if (!(hi > lo)) return null;
      const step = this._niceStep((hi - lo) / 8);
      return {
        min: Number((Math.floor(lo / step) * step).toFixed(4)),
        max: Number((Math.ceil(hi / step) * step).toFixed(4)),
      };
    }

    _niceStep(raw) {
      if (!(raw > 0)) return 1;
      const exp = Math.pow(10, Math.floor(Math.log10(raw)));
      const f = raw / exp;
      const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
      return nice * exp;
    }

    // 当前视窗的价格跨度，用于判断两个标签是否挤在一起（纯启发式，不依赖渲染后坐标）
    _taVisibleSpan() {
      const k = this.data && this.data.kline;
      if (!k || !k.length) return 0;
      let hi = -Infinity, lo = Infinity;
      for (let i = 0; i < k.length; i++) {
        const b = k[i];
        if (b.high != null) hi = Math.max(hi, Number(b.high));
        if (b.low != null) lo = Math.min(lo, Number(b.low));
      }
      return hi > lo ? hi - lo : 0;
    }

    // 坐标轴配置 - 东财风格
    _axisConfig(gridIndex, splitLine, formatter) {
      return {
        scale: true,
        gridIndex,
        splitNumber: gridIndex === 0 ? 4 : 2,
        splitLine: splitLine ? {
          lineStyle: { color: COLORS.grid, type: "dashed" },
        } : { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: COLORS.muted,
          fontSize: 10,
          fontFamily: "'DIN Alternate', 'Tabular Nums', 'Courier New', monospace",
          formatter,
        },
      };
    }

    // 分道轨道左侧行标（M/K/A/B/D）：graphic + 百分比定位（相对整张画布），
    // 容器缩放时自动跟随；纵向按"道中心 - 半字高"手工对中，不依赖文本对齐属性。
    _buildLaneLabels(topPct, heightPct) {
      const rowPct = heightPct / LANE_LABELS.length;
      const chartHeight = (this.chart && this.chart.getHeight()) || 600;
      const halfTextPct = (5 / chartHeight) * 100;   // 8px 字高约 10px，取一半
      return LANE_LABELS.map((label, i) => ({
        type: "text",
        left: "6.6%",
        top: `${(topPct + rowPct * (i + 0.5) - halfTextPct).toFixed(3)}%`,
        silent: true,
        z: 40,
        style: {
          text: label,
          fill: COLORS.indicatorLabel,   // #a9a8a1
          font: "8px ui-monospace, Consolas, monospace",
        },
      }));
    }

    // 均线 - 统一发丝灰，身份交给末端数字（颜色只留给涨跌与信号）
    _buildMASeries(indicators) {
      return [
        ["ma5", "MA5", "5"],
        ["ma10", "MA10", "10"],
        ["ma20", "MA20", "20"],
        ["ma60", "MA60", "60"],
      ].map(([key, name, lab]) => ({
        name,
        type: "line",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: indicators[key] || [],
        smooth: true,
        lineStyle: { width: 0.9, color: COLORS.indicatorMuted },
        // 只影响 tooltip / 图例的色点与行首色，线本身由 lineStyle 决定。
        // 必须用 muted 而非 indicatorMuted：后者在白底上对比度约 1.6:1，tooltip 里会看不清。
        itemStyle: { color: COLORS.muted },
        symbol: "none",
        // 末端数字用 endLabel：逐点 label（data[].label）在 5.5 实测不渲染，
        // endLabel 走 ignoreClip，不受网格右缘裁剪。
        endLabel: {
          show: true,
          distance: 4,
          formatter: lab,
          color: COLORS.indicatorLabel,
          fontSize: 8,
          fontWeight: 400,
          fontFamily: "ui-monospace, Consolas, monospace",
        },
        // 均线收敛时四条末端数字会叠在一起，让它自动上下错开
        labelLayout: { moveOverlap: "shiftY" },
        z: 2,
      }));
    }

    // 构建主图系列（K线/折线/面积）
    _buildMainSeries(kline, ohlc, dates, name, code) {
      const chartType = this.chartType || "candle_up_stroke";

      // 折线图
      if (chartType === "line") {
        const closeData = kline.map((item) => item.close);
        return [{
          name: name || code,
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: closeData,
          smooth: true,
          lineStyle: { width: 1.5, color: COLORS.ma10 },
          symbol: "none",
          areaStyle: null,
        }];
      }

      // 面积图
      if (chartType === "area") {
        const closeData = kline.map((item) => item.close);
        return [{
          name: name || code,
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: closeData,
          smooth: true,
          lineStyle: { width: 1.5, color: COLORS.ma10 },
          symbol: "none",
          areaStyle: {
            color: {
              type: "linear",
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(23, 107, 91, 0.3)" },
                { offset: 1, color: "rgba(23, 107, 91, 0.02)" },
              ],
            },
          },
        }];
      }

      // K线图样式
      let itemStyle = {};
      if (chartType === "candle_solid") {
        // 实心K线：涨红跌绿
        itemStyle = {
          color: COLORS.up,
          color0: COLORS.down,
          borderColor: COLORS.up,
          borderColor0: COLORS.down,
          borderWidth: 1,
        };
      } else if (chartType === "candle_stroke") {
        // 空心K线：涨跌都是空心
        itemStyle = {
          color: "transparent",
          color0: "transparent",
          borderColor: COLORS.up,
          borderColor0: COLORS.down,
          borderWidth: 1.5,
        };
      } else if (chartType === "candle_up_stroke") {
        // 涨空心K线：涨空心，跌实心
        itemStyle = {
          color: "transparent",
          color0: COLORS.down,
          borderColor: COLORS.up,
          borderColor0: COLORS.down,
          borderWidth: 1.5,
        };
      } else if (chartType === "candle_down_stroke") {
        // 跌空心K线：涨实心，跌空心
        itemStyle = {
          color: COLORS.up,
          color0: "transparent",
          borderColor: COLORS.up,
          borderColor0: COLORS.down,
          borderWidth: 1.5,
        };
      }

      return [{
        name: name || code,
        type: "candlestick",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: ohlc,
        itemStyle,
        barWidth: "60%",
        barMaxWidth: 20,
      }];
    }

    // 技术指标副图
    _buildSubIndicatorSeries(indicators) {
      if (this.currentIndicator === "macd" && indicators.macd) {
        return [
          this._lineSeries("DIF", indicators.macd.dif, COLORS.macdDif),
          this._lineSeries("DEA", indicators.macd.dea, COLORS.macdDea),
          {
            name: "MACD",
            type: "bar",
            xAxisIndex: 2,
            yAxisIndex: 2,
            data: indicators.macd.hist,
            itemStyle: {
              color: (params) => params.data >= 0 ? COLORS.macdHistUp : COLORS.macdHistDown,
            },
            barWidth: "60%",
          },
        ];
      }
      if (this.currentIndicator === "rsi") {
        return [
          this._lineSeries("RSI6", indicators.rsi6, COLORS.rsi6),
          this._lineSeries("RSI12", indicators.rsi12, COLORS.rsi12),
          this._lineSeries("RSI24", indicators.rsi24, COLORS.rsi24),
        ];
      }
      if (this.currentIndicator === "kdj" && indicators.kdj) {
        return [
          this._lineSeries("K", indicators.kdj.k, COLORS.kdjK),
          this._lineSeries("D", indicators.kdj.d, COLORS.kdjD),
          this._lineSeries("J", indicators.kdj.j, COLORS.kdjJ, "dotted"),
        ];
      }
      if (this.currentIndicator === "boll" && indicators.boll) {
        const bollPosition = this._calcBollPosition(this.data.kline, indicators.boll);
        return [
          this._mainLineSeries("BOLL上轨", indicators.boll.upper, COLORS.bollUpper, "dashed"),
          this._mainLineSeries("BOLL中轨", indicators.boll.mid, COLORS.bollMid),
          this._mainLineSeries("BOLL下轨", indicators.boll.lower, COLORS.bollLower, "dashed"),
          this._lineSeries("%B", bollPosition, COLORS.ma10),
        ];
      }
      if (this.currentIndicator === "dpo" && indicators.dpo) {
        // DPO 区间震荡线：带 0 轴虚线参考（DPO>0 多头 / <0 空头）
        const dpoSeries = this._lineSeries("DPO", indicators.dpo.dpo, COLORS.dpo);
        dpoSeries.markLine = {
          silent: true,
          symbol: "none",
          lineStyle: { color: COLORS.muted, type: "dashed", width: 1 },
          label: { show: false },
          data: [{ yAxis: 0 }],
        };
        return [
          dpoSeries,
          this._lineSeries("MADPO", indicators.dpo.madpo, COLORS.madpo),
        ];
      }
      return [];
    }

    _calcBollPosition(kline, boll) {
      const position = [];
      for (let i = 0; i < kline.length; i++) {
        const close = kline[i].close;
        const upper = boll.upper[i];
        const lower = boll.lower[i];
        if (upper == null || lower == null || upper === lower) {
          position.push(null);
        } else {
          position.push(((close - lower) / (upper - lower) * 100));
        }
      }
      return position;
    }

    // 副图线系列 - 平滑曲线
    _lineSeries(name, data, color, type) {
      return {
        name,
        type: "line",
        xAxisIndex: 2,
        yAxisIndex: 2,
        data,
        smooth: true,
        lineStyle: { width: 1.1, color, type: type || "solid" },
        itemStyle: { color: COLORS.muted },
        symbol: "none",
        silent: true,
      };
    }

    // 主图线系列（布林带等）
    _mainLineSeries(name, data, color, type) {
      return {
        name,
        type: "line",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data,
        smooth: true,
        lineStyle: { width: 1, color, type: type || "solid" },
        symbol: "none",
        silent: true,
      };
    }

    _getSubTitleData(kline) {
      if (!kline || kline.length === 0) return { text: "", color: COLORS.muted };
      const last = kline[kline.length - 1];
      // 计算涨跌幅（如果 pct_change 为空）
      let pctChange = last.pct_change;
      if (pctChange == null && kline.length >= 2) {
        const prev = kline[kline.length - 2];
        if (prev.close && prev.close > 0) {
          pctChange = ((last.close - prev.close) / prev.close) * 100;
        }
      }
      const changeColor = pctChange >= 0 ? COLORS.up : COLORS.down;
      return {
        price: this._fmtPrice(last.close),
        pctChange: this._fmtPct(pctChange),
        volume: this._formatVolume(last.volume),
        color: changeColor,
      };
    }

    _buildSignalSeries(signals, dates, kline, lanesIdx) {
      if (!signals || signals.length === 0) return [];

      // 分道模式（lanesIdx != null）：仍是这一个 scatter 系列，只换两样东西——
      // value 从"价格"换成"族道中心"，索引从主图换成轨道 grid；
      // 字母 / 红绿 / 透明图标 / 白色光晕两种模式共用，不另建系列。
      const lanes = lanesIdx != null;

      // 只显示最近 60 个交易日的信号，避免过于密集
      const recent = signals.filter(s => {
        const idx = dates.indexOf(s.date);
        return idx >= dates.length - 60;
      });
      if (recent.length === 0) return [];

      // 图标（symbol）与字母（label）必须作为一个“原子单元”整体显示：
      // 不再使用 labelLayout.hideOverlap —— 它会单独隐藏字母而保留图标，
      // 造成“图标和字母不在同一图层、会重叠”的错觉。
      // 改为在数据层做贪心错层：当相邻 K 线（同侧）的标记在价格上过于接近时，
      // 自动向上/下加层，从根上避免任何重叠，字母与图标永远成对出现。
      const STACK_STEP = 0.022;   // 每层间距 = 价格的 2.2%
      const X_NEAR = 1;           // 左右相邻 1 根 K 线内视为可能重叠
      const Y_NEAR = STACK_STEP * 0.9;

      const placed = [];          // 已放置的标记：{ idx, price, above }
      const points = [];

      for (const s of recent) {
        const meta = SIGNAL_META[s.type] || { letter: "?", shape: "circle", good: true };
        const color = meta.good ? COLORS.signalBuy : COLORS.signalSell;
        const idx = dates.indexOf(s.date);
        const item = kline[idx];
        if (!item) continue;

        // 分道：行位由指标族决定，y 取道中心（0.5 / 1.5 / … / 4.5），跳过价格错层——
        // 同一天同一族不会既金叉又死叉，分道后天然零重叠。
        let value;
        if (lanes) {
          value = [s.date, LANE_Y[meta.letter] != null ? LANE_Y[meta.letter] : LANE_Y_FALLBACK];
        } else {
          const above = !meta.good;                 // 坏信号画在 K 线上方，好信号画在下方
          const base = above ? item.high : item.low;

          // 贪心找不与他人重叠的层（上限 30 层，足以覆盖极端密度；每层价格递增，必然能找到空位）
          let price = base;
          for (let level = 1; level <= 30; level++) {
            price = base + (above ? 1 : -1) * base * STACK_STEP * level;
            const collides = placed.some(p =>
              p.above === above &&
              Math.abs(p.idx - idx) <= X_NEAR &&
              Math.abs(p.price - price) <= base * Y_NEAR
            );
            if (!collides) break;
          }

          placed.push({ idx, price, above });
          value = [s.date, price];
        }

        points.push({
          value,
          symbol: SHAPE_SYMBOL[meta.shape] || "circle",
          symbolSize: 20,                       // 尺寸不变，只把图标透明化
          itemStyle: {
            color: "transparent",               // 图标隐形，但命中区还在
            borderWidth: 0,
          },
          label: {
            show: true,
            formatter: meta.letter,
            position: "inside",
            align: "center",
            verticalAlign: "middle",
            color,                              // 红=好 / 绿=坏，颜色回到字母本身
            fontSize: 10,
            fontWeight: "bold",
            textBorderColor: COLORS.labelHalo,  // 白色光晕，压在任何底色上都可读
            textBorderWidth: 2,
          },
          _sig: s,
        });
      }

      return [{
        name: "交易信号",
        type: "scatter",
        xAxisIndex: lanes ? lanesIdx : 0,
        yAxisIndex: lanes ? lanesIdx : 0,
        z: 30,
        data: points,
        tooltip: {
          formatter: (params) => {
            const sig = params.data && params.data._sig;
            if (!sig) return "";
            const meta = SIGNAL_META[sig.type] || { letter: "?", good: true };
            const color = meta.good ? COLORS.signalBuy : COLORS.signalSell;
            return `<div style="font-weight:bold;color:${color}">
              ${esc(sig.label)}<br/>
              <span style="font-size:11px;color:#666">${sig.date}</span>
            </div>`;
          },
        },
      }];
    }

    _formatTooltip(params, kline, signals) {
      if (!params || params.length === 0) return "";
      const idx = params[0].dataIndex;
      const item = kline[idx];
      if (!item) return "";

      // 计算涨跌幅（如果 pct_change 为空）
      let pctChange = item.pct_change;
      if (pctChange == null && idx >= 1) {
        const prev = kline[idx - 1];
        if (prev.close && prev.close > 0) {
          pctChange = ((item.close - prev.close) / prev.close) * 100;
        }
      }

      const changeColor = pctChange >= 0 ? COLORS.up : COLORS.down;
      let html = `<div class="kline-tooltip">
        <div class="kline-tooltip-date">${esc(item.date)}</div>
        <div>开盘: <b style="color:${changeColor}">${this._fmtPrice(item.open)}</b></div>
        <div>收盘: <b style="color:${changeColor}">${this._fmtPrice(item.close)}</b></div>
        <div>最高: <b style="color:${COLORS.up}">${this._fmtPrice(item.high)}</b></div>
        <div>最低: <b style="color:${COLORS.down}">${this._fmtPrice(item.low)}</b></div>
        <div>涨跌: <b style="color:${changeColor}">${this._fmtPct(pctChange)}</b></div>
        <div>成交量: ${this._formatVolume(item.volume)}</div>
        <div>成交额: ${this._formatAmount(item.amount)}</div>`;

      if (item.turnover_rate != null) {
        html += `<div>换手率: ${this._fmtPct(item.turnover_rate)}</div>`;
      }
      params.forEach((param) => {
        // 交易信号是 scatter 系列，其 tooltip 在下方单独列出，避免这里把 [date, price] 数组输出成 "2026-06-17,619.563" 重复行
        // 技术面标注（TA·）是几何测量线，不是当天的数据，悬停时不进明细
        if (param.seriesName && param.seriesName.indexOf("TA·") === 0) return;
        if (param.seriesName && param.value != null && param.seriesType !== "candlestick" && param.seriesType !== "scatter") {
          const value = typeof param.value === "number" ? param.value.toFixed(2) : param.value;
          html += `<div><span style="color:${param.color}">${esc(param.seriesName)}</span>: ${esc(value)}</div>`;
        }
      });

      // 显示当前日期的信号（悬停才显示完整名称，图上只显示字母）
      if (signals && signals.length > 0) {
        const daySignals = signals.filter(s => s.date === item.date);
        if (daySignals.length > 0) {
          html += `<div style="margin-top:4px;padding-top:4px;border-top:1px solid #eee">`;
          daySignals.forEach(sig => {
            const meta = SIGNAL_META[sig.type] || { letter: "?", good: true };
            const color = meta.good ? COLORS.signalBuy : COLORS.signalSell;

            html += `<div style="color:${color};font-weight:bold">${esc(sig.label)}</div>`;
          });
          html += `</div>`;
        }
      }

      // 策略扫描的买点/卖点是 overlay，不在 data.signals 里——
      // 只画在图上、详情不列，就会出现「扫描完成 3 个买点，悬停却查无此项」。
      const ov = this.overlay;
      if (ov && ov.type === "scan" && Array.isArray(ov.signals)) {
        const dayScan = ov.signals.filter(s => s.date === item.date);
        if (dayScan.length > 0) {
          html += `<div style="margin-top:4px;padding-top:4px;border-top:1px solid #eee">`;
          dayScan.forEach(s => {
            if (s.buy) {
              html += `<div style="color:${COLORS.signalBuy};font-weight:bold">▲ 买点 · 策略扫描</div>`;
            }
            if (s.sell) {
              html += `<div style="color:${COLORS.signalSell};font-weight:bold">▼ 卖点 · 策略扫描</div>`;
            }
          });
          html += `</div>`;
        }
      }

      return html + "</div>";
    }

    _fmtPrice(value) {
      if (value == null) return "-";
      const v = Number(value);
      if (!Number.isFinite(v)) return "-";
      // 最多 3 位小数并去掉尾随 0：1446.44 → 1446.44，0.697 → 0.697，3.5 → 3.5
      return v.toFixed(3).replace(/\.?0+$/, "");
    }

    _fmtPct(value) {
      if (value == null || !Number.isFinite(Number(value))) return "-";
      const num = Number(value);
      return `${num > 0 ? "+" : ""}${num.toFixed(2)}%`;
    }

    _formatVolume(value) {
      if (value == null) return "-";
      if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
      if (value >= 10000) return `${(value / 10000).toFixed(2)}万`;
      return String(value);
    }

    _formatAmount(value) {
      if (value == null) return "-";
      if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
      if (value >= 10000) return `${(value / 10000).toFixed(2)}万`;
      return Number(value).toFixed(2);
    }

    // ---- 策略 overlay 系列 ----

    // 分发：scan 信号点 / 回测买卖点
    _buildOverlaySeries(overlay, dates, kline) {
      if (!overlay) return [];
      if (overlay.type === "scan") {
        return this._buildScanSignalSeries(overlay.signals || [], dates, kline);
      }
      if (overlay.type === "backtest") {
        return this._buildBacktestTradeSeries(overlay.trades || [], dates, kline);
      }
      return [];
    }

    // scan 信号：买点红色三角（low 下方），卖点绿色倒三角（high 上方），最新一根放大金边
    // 买点三角下方标注"该买点（按当日收盘价计）到最新收盘价的收益%"
    _buildScanSignalSeries(signals, dates, kline) {
      if (!signals || signals.length === 0) return [];
      const lastIndex = dates.length - 1;
      const latestClose = kline[lastIndex] ? kline[lastIndex].close : null;
      const points = [];

      signals.forEach((s) => {
        const idx = dates.indexOf(s.date);
        if (idx < 0) return;
        const item = kline[idx];
        if (!item) return;
        const isLatest = idx === lastIndex;

        if (s.buy) {
          points.push({
            value: [s.date, item.low],
            symbol: "triangle",
            symbolSize: isLatest ? 16 : 10,
            symbolRotate: 0,
            symbolOffset: [0, "14px"],
            itemStyle: {
              color: COLORS.signalBuy,
              borderColor: isLatest ? "#ffd700" : "#ffffff",
              borderWidth: isLatest ? 2.5 : 1.5,
              shadowBlur: isLatest ? 10 : 4,
              shadowColor: "rgba(231, 76, 60, 0.6)",
            },
            _sig: { ...s, label: isLatest ? "今日买点" : "买点" },
            _kIdx: idx,
            // 买点收益标签（今日买点不显示，避免遮挡最新 K 线）
            label: {
              show: !isLatest && latestClose != null && item.close > 0,
              position: "bottom",
              distance: 5,
              fontSize: 10,
              fontFamily: "'DIN Alternate', 'Tabular Nums', monospace",
              formatter: (p) => {
                const kIdx = p.data && p.data._kIdx;
                const buyClose = kline[kIdx] ? kline[kIdx].close : null;
                if (!buyClose || buyClose <= 0 || latestClose == null) return "";
                const pct = (latestClose / buyClose - 1) * 100;
                const cls = pct >= 0 ? "up" : "down";
                return `{${cls}|${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%}`;
              },
              rich: {
                up: { color: COLORS.up, fontWeight: "bold" },
                down: { color: COLORS.down, fontWeight: "bold" },
              },
            },
          });
        }
        if (s.sell) {
          points.push({
            value: [s.date, item.high],
            symbol: "triangle",
            symbolSize: isLatest ? 16 : 10,
            symbolRotate: 180,
            symbolOffset: [0, "-14px"],
            itemStyle: {
              color: COLORS.signalSell,
              borderColor: isLatest ? "#ffd700" : "#ffffff",
              borderWidth: isLatest ? 2.5 : 1.5,
              shadowBlur: isLatest ? 10 : 4,
              shadowColor: "rgba(23, 107, 91, 0.6)",
            },
            _sig: { ...s, label: isLatest ? "今日卖点" : "卖点" },
          });
        }
      });

      if (points.length === 0) return [];

      return [{
        name: "策略信号",
        type: "scatter",
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 35,
        data: points,
        tooltip: {
          formatter: (params) => {
            const sig = params.data && params.data._sig;
            if (!sig) return "";
            const color = sig.buy ? COLORS.signalBuy : COLORS.signalSell;
            return `<div style="font-weight:bold;color:${color}">${esc(sig.label)}<br/><span style="font-size:11px;color:#666">${sig.date}</span></div>`;
          },
        },
      }];
    }

    // 回测实际成交点：scatter，B/S 实心箭头画成交价（稀疏，状态机驱动）
    _buildBacktestTradeSeries(trades, dates, kline) {
      if (!trades || trades.length === 0) return [];
      const points = trades.map((t) => {
        const isBuy = t.direction === "buy";
        return {
          value: [t.date, t.price],
          symbol: "arrow",
          symbolSize: 14,
          symbolRotate: isBuy ? 0 : 180,
          itemStyle: { color: isBuy ? COLORS.signalBuy : COLORS.signalSell },
          label: {
            show: true,
            position: isBuy ? "bottom" : "top",
            fontSize: 10,
            fontWeight: "bold",
            color: "#fff",
            formatter: isBuy ? "B" : "S",
          },
          _trade: t,
        };
      });

      const latestClose = kline.length > 0 ? kline[kline.length - 1].close : null;
      return [{
        name: "回测交易",
        type: "scatter",
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 36,
        data: points,
        tooltip: {
          formatter: (params) => {
            const t = params.data && params.data._trade;
            if (!t) return "";
            const isBuy = t.direction === "buy";
            const color = isBuy ? COLORS.signalBuy : COLORS.signalSell;
            const pnlText = (t.pnl != null)
              ? `<br/><span style="color:${t.pnl >= 0 ? COLORS.signalBuy : COLORS.signalSell}">盈亏: ${t.pnl >= 0 ? "+" : ""}${Number(t.pnl).toFixed(2)} (${Number(t.pnl_pct || 0).toFixed(2)}%)</span>`
              : "";
            // 买入点追加"该买入价到最新收盘的收益%"（按收盘价计，未计费用）
            let nowPctText = "";
            if (isBuy && latestClose != null && t.price > 0) {
              const nowPct = (latestClose / t.price - 1) * 100;
              const nc = nowPct >= 0 ? COLORS.signalBuy : COLORS.signalSell;
              nowPctText = `<br/><span style="color:${nc}">至今: ${nowPct >= 0 ? "+" : ""}${nowPct.toFixed(2)}%（按收盘价）</span>`;
            }
            return `<div style="font-weight:bold;color:${color}">${isBuy ? "买入" : "卖出"} ${esc(String(t.price))}<br/><span style="font-size:11px;color:#666">${t.date} · ${t.quantity || ""}股</span>${pnlText}${nowPctText}</div>`;
          },
        },
      }];
    }

    // 持仓成本线：主图横向虚线（紫色 #8b5cf6，避开 A 股红涨绿跌语义），
    // label "成本 xx.xx"，symbol:'none' 不画端点，不画买卖点（流水不全，标了误导）
    _buildCostLineSeries(cost) {
      return {
        name: "成本线",
        type: "line",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: [],
        markLine: {
          silent: true,
          symbol: "none",
          data: [{ yAxis: cost }],
          lineStyle: { color: "#8b5cf6", type: "dashed", width: 1.2 },
          label: {
            formatter: `成本 ${Number(cost).toFixed(2)}`,
            color: "#8b5cf6",
            fontSize: 10,
            position: "insideEndTop",
          },
        },
      };
    }

    // 回测净值曲线（第 4 栏），含初始资金基准线
    _buildEquitySeries(equityCurve, dates, initialCash) {
      if (!equityCurve || equityCurve.length === 0) return [];
      const equityMap = {};
      equityCurve.forEach((e) => { equityMap[e.date] = e.equity; });
      const data = dates.map((d) => (equityMap[d] != null ? equityMap[d] : null));
      const base = initialCash || (equityCurve[0] ? equityCurve[0].equity : 1000000);

      return [{
        name: "净值",
        type: "line",
        xAxisIndex: 3,
        yAxisIndex: 3,
        data,
        smooth: true,
        symbol: "none",
        lineStyle: { width: 1.5, color: COLORS.ma10 },
        areaStyle: {
          color: {
            type: "linear",
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(23, 107, 91, 0.25)" },
              { offset: 1, color: "rgba(23, 107, 91, 0.02)" },
            ],
          },
        },
        markLine: {
          silent: true,
          symbol: "none",
          data: [{ yAxis: base, name: "初始资金" }],
          lineStyle: { color: COLORS.light, type: "dashed", width: 1 },
          label: { formatter: "初始", color: COLORS.muted, fontSize: 10, position: "insideEndTop" },
        },
      }];
    }
  }

  // 暴露 KlineChart 绘图类：报告「技术面图板」(tech_panel) 复用同一套
  // K线+成交量+副图指标渲染与信号标注能力（chat-core.js 中使用）
  window.StockRadar.KlineChart = KlineChart;

  window.StockRadar.KlinePanel = class KlinePanel {
    constructor() {
      this.overlayEl = document.getElementById("klineOverlay");
      this.modalEl = document.getElementById("klineModal");
      this.searchInput = document.getElementById("klineSearchInput");
      this.searchResults = document.getElementById("klineSearchResults");
      this.statusEl = document.getElementById("klineStatus");
      this.syncBtn = document.getElementById("klineSyncBtn");
      this.autoSyncToggle = document.getElementById("klineAutoSyncToggle");
      this.sourceDropdown = document.getElementById("klineSourceDropdown");
      this.sourceTrigger = document.getElementById("klineSourceTrigger");
      this.sourceLabel = document.getElementById("klineSourceLabel");
      // 只取带 data-days 的预设按钮：「缩短/拉长」复用了同款外观类（CSS 冻结），
      // 但没有 data-days——带上它们会让 active 判定出现「点 120 柱两个按钮一起亮」。
      this.daysButtons = Array.from(document.querySelectorAll(".kline-days-btn[data-days]"));
      this.daysHintEl = document.getElementById("klineDaysHint");
      this.shrinkBtn = document.getElementById("klineShrinkBtn");
      this.stretchBtn = document.getElementById("klineStretchBtn");
      this.rangeStartInput = document.getElementById("klineRangeStart");
      this.rangeEndInput = document.getElementById("klineRangeEnd");
      this.rangeApplyBtn = document.getElementById("klineRangeApply");
      this.rangeClearBtn = document.getElementById("klineRangeClear");
      // 只取副图那五个指标按钮：信号布局按钮复用了同款外观类（CSS 冻结），
      // 必须在这里排除，否则点「贴图/分道」会被当成切换副图指标。
      this.indicatorButtons = Array.from(
        document.querySelectorAll(".kline-indicator-group .kline-indicator-btn:not(.kline-signal-layout-btn)")
      );
      this.chart = new KlineChart(document.getElementById("klineChartContainer"));

      // 技术面图层：下拉控件 + 一份按 code+days 缓存的几何结果（off 时不发请求）
      this.taSelect = document.getElementById("klineTaSelect");
      this.taMode = "off";
      this.taCache = null;

      // 导出图片：直接从 canvas 取图，白底 2 倍，文件名带标的与区间
      const exportBtn = document.getElementById("klineExportBtn");
      if (exportBtn) {
        exportBtn.onclick = () => {
          try {
            const inst = this.chart && this.chart.chart;
            if (!inst) return;
            const url = inst.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
            const code = (this.stockCodeEl && this.stockCodeEl.textContent || "").trim();
            const label = (code && code !== "-") ? code : "K线";
            const a = document.createElement("a");
            a.href = url;
            a.download = `${label.replace(/[\\\\/:*?"<>|\s]+/g, "_")}_K线.png`;
            document.body.appendChild(a);
            a.click();
            a.remove();
          } catch (e) {
            console.error("K线导出失败:", e);
          }
        };
      }

      // 策略选择 + 扫描/回测控件
      this.strategySelect = document.getElementById("klineStrategySelect");
      this.scanBtn = document.getElementById("klineScanBtn");
      this.backtestBtn = document.getElementById("klineBacktestBtn");
      this.scanBadge = document.getElementById("klineScanBadge");
      this.strategiesLoaded = false;
      this.currentStrategy = localStorage.getItem("kline_strategy") || "";

      // 股票信息栏元素
      this.stockNameEl = document.getElementById("klineStockName");
      this.stockCodeEl = document.getElementById("klineStockCode");
      this.priceEl = document.getElementById("klinePrice");
      this.changeEl = document.getElementById("klineChange");
      this.changePctEl = document.getElementById("klineChangePct");
      this.volumeEl = document.getElementById("klineVolume");
      this.amountEl = document.getElementById("klineAmount");
      this.turnoverEl = document.getElementById("klineTurnover");

      this.currentCode = "";
      this.dataSources = [];
      this.searchTimer = null;

      // 从 localStorage 读取配置
      this.autoSync = localStorage.getItem("kline_auto_sync") === "true";
      this.currentDays = parseInt(localStorage.getItem("kline_default_days") || "120");
      this.selectedSource = localStorage.getItem("kline_source") || "auto";

      // 自定义区间 {start_date, end_date}：给了日期就以日期为准，后端会忽略 days，
      // 但 currentDays 仍保留——信号/技术面两个接口还只认天数，用它做近似窗口。
      this.range = null;
      // 「清除」要还原到应用区间之前的柱数：临时缩放不写 localStorage，
      // 所以这里必须自己记住，不能指望预设按钮就是上一个状态。
      this._daysBeforeRange = null;

      // 初始化 UI 状态
      if (this.autoSyncToggle) {
        this.autoSyncToggle.checked = this.autoSync;
      }
      this._updateDaysButtons();

      this._bindEvents();
      this._loadDataSources();
      this._loadStrategies();
    }

    open(code, opts = {}) {
      this.overlayEl.classList.add("open");
      this.modalEl.classList.add("open");
      this.chart.resize();
      // AI 信号评测跳转：markRange = {from: 'YYYY-MM-DD', to: '+5D'|null, buy: bool}
      this._pendingMarkRange = opts.markRange || null;
      // 持仓成本线（持仓行「看K线」openKlineChart(code, {costLine: avg_cost})）
      this._pendingCostLine = opts.costLine != null ? Number(opts.costLine) : null;
      if (code) {
        this.searchInput.value = code;
        this.load(code);
      } else {
        this.searchInput.focus();
      }
    }

    close() {
      this.overlayEl.classList.remove("open");
      this.modalEl.classList.remove("open");
      this._hideSearchResults();
      this._closeSourceDropdown();
    }

    _bindEvents() {
      document.getElementById("klineCloseBtn").onclick = () => this.close();
      // 资金面条：默认折叠，点标题展开
      const fundsToggle = document.getElementById("klineFundsToggle");
      if (fundsToggle) {
        fundsToggle.addEventListener("click", () => {
          const body = document.getElementById("klineFundsBody");
          const section = document.getElementById("klineFundsSection");
          if (!body) return;
          const willOpen = body.hidden;
          body.hidden = !willOpen;
          fundsToggle.setAttribute("aria-expanded", String(willOpen));
          if (section) section.classList.toggle("open", willOpen);
        });
      }
      this.overlayEl.onclick = () => this.close();
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && this.modalEl.classList.contains("open")) this.close();
      });

      // 自动同步开关
      if (this.autoSyncToggle) {
        this.autoSyncToggle.addEventListener("change", () => {
          this.autoSync = this.autoSyncToggle.checked;
          localStorage.setItem("kline_auto_sync", String(this.autoSync));
        });
      }

      this.searchInput.addEventListener("input", () => this._queueSearch());
      this.searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          const code = this._normalizeCode(this.searchInput.value.trim());
          if (code) this.load(code);
        }
      });
      this.searchResults.addEventListener("click", (event) => {
        const item = event.target.closest(".kline-search-item");
        if (!item || !item.dataset.code) return;
        this.searchInput.value = item.dataset.code;
        this._hideSearchResults();
        this.load(item.dataset.code, item.dataset.market);
      });
      document.addEventListener("click", (event) => {
        if (!event.target.closest(".kline-search-box")) this._hideSearchResults();
      });
      this.sourceTrigger.addEventListener("click", () => this._toggleSourceDropdown());
      this.sourceTrigger.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          this._toggleSourceDropdown();
        }
      });
      document.addEventListener("click", (event) => {
        if (!event.target.closest(".kline-source-selector")) this._closeSourceDropdown();
      });

      this.daysButtons.forEach((button) => {
        button.addEventListener("click", () => {
          // 预设按钮是「天数模式」的入口：点它就等于离开自定义区间，
          // 否则会出现「按钮亮着、图上却是别的日期段」的错位。
          this.currentDays = Number(button.dataset.days || 120);
          this.range = null;
          this._daysBeforeRange = null;
          localStorage.setItem("kline_default_days", String(this.currentDays));
          this._updateDaysButtons();
          if (this.currentCode) this.load(this.currentCode);
        });
      });

      // 连续缩放（东财式拉长/缩短）：只改当前视图，不写 localStorage ——
      // 临时看一眼更长的历史，不该把用户的默认区间改掉。
      if (this.shrinkBtn) this.shrinkBtn.addEventListener("click", () => this._zoomDays(0.7));
      if (this.stretchBtn) this.stretchBtn.addEventListener("click", () => this._zoomDays(1.4));

      // 自定义区间：只有「应用」才真正重取数。日期框的 input 事件不绑定，
      // 否则用户每敲一段就在后台发一次请求（本地日线再快也扛不住这么点）。
      if (this.rangeApplyBtn) this.rangeApplyBtn.addEventListener("click", () => this._applyRange());
      if (this.rangeClearBtn) this.rangeClearBtn.addEventListener("click", () => this._clearRange());
      // 日期框里按回车 = 应用，省一次鼠标挪动
      [this.rangeStartInput, this.rangeEndInput].forEach((input) => {
        if (!input) return;
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            this._applyRange();
          }
        });
      });

      // K线样式切换
      this.chartTypeButtons = Array.from(document.querySelectorAll(".kline-chart-type-btn"));
      this.chartType = localStorage.getItem("kline_chart_type") || "candle_up_stroke";
      this._updateChartTypeButtons();
      // 把已存/默认画法真正喂给图表实例。历史上这里只设了按钮高亮，
      // KlineChart 自己的 chartType 一直是 undefined，于是图永远走兜底值，
      // localStorage 里的选择从未生效（默认值改成空心涨后这个错位才显形）。
      this.chart.setChartType(this.chartType);
      this.chartTypeButtons.forEach((button) => {
        button.addEventListener("click", () => {
          this.chartTypeButtons.forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          this.chartType = button.dataset.type;
          localStorage.setItem("kline_chart_type", this.chartType);
          this.chart.setChartType(this.chartType);
        });
      });

      // 信号布局切换（贴图 / 分道）。只重绘，不重新取数。
      this.signalLayoutButtons = Array.from(document.querySelectorAll(".kline-signal-layout-btn"));
      this.signalLayout = readSignalLayout();
      this._updateSignalLayoutButtons();
      this.chart.setSignalLayout(this.signalLayout);
      this.signalLayoutButtons.forEach((button) => {
        button.addEventListener("click", () => {
          this.signalLayoutButtons.forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          this.signalLayout = normalizeSignalLayout(button.dataset.layout);
          localStorage.setItem(SIGNAL_LAYOUT_KEY, this.signalLayout);
          this.chart.setSignalLayout(this.signalLayout);
        });
      });

      this.indicatorButtons.forEach((button) => {
        button.addEventListener("click", () => {
          this.indicatorButtons.forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          this.chart.setSubIndicator(button.dataset.indicator);
        });
      });

      // 技术面图层（默认关）：切换只重绘；第一次开启才去后端算
      this.taMode = normalizeTaMode(localStorage.getItem(TA_KEY));
      if (this.taSelect) {
        this.taSelect.value = this.taMode;
        this.taSelect.addEventListener("change", () => {
          this.taMode = normalizeTaMode(this.taSelect.value);
          localStorage.setItem(TA_KEY, this.taMode);
          this.chart.setTaMode(this.taMode);
          if (this.taMode !== "off") this._loadTa();
        });
      }
      this.chart.setTaMode(this.taMode);
      this.syncBtn.onclick = () => this.syncCurrent();

      // 策略选择 + 扫描买点 + 跑回测
      if (this.strategySelect) {
        this.strategySelect.addEventListener("change", () => {
          this.currentStrategy = this.strategySelect.value;
          localStorage.setItem("kline_strategy", this.currentStrategy);
          this._updateStrategyButtons();
        });
      }
      if (this.scanBtn) this.scanBtn.onclick = () => this._scanSignals();
      if (this.backtestBtn) this.backtestBtn.onclick = () => this._runBacktest();
    }

    // 预设按钮态。有 range 时预设**全不** active：
    // 自定义区间跟预设不是一回事，亮着任何一档都是在谎报当前窗口。
    _updateDaysButtons() {
      const inRange = !!this.range;
      this.daysButtons.forEach((button) => {
        const days = Number(button.dataset.days || 120);
        button.classList.toggle("active", !inRange && days === this.currentDays);
      });
      this._renderDaysHint();
      // 柱数写进 title：工具栏已经排满，再放一个数字标签会把后面那组挤掉行
      if (this.shrinkBtn) this.shrinkBtn.title = `缩短区间：当前 ${this.currentDays} 柱 ×0.7`;
      if (this.stretchBtn) this.stretchBtn.title = `拉长区间：当前 ${this.currentDays} 柱 ×1.4`;
    }

    // 柱数提示。有实际返回条数时用真值：range 模式下 currentDays 只是按日历折算的
    // 近似窗口（还得剔除停牌/节假日），拿它当柱数显示会跟图上的 bar 数量对不上。
    _renderDaysHint(actualBars) {
      if (!this.daysHintEl) return;
      const bars = actualBars || this.currentDays;
      const inRange = !!this.range;
      this.daysHintEl.textContent = inRange ? `区间 ${bars}柱` : `${bars}柱`;
      this.daysHintEl.classList.toggle("range", inRange);
      this.daysHintEl.title = inRange
        ? `${this.range.start_date} ~ ${this.range.end_date}，实际 ${bars} 柱`
        : `当前 ${bars} 柱`;
    }

    // 连续缩放。clamp 到 [20, 1000]：后端 days 上限就是 1000，少于 20 根时
    // 均线和 MACD 全是空窗，看着像「没数据」。
    _zoomDays(factor) {
      this.currentDays = Math.max(20, Math.min(1000, Math.round(this.currentDays * factor)));
      // 缩放是在天数轴上做的，自定义区间必须一起清掉，否则改了柱数图没反应
      this.range = null;
      this._daysBeforeRange = null;
      this._updateDaysButtons();
      if (this.currentCode) this.load(this.currentCode);
    }

    // 应用自定义起止日期
    _applyRange() {
      const start = ((this.rangeStartInput && this.rangeStartInput.value) || "").trim();
      const end = ((this.rangeEndInput && this.rangeEndInput.value) || "").trim();
      if (!start || !end) {
        this._setStatus("error", "起止日期都要选，单边日期交不出区间");
        return;
      }
      const startMs = Date.parse(`${start}T00:00:00`);
      const endMs = Date.parse(`${end}T00:00:00`);
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) {
        this._setStatus("error", "日期格式不对，需要 YYYY-MM-DD");
        return;
      }
      if (startMs > endMs) {
        this._setStatus("error", "起始日期不能晚于结束日期");
        return;
      }
      const spanDays = Math.round((endMs - startMs) / 86400000) + 1;
      if (spanDays > 3660) {
        // 本地日线也就十来年，再长只会把一次请求拖成十几秒
        this._setStatus("error", "区间跨度不能超过 10 年");
        return;
      }
      // 只在第一次进区间模式时记：连续换两段日期后「清除」应该回到当初的柱数
      if (!this.range) this._daysBeforeRange = this.currentDays;
      this.range = { start_date: start, end_date: end };
      // days 仍需给一个近似值：信号/技术面接口还没吃 range（TA 后端后续才支持），
      // 按 A 股一年约 245 个交易日折算出窗口，别让信号点只算到区间的一半。
      this.currentDays = Math.max(20, Math.min(1000, Math.round((spanDays * 245) / 365)));
      this._updateDaysButtons();
      this._setStatus("loading", `按 ${start} ~ ${end} 取数（约 ${this.currentDays} 个交易日）`);
      if (this.currentCode) this.load(this.currentCode);
    }

    // 清除自定义区间：回到应用前的柱数（没经历过应用就保持现状），
    // 日期框一起清空，否则界面上留着日期却按天数渲染，用户没法判断到底哪个生效。
    _clearRange() {
      const hadRange = !!this.range;
      this.range = null;
      if (this._daysBeforeRange != null) {
        this.currentDays = this._daysBeforeRange;
        this._daysBeforeRange = null;
      }
      if (this.rangeStartInput) this.rangeStartInput.value = "";
      if (this.rangeEndInput) this.rangeEndInput.value = "";
      this._updateDaysButtons();
      // 本来就没区间时只是清输入框，别再白跑一次取数
      if (hadRange && this.currentCode) this.load(this.currentCode);
    }

    _updateChartTypeButtons() {
      this.chartTypeButtons.forEach((button) => {
        if (button.dataset.type === this.chartType) {
          button.classList.add("active");
        } else {
          button.classList.remove("active");
        }
      });
    }

    // 按钮的初始态以 localStorage 里的真实值为准（HTML 里的 active 只是兜底）
    _updateSignalLayoutButtons() {
      this.signalLayoutButtons.forEach((button) => {
        if (button.dataset.layout === this.signalLayout) {
          button.classList.add("active");
        } else {
          button.classList.remove("active");
        }
      });
    }

    // ── 资金面（北向持股 + 龙虎榜席位） ──────────────────────────────
    async _loadFunds(code) {
      const body = document.getElementById("klineFundsBody");
      const summary = document.getElementById("klineFundsSummary");
      if (!body || !summary) return;
      this._fundsSeq = (this._fundsSeq || 0) + 1;
      const seq = this._fundsSeq;
      body.innerHTML = '<div class="kline-funds-empty">资金面加载中…</div>';
      try {
        const resp = await fetch(`/api/stock/funds?code=${encodeURIComponent(code)}`);
        const payload = await resp.json();
        if (seq !== this._fundsSeq) return;
        this._renderFunds((payload && payload.data) || {}, body, summary);
      } catch (err) {
        if (seq === this._fundsSeq) {
          body.innerHTML = '<div class="kline-funds-empty">资金面加载失败</div>';
          summary.textContent = "";
        }
      }
    }

    _fmtYi(value) {
      if (value == null || Number.isNaN(Number(value))) return "—";
      const n = Number(value);
      if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + "亿";
      if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + "万";
      return n.toFixed(0);
    }

    _renderFunds(data, body, summary) {
      const mg = data.margin || {};
      const lhb = data.lhb || {};
      const m = mg.metrics || {};
      const parts = [];
      if (mg.available) {
        parts.push(`融资 ${this._fmtYi(m.fin_balance)}`);
        if (m.change_5d_pct != null) {
          parts.push(`5日 ${m.change_5d_pct >= 0 ? "+" : ""}${m.change_5d_pct}%`);
        }
        if (mg.risk === "high") parts.push("融资高位");
      } else {
        parts.push("无两融数据");
      }
      parts.push(lhb.available ? `龙虎榜 ${String(lhb.date || "").slice(5)}` : "近半年未上榜");
      summary.textContent = parts.join(" · ");

      let marginHtml = '<div class="kline-funds-empty">暂无融资融券数据（先跑 sync_margin_data.py）</div>';
      if (mg.available) {
        const riskCls = mg.risk === "high" ? "high" : mg.risk === "low" ? "low" : "mid";
        const riskLabel = { high: "高位", mid: "中位", low: "低位" }[mg.risk] || "—";
        const fmtPct = (v) => (v != null ? (v >= 0 ? "+" : "") + v + "%" : "—");
        const cls = (v) => (v != null && v < 0 ? "down" : "up");
        marginHtml = `<div class="kline-funds-kv">
          <span>融资余额 <b>${this._fmtYi(m.fin_balance)}</b></span>
          <span>环比 <b class="${cls(m.change_pct)}">${fmtPct(m.change_pct)}</b></span>
          <span>5日 <b class="${cls(m.change_5d_pct)}">${fmtPct(m.change_5d_pct)}</b></span>
          <span>20日 <b class="${cls(m.change_20d_pct)}">${fmtPct(m.change_20d_pct)}</b></span>
          <span class="kline-funds-risk ${riskCls}">${riskLabel}${m.percentile != null ? " " + (m.percentile * 100).toFixed(0) + "%" : ""}</span>
        </div>
        ${mg.risk_note ? `<div class="kline-funds-note">${mg.risk_note}</div>` : ""}
        ${this._renderFundSpark(mg.series || [])}`;
      }

      let lhbHtml = '<div class="kline-funds-empty">近半年未上龙虎榜</div>';
      if (lhb.available) {
        const rows = (lhb.seats || []).slice(0, 6).map((s) => {
          const seat = String(s.seat || "");
          const short = seat.length > 18 ? seat.slice(0, 18) + "…" : seat;
          return `<tr><td>${short}</td>
            <td>${s.buy != null ? this._fmtYi(s.buy) : "—"}</td>
            <td>${s.sell != null ? this._fmtYi(s.sell) : "—"}</td>
            <td>${s.net != null ? this._fmtYi(s.net) : "—"}</td></tr>`;
        }).join("");
        lhbHtml = `<table class="kline-funds-table">
          <thead><tr><th>席位（${lhb.date}）</th><th>买入</th><th>卖出</th><th>净额</th></tr></thead>
          <tbody>${rows}</tbody></table>`;
      }

      body.innerHTML = `
        <div class="kline-funds-block">
          <div class="kline-funds-block-title">融资融券</div>${marginHtml}
        </div>
        <div class="kline-funds-block">
          <div class="kline-funds-block-title">龙虎榜席位</div>${lhbHtml}
        </div>`;
    }

    _renderFundSpark(series) {
      const points = (series || []).map((x) => x.fin_balance).filter((v) => v != null);
      if (points.length < 2) return "";
      const w = 220;
      const h = 34;
      const min = Math.min(...points);
      const max = Math.max(...points);
      const span = max - min || 1;
      const step = w / (points.length - 1);
      const path = points.map((v, i) =>
        `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(h - ((v - min) / span) * (h - 4) - 2).toFixed(1)}`
      ).join(" ");
      return `<svg class="kline-funds-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${path}" fill="none" stroke="#176b5b" stroke-width="1.5"/></svg>`;
    }

    async _loadDataSources() {
      try {
        const data = await window.StockRadar.api.getDataSources();
        this.dataSources = data.sources || [];
      } catch {
        this.dataSources = [];
      }
      this._renderSourceDropdown();
    }

    _renderSourceDropdown() {
      this.sourceDropdown.innerHTML = "";
      this.sourceDropdown.appendChild(this._createSourceOption("auto", "自动", "按优先级自动选择"));
      for (const ds of this.dataSources) {
        if (!ds.available) continue;
        this.sourceDropdown.appendChild(this._createSourceOption(ds.name, ds.label || ds.name, ds.description || ""));
      }
    }

    _createSourceOption(value, label, desc) {
      const opt = document.createElement("div");
      opt.className = "kline-source-option" + (value === this.selectedSource ? " active" : "");
      opt.innerHTML = `
        <div class="kline-source-option-main">
          <div class="kline-source-option-name">${esc(label)}</div>
          ${desc ? `<div class="kline-source-option-desc">${esc(desc)}</div>` : ""}
        </div>
        <svg class="kline-source-option-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
      opt.addEventListener("click", () => {
        this.selectedSource = value;
        localStorage.setItem("kline_source", value);
        this.sourceLabel.textContent = label;
        this._closeSourceDropdown();
        this._renderSourceDropdown();
      });
      return opt;
    }

    _toggleSourceDropdown() {
      this.sourceDropdown.classList.contains("open") ? this._closeSourceDropdown() : this._openSourceDropdown();
    }

    _openSourceDropdown() {
      this.sourceDropdown.classList.add("open");
      this.sourceTrigger.setAttribute("aria-expanded", "true");
    }

    _closeSourceDropdown() {
      this.sourceDropdown.classList.remove("open");
      this.sourceTrigger.setAttribute("aria-expanded", "false");
    }

    async load(code, market) {
      this.currentCode = code.trim();
      this.currentMarket = market || null;
      if (!this.currentCode) return;
      this._loadFunds(this.currentCode);
      this.searchInput.value = this.currentCode;
      this._hideSearchResults();
      this._setStatus("loading", `正在读取 ${this.currentCode} 本地 K 线...`);
      this.syncBtn.disabled = true;
      this.chart.clearOverlay();
      // 换股时先清掉上一只的标注，避免旧线挂在新蜡烛上
      if (this.taMode !== "off") this.chart.setTaData(null);
      // 持仓成本线：open() 传入的 pending 值在此消费（换股/重查后失效）
      if (this._pendingCostLine !== undefined) {
        this.chart.setCostLine(this._pendingCostLine);
        this._pendingCostLine = undefined;
      }
      this._hideScanBadge();
      await this.chart.showLoading("读取本地 K 线数据...");
      try {
        // 并行获取 K 线数据和信号数据。range 交给 K 线接口（有 start_date 时它忽略 days），
        // 信号接口还只认天数——用 currentDays 这个近似窗口兜底，别让它退化成默认 120。
        const [data, signalsData] = await Promise.all([
          window.StockRadar.api.getKline(this.currentCode, this.currentDays, this.currentMarket, this.range),
          window.StockRadar.api.getSignals(this.currentCode, this.currentDays).catch(() => ({ signals: [] })),
        ]);
        data.signals = signalsData.signals || [];
        // AI 信号评测 markRange → 注入信号点（复用 SIGNAL_META 绘制）
        if (this._pendingMarkRange && this._pendingMarkRange.from) {
          data.signals.push({
            type: this._pendingMarkRange.buy === false ? "sell_mark" : "buy_mark",
            date: this._pendingMarkRange.from,
            note: this._pendingMarkRange.to ? `AI 信号 → ${this._pendingMarkRange.to}` : "AI 信号",
          });
          this._pendingMarkRange = null;
        }
        await this.chart.setData(data);
        this._setLoaded(data);
        if (this.taMode !== "off") await this._loadTa();
      } catch (error) {
        // 如果开启自动同步且是无数据错误，自动同步
        if (this.autoSync && error.detail && error.detail.reason === "no_local_data") {
          await this.syncCurrent();
          return;
        }
        this._handleLoadError(error);
      }
    }

    // 技术面标注：按 code+days+区间缓存一份几何结果，同一只票来回切层不重复请求。
    // 失败时清空而不是画半截线——宁可没有，也不能给出错位的测量线。
    async _loadTa() {
      const code = this.currentCode;
      if (!code) return;
      const range = this.range;
      // 路由对 days 有 ge=60 的硬约束：缩短到 20 柱后直接传会被 422 打回，
      // 而 TA 目前还只认天数 → 先夹到合法区间，缓存 key 也跟着用夹过的值。
      const taDays = Math.max(60, Math.min(1000, this.currentDays || 250));
      // range 必须进 key：只按 days 缓存的话，换一段日期（近似柱数撞上同一个值）会把
      // 上一段的标注原样贴到新蜡烛上，看起来像算法算错了。
      const rangeKey = range ? `${range.start_date}~${range.end_date}` : "";
      const key = `${code}:${taDays}:${this.currentMarket || ""}:${rangeKey}`;
      if (this.taCache && this.taCache.key === key) {
        this.chart.setTaData(this.taCache.data);
        return;
      }
      try {
        const data = await window.StockRadar.api.getTaAnnotations(
          code, taDays, this.currentMarket, range);
        if (this.currentCode !== code) return;   // 期间换股了，丢弃这次结果
        this.taCache = { key, data };
        this.chart.setTaData(data);
      } catch (error) {
        this.chart.setTaData(null);
        this._setStatus("error", `技术面标注读取失败：${error.message || "接口异常"}`);
      }
    }

    async syncCurrent() {
      const code = (this.currentCode || this.searchInput.value).trim();
      if (!code) return;
      this.currentCode = code;
      this.searchInput.value = code;
      const source = this.selectedSource || "auto";
      const syncDays = Math.max(this.currentDays, 250);
      this.syncBtn.disabled = true;
      this.syncBtn.classList.add("syncing");
      this._setStatus("loading", `正在同步 ${code} 最近 ${Math.max(this.currentDays, 250)} 天 K 线...`);
      await this.chart.showLoading("同步数据中，完成后自动刷新图表...");
      try {
        const result = await window.StockRadar.api.syncKline(code, syncDays, source);
        this._setStatus("ok", `已同步 ${result.count || 0} 条记录，正在刷新图表...`);
        await this.load(code);
      } catch (error) {
        this._setStatus("error", error.message || "同步失败");
        await this.chart.showError("同步失败，请稍后重试");
      } finally {
        this.syncBtn.classList.remove("syncing");
        this.syncBtn.disabled = false;
      }
    }

    _normalizeCode(raw) {
      if (!raw) return raw;
      // 去掉前导市场前缀：sh / sz / bj（不区分大小写），可带 . 或 : 分隔
      // 如 sh001309 / SZ.300750 / bj:830799 -> 001309 / 300750 / 830799
      return raw.replace(/^(sh|sz|bj)[\.\:]?/i, "").trim();
    }

    async _queueSearch() {
      clearTimeout(this.searchTimer);
      const keyword = this.searchInput.value.trim();
      if (!keyword) {
        this._hideSearchResults();
        return;
      }
      this.searchTimer = setTimeout(() => this._search(keyword), 240);
    }

    async _search(keyword) {
      try {
        const data = await window.StockRadar.api.searchChartStock(this._normalizeCode(keyword));
        const results = data.results || [];
        if (!results.length) {
          this.searchResults.innerHTML = '<div class="kline-search-empty">未找到匹配股票，可直接按回车按代码加载</div>';
        } else {
          this.searchResults.innerHTML = results.map((item) => `
            <div class="kline-search-item" data-code="${esc(item.code)}" data-market="${esc(item.market || '')}">
              <div>
                <span class="kline-search-code">${esc(item.code)}</span>
                <span class="kline-search-name">${esc(item.name || "-")}</span>
              </div>
              <span class="kline-search-market">${esc(item.market || "-")}</span>
            </div>`).join("");
        }
        this.searchResults.classList.add("open");
      } catch {
        this.searchResults.innerHTML = '<div class="kline-search-empty">搜索失败，可直接按回车按代码加载</div>';
        this.searchResults.classList.add("open");
      }
    }

    _hideSearchResults() {
      this.searchResults.classList.remove("open");
    }

    _setLoaded(data) {
      const last = data.kline[data.kline.length - 1];
      const prev = data.kline.length >= 2 ? data.kline[data.kline.length - 2] : null;

      // 更新股票信息栏
      this._updateStockBar(data, last, prev);

      this.syncBtn.disabled = false;
      this._updateStrategyButtons();
      // 柱数提示以实际返回为准（range 模式下 currentDays 只是近似窗口）
      this._renderDaysHint(data.kline.length);
      this._setStatus("ok", `本地 ${data.kline.length} 条，最新 ${last ? last.date : "-"}`);
    }

    _updateStockBar(data, last, prev) {
      if (!last) return;

      // 股票名称和代码
      if (this.stockNameEl) this.stockNameEl.textContent = data.name || data.code;
      if (this.stockCodeEl) this.stockCodeEl.textContent = data.code;

      // 价格
      if (this.priceEl) {
        this.priceEl.textContent = this._fmtPrice(last.close);
      }

      // 计算涨跌幅
      let pctChange = last.pct_change;
      let changeAmount = last.change_amount;
      if (pctChange == null && prev && prev.close > 0) {
        pctChange = ((last.close - prev.close) / prev.close) * 100;
        changeAmount = last.close - prev.close;
      }

      const isUp = pctChange >= 0;
      const changeClass = isUp ? "up" : "down";

      // 涨跌额
      if (this.changeEl) {
        this.changeEl.textContent = `${isUp ? "+" : ""}${this._fmtPrice(changeAmount)}`;
        this.changeEl.className = `kline-price-change ${changeClass}`;
      }

      // 涨跌幅
      if (this.changePctEl) {
        this.changePctEl.textContent = this._fmtPct(pctChange);
        this.changePctEl.className = `kline-price-change-pct ${changeClass}`;
      }

      // 价格颜色
      if (this.priceEl) {
        this.priceEl.className = `kline-price-value ${changeClass}`;
      }

      // 成交量
      if (this.volumeEl) {
        this.volumeEl.textContent = this._formatVolume(last.volume);
      }

      // 成交额
      if (this.amountEl) {
        this.amountEl.textContent = this._formatAmount(last.amount);
      }

      // 换手率
      if (this.turnoverEl) {
        this.turnoverEl.textContent = last.turnover_rate != null ? `${last.turnover_rate.toFixed(2)}%` : "-";
      }
    }

    _fmtPrice(value) {
      if (value == null) return "-";
      const v = Number(value);
      if (!Number.isFinite(v)) return "-";
      // 最多 3 位小数并去掉尾随 0：1446.44 → 1446.44，0.697 → 0.697，3.5 → 3.5
      return v.toFixed(3).replace(/\.?0+$/, "");
    }

    _fmtPct(value) {
      if (value == null || !Number.isFinite(Number(value))) return "-";
      const num = Number(value);
      return `${num > 0 ? "+" : ""}${num.toFixed(2)}%`;
    }

    _formatVolume(value) {
      if (value == null) return "-";
      if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
      if (value >= 10000) return `${(value / 10000).toFixed(2)}万`;
      return String(value);
    }

    _formatAmount(value) {
      if (value == null) return "-";
      if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
      if (value >= 10000) return `${(value / 10000).toFixed(2)}万`;
      return Number(value).toFixed(2);
    }

    async _handleLoadError(error) {
      const message = error.message || "";
      if (this.stockNameEl) this.stockNameEl.textContent = this.currentCode;
      if (this.stockCodeEl) this.stockCodeEl.textContent = "";
      this.syncBtn.disabled = false;
      if (error.detail && error.detail.reason === "no_local_data") {
        this._setStatus("empty", "暂无本地 K 线数据，点击同步后会自动绘图。");
        await this.chart.showPlaceholder("暂无本地 K 线数据，请点击'同步数据'");
        return;
      }
      this._setStatus("error", message || "加载失败");
      await this.chart.showError("加载失败，请检查代码或稍后重试");
    }

    _setStatus(type, text) {
      this.statusEl.className = `kline-status ${type || ""}`;
      this.statusEl.textContent = text || "";
    }

    // ---- 策略扫描 / 回测集成 ----

    _currentName() {
      return this.stockNameEl ? this.stockNameEl.textContent : "";
    }

    // 懒加载策略列表填充下拉框
    async _loadStrategies() {
      if (this.strategiesLoaded || !this.strategySelect) return;
      try {
        const data = await window.StockRadar.api.getBacktestStrategies();
        const strategies = data.strategies || data.items || [];
        strategies.forEach((s) => {
          const id = s.id || s.strategy_id;
          if (!id) return;
          const opt = document.createElement("option");
          opt.value = id;
          opt.textContent = s.name || id;
          this.strategySelect.appendChild(opt);
        });
        this.strategiesLoaded = true;
        if (this.currentStrategy) this.strategySelect.value = this.currentStrategy;
        this._updateStrategyButtons();
      } catch {
        // 静默失败，不影响 K 线核心功能
      }
    }

    // 策略按钮启用条件：已加载股票 + 已选策略
    _updateStrategyButtons() {
      const enabled = !!this.currentCode && !!this.currentStrategy;
      if (this.scanBtn) this.scanBtn.disabled = !enabled;
      if (this.backtestBtn) this.backtestBtn.disabled = !enabled;
    }

    // 扫描买点：无状态逐 K 线评估，最新一根即"今日是否买点"
    async _scanSignals() {
      if (!this.currentCode || !this.currentStrategy) return;
      const btn = this.scanBtn;
      btn.classList.add("running");
      btn.disabled = true;
      this._setStatus("loading", `扫描 ${this.currentStrategy} 买点信号...`);
      try {
        const result = await window.StockRadar.api.scanStrategySignals({
          code: this.currentCode,
          strategy_id: this.currentStrategy,
          adjust_type: "qfq",
          days: this.currentDays || 120,
        });
        // 叠加 scan 信号点（保留本地 K 线和标准指标，signals 按日期匹配）
        this.chart.setOverlay({
          type: "scan",
          signals: result.signals || [],
        });
        this._showScanBadge(result);
        const sigs = result.signals || [];
        const buySignals = sigs.filter((s) => s.buy);
        // "最新" 必须是最新**买点**的日期。原来写 result.latest_date（最新 K 线日），
        // 会让人以为今天出了买点，悬停今天却什么都没有。
        const lastBuyDate = buySignals.length ? buySignals[buySignals.length - 1].date : "-";
        this._setStatus("ok", `扫描完成：${buySignals.length} 个买点信号，最新买点 ${lastBuyDate}`);
      } catch (e) {
        this._setStatus("error", e.message || "扫描失败");
        this._hideScanBadge();
      } finally {
        btn.classList.remove("running");
        this._updateStrategyButtons();
      }
    }

    // 跑回测：默认 100w、满仓，异步轮询完成后叠加买卖点 + 净值曲线
    async _runBacktest() {
      if (!this.currentCode || !this.currentStrategy) return;
      const kline = this.chart.data && this.chart.data.kline;
      if (!kline || kline.length === 0) {
        this._setStatus("error", "请先加载 K 线数据");
        return;
      }
      const btn = this.backtestBtn;
      btn.classList.add("running");
      btn.disabled = true;
      this.chart.clearOverlay();
      this._hideScanBadge();
      this._setStatus("loading", `提交回测 ${this.currentStrategy}...`);
      try {
        // 回测区间对齐当前 K 线，保证买卖点日期匹配
        const run = await window.StockRadar.api.runBacktest({
          code: this.currentCode,
          strategy_id: this.currentStrategy,
          start_date: kline[0].date,
          end_date: kline[kline.length - 1].date,
          initial_cash: 1000000,    // 默认 100 万
          position_mode: "full",    // 默认满仓
          position_size: 1.0,
          adjust_type: "qfq",
          t_plus_1: true,
        });

        this._setStatus("loading", `回测运行中 ${run.run_id}...`);
        const detail = await this._pollBacktest(run.run_id);
        if (!detail.run || detail.run.status !== "completed") {
          throw new Error((detail.run && detail.run.error) || "回测未完成");
        }

        // 拉回测 K 线 + 买卖点，覆盖本地 K 线确保日期对齐；保留本地标准指标
        const klineData = await window.StockRadar.api.getBacktestKline(run.run_id);
        if (klineData.kline && klineData.kline.length > 0) {
          await this.chart.setData({
            kline: klineData.kline,
            indicators: (this.chart.data && this.chart.data.indicators) || {},
            code: klineData.code || this.currentCode,
            name: klineData.name || this._currentName() || this.currentCode,
            signals: [],
          });
        }

        // 叠加回测买卖点 + 净值曲线
        const result = detail.result || {};
        this.chart.setOverlay({
          type: "backtest",
          trades: (klineData.trades) || detail.trades || [],
          equityCurve: result.equity_curve || [],
          stats: result,
          initialCash: 1000000,
        });
        this._showBacktestStats(result);
      } catch (e) {
        this._setStatus("error", e.message || "回测失败");
      } finally {
        btn.classList.remove("running");
        this._updateStrategyButtons();
      }
    }

    // 轮询回测状态，完成或失败时 resolve
    _pollBacktest(runId, maxAttempts = 80, interval = 1500) {
      return new Promise((resolve, reject) => {
        let attempts = 0;
        const poll = async () => {
          attempts++;
          try {
            const detail = await window.StockRadar.api.getBacktestRunDetail(runId);
            const status = detail.run && detail.run.status;
            if (status === "completed" || status === "failed") {
              resolve(detail);
            } else if (attempts >= maxAttempts) {
              reject(new Error("回测超时，请稍后查看回测历史"));
            } else {
              this._setStatus("loading", `回测运行中... (${attempts})`);
              setTimeout(poll, interval);
            }
          } catch (e) {
            reject(e);
          }
        };
        poll();
      });
    }

    // 买点徽章：今日是买点 / 卖点 / 非买点
    _showScanBadge(result) {
      if (!this.scanBadge) return;
      const date = result.latest_date || "";
      let cls, text;
      if (result.latest_is_buy) {
        cls = "buy";
        text = "今日是买点 ✓";
      } else if (result.latest_is_sell) {
        cls = "sell";
        text = "今日是卖点";
      } else {
        cls = "neutral";
        text = "今日非买点";
      }
      this.scanBadge.className = `kline-scan-badge ${cls}`;
      this.scanBadge.innerHTML = `${text}${date ? `<span class="badge-date">${esc(date)}</span>` : ""}`;
      this.scanBadge.hidden = false;
    }

    _hideScanBadge() {
      if (!this.scanBadge) return;
      this.scanBadge.hidden = true;
      this.scanBadge.className = "kline-scan-badge";
    }

    // 回测统计摘要写入状态栏
    _showBacktestStats(stats) {
      if (!stats) return;
      const pct = (v) => (v == null ? "-" : `${(v * 100).toFixed(2)}%`);
      const wan = (v) => (v == null ? "-" : `${(v / 10000).toFixed(2)}万`);
      this._setStatus("ok",
        `收益 ${pct(stats.total_return)} · 终值 ${wan(stats.final_equity)} · `
        + `${stats.trade_count || 0}笔 · 胜率 ${pct(stats.win_rate)} · 回撤 ${pct(stats.max_drawdown)}`);
    }
  };
})();
