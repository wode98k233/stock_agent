/**
 * 多策略对比图表 — 热力图 + 排名表
 *
 * 暴露: window.StockRadar.MultiCompareChart
 * 接口:
 *   - renderHeatmap(container, items, strategies, codes, codeNames, options)
 *   - getColorMode()
 *   - setColorMode(mode)
 */
window.StockRadar = window.StockRadar || {};

window.StockRadar.MultiCompareChart = (() => {
  'use strict';

  let _chart = null;
  let _colorMode = 'return';  // 'return' | 'sharpe' | 'drawdown'
  let _heatmapData = null;

  const COLOR_MODES = {
    return:  { key: 'total_return',  min: null, max: null, label: v => (v * 100).toFixed(1) + '%', name: '收益' },
    sharpe:  { key: 'sharpe_ratio',  min: -2,   max: null, label: v => v.toFixed(2),               name: '夏普' },
    drawdown:{ key: 'max_drawdown',   min: null, max: 0,    label: v => (v * 100).toFixed(1) + '%', name: '最大回撤' },
  };

  // ── 热力图 ──────────────────────────────────────────────────

  function renderHeatmap(container, items, strategies, codes, codeNames, options) {
    if (!container || typeof echarts === 'undefined') return;
    _heatmapData = { items, strategies, codes, codeNames };

    if (!_chart || _chart.getDom() !== container) {
      _chart = echarts.init(container);
    }
    _chart.clear();

    const mode = COLOR_MODES[_colorMode];

    // X 轴 = 标的（含名称）
    const xData = codes.map(c => (codeNames[c] || c) + '\n' + c);
    const yData = strategies.map(s => s.name || s.id);

    // 数据：[col, row, value]
    const data = items.map(d => {
      const ci = codes.indexOf(d.code);
      const si = strategies.findIndex(s => (s.id || s.strategy_id) === (d.strategy_id || d.strategyId));
      return [ci, si, d[mode.key] ?? 0];
    });

    // 动态 min/max
    const vals = items.map(d => d[mode.key] ?? 0).filter(v => v != null);
    let vMin = mode.min;
    let vMax = mode.max;
    if (vMin == null) vMin = Math.min(0, ...vals);
    if (vMax == null) vMax = Math.max(...vals);
    if (vMin === vMax) { vMin -= 0.01; vMax += 0.01; }

    // 回撤颜色反转（负值=红即好）
    const colorRange = _colorMode === 'drawdown'
      ? ['#14b143', '#fff', '#ef232a']
      : ['#14b143', '#fff', '#ef232a'];

    _chart.setOption({
      tooltip: {
        backgroundColor: '#fff',
        borderColor: '#e5e7eb',
        textStyle: { color: '#1a1a1a', fontSize: 11 },
        formatter: p => {
          const d = items.find(r =>
            codes.indexOf(r.code) === p.data[0] &&
            (strategies.findIndex(s => (s.id || s.strategy_id) === (r.strategy_id || r.strategyId)) === p.data[1])
          );
          if (!d) return '';
          const nm = codeNames[d.code] || d.code;
          const excess = (d.excess_return || 0);
          const exLabel = excess >= 0 ? '+' + (excess * 100).toFixed(2) + 'pp' : (excess * 100).toFixed(2) + 'pp';
          return `<b>${yData[p.data[1]]} × ${nm} (${d.code})</b><br/>
            收益: <b>${((d.total_return||0)*100).toFixed(2)}%</b> (超额 ${exLabel})<br/>
            夏普: ${(d.sharpe_ratio||0).toFixed(2)} | 最大回撤: ${((d.max_drawdown||0)*100).toFixed(2)}%<br/>
            最大连亏: ${d.max_consecutive_losses||0} 笔`;
        }
      },
      grid: { left: 120, right: 40, top: 10, bottom: 50 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 0 }, position: 'top' },
      yAxis: { type: 'category', data: yData, axisLabel: { fontSize: 11 } },
      visualMap: {
        min: vMin, max: vMax, calculable: true,
        orient: 'horizontal', left: 'center', bottom: 0,
        inRange: { color: colorRange },
        formatter: mode.label
      },
      series: [{
        type: 'heatmap', data,
        label: { show: true, fontSize: 10, formatter: p => mode.label(p.data[2]) },
        emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' } }
      }]
    });

    // 热力图点击事件：通知回调
    _chart.off('click');
    _chart.on('click', params => {
      if (params.componentType === 'series') {
        const ci = params.data[0];
        const si = params.data[1];
        const code = codes[ci];
        const strategy = strategies[si];
        if (options.onCellClick) {
          options.onCellClick(strategy, code);
        }
      }
    });
  }

  function getColorMode() { return _colorMode; }

  function setColorMode(mode) {
    if (!COLOR_MODES[mode]) return;
    _colorMode = mode;
  }

  function resize() {
    if (_chart) _chart.resize();
  }

  return { renderHeatmap, getColorMode, setColorMode, COLOR_MODES, resize };
})();
