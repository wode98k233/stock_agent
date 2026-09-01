/*
   选股雷达 Web - K 线图组件
   配色方案：与项目整体淡绿色风格保持一致
*/

window.StockRadar = window.StockRadar || {};

(function () {
  "use strict";

  const ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js";

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

    // MACD 颜色
    macdDif: "#f59e0b",   // DIF - 琥珀色
    macdDea: "#176b5b",   // DEA - 品牌绿色
    macdHistUp: "rgba(231, 76, 60, 0.6)",
    macdHistDown: "rgba(23, 107, 91, 0.6)",

    // KDJ 颜色
    kdjK: "#f59e0b",      // K线 - 琥珀色
    kdjD: "#176b5b",      // D线 - 品牌绿色
    kdjJ: "#8b5cf6",      // J线 - 紫色

    // RSI 颜色
    rsi6: "#f59e0b",
    rsi12: "#176b5b",
    rsi24: "#8b5cf6",

    // DPO 颜色
    dpo: "#f59e0b",      // DPO - 琥珀色
    madpo: "#176b5b",    // MADPO - 品牌绿色

    // 布林带颜色
    bollUpper: "#e74c3c",
    bollMid: "#f59e0b",
    bollLower: "#176b5b",

    // 信号颜色
    signalBuy: "#e74c3c",
    signalSell: "#176b5b",
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
        // 网格布局 - 回测有净值时多一栏
        grid: hasEquity
          ? [
              { left: "8%", right: "3%", top: "12%", height: "42%" },  // K线主图
              { left: "8%", right: "3%", top: "58%", height: "8%" },   // 成交量
              { left: "8%", right: "3%", top: "70%", height: "12%" },  // 技术指标
              { left: "8%", right: "3%", top: "86%", height: "10%" },  // 净值曲线
            ]
          : [
              { left: "8%", right: "3%", top: "12%", height: "50%" },  // K线主图
              { left: "8%", right: "3%", top: "66%", height: "10%" },  // 成交量
              { left: "8%", right: "3%", top: "80%", height: "14%" },  // 技术指标
            ],
        // X轴 - 动态数量
        xAxis: allGrids.map((gridIndex) => ({
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
          splitLine: { show: false },
        })),
        // Y轴
        yAxis: [
          this._axisConfig(0, true),
          this._axisConfig(1, false, (value) => this._formatVolume(value)),
          this._axisConfig(2, true),
          ...(hasEquity ? [this._axisConfig(3, true)] : []),
        ],
        // 缩放控件
        dataZoom: [
          { type: "inside", xAxisIndex: allGrids, start: 0, end: 100 },
          {
            type: "slider",
            xAxisIndex: allGrids,
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
          ...this._buildSignalSeries(signals, dates, kline),
          // 策略 overlay：scan 信号点 / 回测买卖点
          ...(overlay ? this._buildOverlaySeries(overlay, dates, kline) : []),
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

    // 均线 - 东财风格：平滑曲线
    _buildMASeries(indicators) {
      return [
        ["ma5", "MA5", COLORS.ma5],
        ["ma10", "MA10", COLORS.ma10],
        ["ma20", "MA20", COLORS.ma20],
        ["ma60", "MA60", COLORS.ma60],
      ].map(([key, name, color]) => ({
        name,
        type: "line",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: indicators[key] || [],
        smooth: true,
        lineStyle: { width: 1.5, color },
        symbol: "none",
        silent: true,
      }));
    }

    // 构建主图系列（K线/折线/面积）
    _buildMainSeries(kline, ohlc, dates, name, code) {
      const chartType = this.chartType || "candle_solid";

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
          this._lineSeries("J", indicators.kdj.j, COLORS.kdjJ),
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
    _lineSeries(name, data, color) {
      return {
        name,
        type: "line",
        xAxisIndex: 2,
        yAxisIndex: 2,
        data,
        smooth: true,
        lineStyle: { width: 1.5, color },
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

    _buildSignalSeries(signals, dates, kline) {
      if (!signals || signals.length === 0) return [];

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

        points.push({
          value: [s.date, price],
          symbol: SHAPE_SYMBOL[meta.shape] || "circle",
          symbolSize: 20,
          itemStyle: {
            color,
            borderColor: "#ffffff",
            borderWidth: 2,
            shadowBlur: 5,
            shadowColor: meta.good ? "rgba(231,76,60,0.5)" : "rgba(23,107,91,0.5)",
          },
          label: {
            show: true,
            formatter: meta.letter,   // 图上只显示指标字母（M/K/D/B/A），形状+颜色区分类型与好/坏，一眼可辨
            position: "inside",
            align: "center",
            verticalAlign: "middle",
            color: "#ffffff",
            fontSize: 10,
            fontWeight: "bold",
            backgroundColor: "rgba(0,0,0,0.55)",   // 半透明背景徽章，把字母和图标线条分开层级，避免互相压线
            borderColor: "#ffffff",
            borderWidth: 1,
            borderRadius: 3,
            padding: [1, 3],
            shadowBlur: 2,
            shadowColor: "rgba(0,0,0,0.4)",
          },
          _sig: s,
        });
      }

      return [{
        name: "交易信号",
        type: "scatter",
        xAxisIndex: 0,
        yAxisIndex: 0,
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

      return html + "</div>";
    }

    _fmtPrice(value) {
      return value == null ? "-" : Number(value).toFixed(2);
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
      this.daysButtons = Array.from(document.querySelectorAll(".kline-days-btn"));
      this.indicatorButtons = Array.from(document.querySelectorAll(".kline-indicator-btn"));
      this.chart = new KlineChart(document.getElementById("klineChartContainer"));

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
          this.daysButtons.forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          this.currentDays = Number(button.dataset.days || 120);
          localStorage.setItem("kline_default_days", String(this.currentDays));
          if (this.currentCode) this.load(this.currentCode);
        });
      });

      // K线样式切换
      this.chartTypeButtons = Array.from(document.querySelectorAll(".kline-chart-type-btn"));
      this.chartType = localStorage.getItem("kline_chart_type") || "candle_solid";
      this._updateChartTypeButtons();
      this.chartTypeButtons.forEach((button) => {
        button.addEventListener("click", () => {
          this.chartTypeButtons.forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          this.chartType = button.dataset.type;
          localStorage.setItem("kline_chart_type", this.chartType);
          this.chart.setChartType(this.chartType);
        });
      });

      this.indicatorButtons.forEach((button) => {
        button.addEventListener("click", () => {
          this.indicatorButtons.forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          this.chart.setSubIndicator(button.dataset.indicator);
        });
      });
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

    _updateDaysButtons() {
      this.daysButtons.forEach((button) => {
        const days = Number(button.dataset.days || 120);
        if (days === this.currentDays) {
          button.classList.add("active");
        } else {
          button.classList.remove("active");
        }
      });
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
      this.searchInput.value = this.currentCode;
      this._hideSearchResults();
      this._setStatus("loading", `正在读取 ${this.currentCode} 本地 K 线...`);
      this.syncBtn.disabled = true;
      this.chart.clearOverlay();
      this._hideScanBadge();
      await this.chart.showLoading("读取本地 K 线数据...");
      try {
        // 并行获取 K 线数据和信号数据
        const [data, signalsData] = await Promise.all([
          window.StockRadar.api.getKline(this.currentCode, this.currentDays, this.currentMarket),
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
      } catch (error) {
        // 如果开启自动同步且是无数据错误，自动同步
        if (this.autoSync && error.detail && error.detail.reason === "no_local_data") {
          await this.syncCurrent();
          return;
        }
        this._handleLoadError(error);
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
      return value == null ? "-" : Number(value).toFixed(2);
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
        const buyCount = (result.signals || []).filter((s) => s.buy).length;
        this._setStatus("ok", `扫描完成：${buyCount} 个买点信号，最新 ${result.latest_date || "-"}`);
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
