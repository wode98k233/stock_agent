/* 回测结果 K 线图渲染：candlestick + 成交量 + 买卖点 + 本策略指标。
   独立于 chart-kline.js，仅接收数据画图，不耦合在线搜索/同步。

   opts（可选，第三参）：
     tradeSeries: true  —— 买卖点改画成两条 scatter 系列（三角形=买入 / 菱形=卖出），
                            好处是图例里能直接勾选「买入点 / 卖出点」，并且能显示中文名。
                            不传时沿用原 markPoint 箭头的画法（回测页行为不变）。 */
(function () {
  // 涨跌色统一走 CSS token，不再硬编码（Backtest 页曾用 #ef232a/#14b143，与主 K 线 #e74c3c/#176b5b 不一致）
  const UP_FALLBACK = '#e74c3c';
  const DOWN_FALLBACK = '#176b5b';
  // 与 chart-kline.js 的 COLORS.indicatorMuted 保持一致
  const MUTED_LINE = '#c2c1ba';
  const MUTED_ITEM = '#9ca3af';
  // 副图指标配色（方案 C）：快线品牌绿做视线落点，慢线中灰，第三线浅灰点线
  const SUB_FAST = '#176b5b';
  const SUB_SLOW = '#8f8e88';
  const SUB_THIRD = '#a9a8a1';

  /* 蜡烛填充跟随主 K 线的画法偏好（localStorage['kline_chart_type']）。
     回测图只承接"填充"这一个维度：line/area 是主 K 线的画法，回测图仍按蜡烛画。 */
  const CANDLE_FILL = {
    candle_solid:       { up: true,  down: true  },
    candle_stroke:      { up: false, down: false },
    candle_up_stroke:   { up: false, down: true  },
    candle_down_stroke: { up: true,  down: false },
  };

  function _candleItemStyle(upColor, downColor) {
    let mode = 'candle_up_stroke';
    try { mode = localStorage.getItem('kline_chart_type') || mode; } catch (e) { /* 隐私模式无 localStorage */ }
    const f = CANDLE_FILL[mode] || CANDLE_FILL.candle_up_stroke;
    return {
      color: f.up ? upColor : 'transparent',
      color0: f.down ? downColor : 'transparent',
      borderColor: upColor,
      borderColor0: downColor,
      borderWidth: 1.2,
    };
  }

  function _cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  function renderBacktestKline(el, data, opts) {
    if (!el || typeof echarts === 'undefined') return;
    data = data || {};
    const kline = data.kline || [];
    if (kline.length === 0) {
      el.innerHTML = '<div style="padding:40px;text-align:center;color:#9ca3af;font-size:13px;">无 K 线数据</div>';
      return;
    }

    const chart = echarts.getInstanceByDom(el) || echarts.init(el);
    chart.clear();

    const UP = _cssVar('--up', UP_FALLBACK);
    const DOWN = _cssVar('--down', DOWN_FALLBACK);

    const dates = kline.map(d => d.date);
    const ohlc = kline.map(d => [d.open, d.close, d.low, d.high]);  // ECharts: [open,close,low,high]
    const volumes = kline.map(d => ({
      value: d.volume,
      itemStyle: { color: d.close >= d.open ? UP : DOWN },
    }));

    const ind = data.indicators || { main: [], sub: [] };
    const hasSub = (ind.sub || []).length > 0;

    // 主图叠加指标线：统一发丝灰，颜色不再表达"指标身份"
    const overlaySeries = (ind.main || []).map(s => ({
      name: s.name, type: 'line', data: s.data, smooth: true, symbol: 'none',
      lineStyle: { width: 0.9, color: MUTED_LINE }, itemStyle: { color: MUTED_ITEM },
      xAxisIndex: 0, yAxisIndex: 0,
    }));

    const o = opts || {};
    const trades = data.trades || [];

    // 买卖点（可选）：两条 scatter 系列，图例可勾选、形状可区分
    const tradeSeries = [];
    if (o.tradeSeries) {
      const buyColor = _cssVar('--up', UP_FALLBACK);
      const sellColor = _cssVar('--down', DOWN_FALLBACK);
      const buy = [], sell = [];
      trades.forEach(t => {
        (t.direction === 'buy' ? buy : sell).push([t.date, t.price]);
      });
      tradeSeries.push({
        name: '买入点', type: 'scatter', data: buy, xAxisIndex: 0, yAxisIndex: 0, z: 10,
        symbol: 'triangle', symbolSize: 10, symbolOffset: [0, 8],
        itemStyle: { color: buyColor },
      });
      tradeSeries.push({
        name: '卖出点', type: 'scatter', data: sell, xAxisIndex: 0, yAxisIndex: 0, z: 10,
        symbol: 'diamond', symbolSize: 10, symbolOffset: [0, -8],
        itemStyle: { color: sellColor },
      });
    }

    // 买卖点 markPoint（默认画法；tradeSeries 模式下不重复画）
    const markData = (data.trades || []).map(t => {
      const isBuy = t.direction === 'buy';
      return {
        name: isBuy ? '买入' : '卖出',
        coord: [t.date, t.price],
        value: t.price,
        symbol: 'arrow', symbolSize: 14, symbolRotate: isBuy ? 0 : 180,
        itemStyle: { color: isBuy ? UP : DOWN },
        label: { show: true, position: isBuy ? 'bottom' : 'top', fontSize: 10,
                 formatter: () => (isBuy ? 'B' : 'S') },
        _trade: t,
      };
    });

    // grid 布局：主图 + 成交量（+ 副图）
    const grids = hasSub
      ? [{ left: 55, right: 20, top: 30, height: '46%' },
         { left: 55, right: 20, top: '60%', height: '14%' },
         { left: 55, right: 20, top: '78%', height: '16%' }]
      : [{ left: 55, right: 20, top: 30, height: '60%' },
         { left: 55, right: 20, top: '74%', height: '18%' }];

    const xAxis = grids.map((g, i) => ({
      type: 'category', data: dates, gridIndex: i, boundaryGap: true,
      axisLine: { lineStyle: { color: '#e5e7eb' } },
      axisLabel: { show: i === grids.length - 1, color: '#9ca3af', fontSize: 10 },
      splitLine: { show: false }, axisTick: { show: false },
    }));
    const yAxis = grids.map((g, i) => ({
      scale: true, gridIndex: i,
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#9ca3af', fontSize: 10 },
      splitLine: { lineStyle: { color: '#f3f4f6' } },
    }));

    const series = [
      {
        name: 'K线', type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: _candleItemStyle(UP, DOWN),
        markPoint: o.tradeSeries ? undefined : { data: markData, symbolSize: 14 },
      },
      ...tradeSeries,
      ...overlaySeries,
      { name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 1 },
    ];

    // 副图指标（取第一个 sub）。配色与 chart-kline.js 的 COLORS 保持一致：
    // 快线品牌绿 / 慢线中灰 / 第三线浅灰点线；柱体保留红涨绿跌。
    if (hasSub) {
      const sub = ind.sub[0];
      const subLine = (name, data, color, type) => ({
        name, type: 'line', data, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none',
        lineStyle: { width: 1.1, color, type: type || 'solid' }, itemStyle: { color: MUTED_ITEM },
      });
      if (sub.type === 'MACD') {
        series.push(subLine('DIF', sub.dif, SUB_FAST));
        series.push(subLine('DEA', sub.dea, SUB_SLOW));
        series.push({ name: 'MACD', type: 'bar', data: sub.hist, xAxisIndex: 2, yAxisIndex: 2,
          itemStyle: { color: p => (p.data >= 0 ? UP : DOWN) } });
      } else if (sub.type === 'RSI') {
        series.push(subLine(`RSI${sub.period}`, sub.data, SUB_FAST));
      } else if (sub.type === 'KDJ') {
        series.push(subLine('K', sub.k, SUB_FAST));
        series.push(subLine('D', sub.d, SUB_SLOW));
        series.push(subLine('J', sub.j, SUB_THIRD, 'dotted'));
      } else if (sub.type === 'DPO') {
        series.push({
          name: 'DPO', type: 'line', data: sub.dpo, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none',
          lineStyle: { width: 1.1, color: SUB_FAST },
          itemStyle: { color: MUTED_ITEM },
          markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0 }],
                      lineStyle: { color: '#9ca3af', type: 'dashed', width: 1 } },
        });
        series.push(subLine('MADPO', sub.madpo, SUB_SLOW));
      }
    }

    const gridCount = grids.length;
    chart.setOption({
      animation: false,
      legend: { top: 4, right: 10, textStyle: { fontSize: 10 } },
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'cross' },
        backgroundColor: '#fff', borderColor: '#e5e7eb', textStyle: { color: '#1a1a1a', fontSize: 11 },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: grids,
      xAxis,
      yAxis,
      dataZoom: [
        { type: 'inside', xAxisIndex: Array.from({ length: gridCount }, (_, i) => i), start: 0, end: 100 },
        { type: 'slider', xAxisIndex: Array.from({ length: gridCount }, (_, i) => i), bottom: 2, height: 14 },
      ],
      series,
    });
    chart.resize();
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.renderBacktestKline = renderBacktestKline;
})();
