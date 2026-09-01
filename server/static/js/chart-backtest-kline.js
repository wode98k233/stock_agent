/* 回测结果 K 线图渲染：candlestick + 成交量 + 买卖点 + 本策略指标。
   独立于 chart-kline.js，仅接收数据画图，不耦合在线搜索/同步。 */
(function () {
  const UP = '#ef232a';    // 涨/买入 红
  const DOWN = '#14b143';  // 跌/卖出 绿

  function renderBacktestKline(el, data) {
    if (!el || typeof echarts === 'undefined') return;
    data = data || {};
    const kline = data.kline || [];
    if (kline.length === 0) {
      el.innerHTML = '<div style="padding:40px;text-align:center;color:#9ca3af;font-size:13px;">无 K 线数据</div>';
      return;
    }

    const chart = echarts.getInstanceByDom(el) || echarts.init(el);
    chart.clear();

    const dates = kline.map(d => d.date);
    const ohlc = kline.map(d => [d.open, d.close, d.low, d.high]);  // ECharts: [open,close,low,high]
    const volumes = kline.map(d => ({
      value: d.volume,
      itemStyle: { color: d.close >= d.open ? UP : DOWN },
    }));

    const ind = data.indicators || { main: [], sub: [] };
    const hasSub = (ind.sub || []).length > 0;

    // 主图叠加指标线
    const overlaySeries = (ind.main || []).map(s => ({
      name: s.name, type: 'line', data: s.data, smooth: true, symbol: 'none',
      lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0,
    }));

    // 买卖点 markPoint
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
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        markPoint: { data: markData, symbolSize: 14 },
      },
      ...overlaySeries,
      { name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 1 },
    ];

    // 副图指标（取第一个 sub）
    if (hasSub) {
      const sub = ind.sub[0];
      if (sub.type === 'MACD') {
        series.push({ name: 'DIF', type: 'line', data: sub.dif, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#f59e0b' } });
        series.push({ name: 'DEA', type: 'line', data: sub.dea, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#3b82f6' } });
        series.push({ name: 'MACD', type: 'bar', data: sub.hist, xAxisIndex: 2, yAxisIndex: 2,
          itemStyle: { color: p => (p.data >= 0 ? UP : DOWN) } });
      } else if (sub.type === 'RSI') {
        series.push({ name: `RSI${sub.period}`, type: 'line', data: sub.data, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#8b5cf6' } });
      } else if (sub.type === 'KDJ') {
        series.push({ name: 'K', type: 'line', data: sub.k, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#f59e0b' } });
        series.push({ name: 'D', type: 'line', data: sub.d, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#3b82f6' } });
        series.push({ name: 'J', type: 'line', data: sub.j, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#ec4899' } });
      } else if (sub.type === 'DPO') {
        series.push({
          name: 'DPO', type: 'line', data: sub.dpo, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none',
          lineStyle: { width: 1, color: '#10b981' },
          markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0 }],
                      lineStyle: { color: '#9ca3af', type: 'dashed', width: 1 } },
        });
        series.push({ name: 'MADPO', type: 'line', data: sub.madpo, xAxisIndex: 2, yAxisIndex: 2, symbol: 'none', lineStyle: { width: 1, color: '#f59e0b' } });
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
