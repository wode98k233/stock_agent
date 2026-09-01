/**
 * 批量对比图表 — 对齐原型 docs/superpowers/backtest_unit_20260728/prototype.html
 *
 * 暴露: window.StockRadar.BatchCompareChart
 * 接口:
 *   - render(container, enrichedItems, options)  渲染图表
 *   - getVisibleCodes()                           获取 legend 中当前可见的 code 列表
 *   - resize()                                    调整图表大小
 *
 * enrichedItems 元素结构:
 *   { code, status, asset_type, result: { total_return, annual_return, sharpe_ratio,
 *     max_drawdown, win_rate, trade_count, equity_curve, trades }, equity_curve, error }
 *
 * equity_curve 支持格式: [{date, equity}] | [{date, value}] | [[date, value]]
 * trades 支持格式: [{date, direction:'buy'|'sell'}] | [{date, type:'buy'|'sell'}]
 */
window.StockRadar = window.StockRadar || {};

window.StockRadar.BatchCompareChart = (() => {
  'use strict';

  const COLORS = ['#176b5b', '#7c3aed', '#0369a1', '#b45309', '#dc2626', '#059669',
                  '#be185d', '#4338ca', '#0d9488', '#c2410c'];

  let _chart = null;
  let _enriched = [];
  let _options = {};
  let _visibleCodes = [];
  let _onLegendToggleCb = null;

  // ── 工具函数 ──────────────────────────────────────────────

  /** 从 enriched item 提取净值曲线，统一返回 [{date, equity}] */
  function _extractCurve(item) {
    const curve = item.equity_curve || (item.result && item.result.equity_curve) || [];
    if (!Array.isArray(curve) || curve.length === 0) return [];
    // 适配多种格式
    return curve.map(p => {
      if (Array.isArray(p)) return { date: p[0], equity: +p[1] };
      const eq = p.equity != null ? p.equity : (p.value != null ? p.value : (p.nav != null ? p.nav : null));
      return { date: p.date, equity: eq != null ? +eq : null };
    }).filter(p => p.date && p.equity != null);
  }

  /** 从 enriched item 提取交易列表，统一返回 [{date, direction:'buy'|'sell'}] */
  function _extractTrades(item) {
    const trades = (item.result && item.result.trades) || item.trades || [];
    if (!Array.isArray(trades)) return [];
    return trades.map(t => {
      const d = t.trade_date || t.date || '';
      const dir = t.direction || t.type || t.side || '';
      return { date: d, direction: dir.toLowerCase().startsWith('b') ? 'buy' : 'sell' };
    }).filter(t => t.date);
  }

  /** 获取所有日期的并集（排序） */
  function _unionDates(curves) {
    const set = new Set();
    curves.forEach(c => c.forEach(p => set.add(p.date)));
    return Array.from(set).sort();
  }

  /** 将曲线按日期对齐，缺失用前值填充，并归一化到初始资金基准（1.0 = 本金） */
  function _alignCurve(curve, dates, initialCash) {
    const cash = initialCash || 100000;
    const map = new Map(curve.map(p => [p.date, +(p.equity / cash).toFixed(6)]));
    let prev = 1.0;
    return dates.map(d => {
      if (map.has(d)) prev = map.get(d);
      return prev;
    });
  }

  /** 检测 B/S 开关是否打开 */
  function _bsEnabled() {
    const el = document.getElementById('btBsToggle');
    return el ? el.checked : false;
  }

  /** 检测综合净值开关是否打开 */
  function _aggregateEnabled() {
    const el = document.getElementById('btAggregateToggle');
    return el ? el.checked : true;
  }

  // ── 核心渲染 ──────────────────────────────────────────────

  function render(container, enrichedItems, options) {
    if (!container) return;
    _enriched = (enrichedItems || []).filter(it => it && it.code);
    _options = options || {};
    _onLegendToggleCb = _options.onLegendToggle || null;

    // 新批次渲染时重置 visibleCodes（检测 code 列表是否变化）
    const newCodes = _enriched.map(it => it.code);
    const newSet = new Set(newCodes);
    const hasStaleCodes = _visibleCodes.some(c => !newSet.has(c));
    if (hasStaleCodes || _visibleCodes.length === 0) {
      _visibleCodes = newCodes;
    }

    if (typeof echarts === 'undefined') {
      container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">ECharts 未加载</div>';
      return;
    }

    if (!_chart || _chart.getDom() !== container) {
      _chart = echarts.init(container);
      _chart.on('legendselectchanged', (params) => {
        const selected = params.selected || {};
        _visibleCodes = Object.keys(selected).filter(k => selected[k] && _enriched.some(it => _seriesName(it) === k));
        if (_onLegendToggleCb) {
          try { _onLegendToggleCb(_visibleCodes); } catch (e) { console.error('legend 回调异常:', e); }
        }
      });
    }
    _chart.clear();

    // 只取 completed 且有净值曲线的标的
    const valid = _enriched.filter(it => {
      if (it.status !== 'completed') return false;
      const curve = _extractCurve(it);
      return curve.length > 0;
    });
    if (valid.length === 0) {
      container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">暂无可对比的净值数据</div>';
      return;
    }

    const curves = valid.map(it => _extractCurve(it));
    const dates = _unionDates(curves);

    // 构建每条 series
    const series = valid.map((it, idx) => {
      const curve = curves[idx];
      const data = _alignCurve(curve, dates, _options.initialCash);
      const name = _seriesName(it);
      const color = COLORS[idx % COLORS.length];
      const showBs = _bsEnabled();
      const trades = showBs ? _extractTrades(it) : [];

      const markPoint = showBs && trades.length > 0 ? {
        symbol: 'arrow',
        symbolSize: 12,
        data: trades.map(t => {
          const di = dates.indexOf(t.date);
          return {
            name: t.direction === 'buy' ? 'B' : 'S',
            coord: [t.date, di >= 0 ? data[di] : null],
            value: t.direction === 'buy' ? 'B' : 'S',
            symbolRotate: t.direction === 'buy' ? 0 : 180,
            itemStyle: { color: t.direction === 'buy' ? '#ef232a' : '#14b143' },
            label: {
              show: true,
              position: t.direction === 'buy' ? 'bottom' : 'top',
              fontSize: 9,
              formatter: t.direction === 'buy' ? 'B' : 'S',
            },
          };
        }).filter(p => p.coord[1] != null),
      } : undefined;

      return {
        name,
        type: 'line',
        data,
        smooth: true,
        symbol: 'none',
        lineStyle: { color, width: 2 },
        itemStyle: { color },
        markPoint,
      };
    });

    // 综合净值曲线（可见标的的均值）
    const showAgg = _aggregateEnabled();
    if (showAgg && series.length > 0) {
      const validNames = new Set(valid.map(it => _seriesName(it)));
      const aggData = dates.map((d, i) => {
        let sum = 0, cnt = 0;
        valid.forEach((it, idx) => {
          const name = _seriesName(it);
          if (_visibleCodes.includes(it.code) && validNames.has(name)) {
            sum += series[idx].data[i];
            cnt++;
          }
        });
        return cnt > 0 ? +(sum / cnt).toFixed(4) : null;
      });
      series.push({
        name: '综合平均净值',
        type: 'line',
        data: aggData,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#1a1a1a', width: 3, type: 'dashed' },
        z: 10,
      });
    }

    // legend selected 状态
    const selected = {};
    valid.forEach(it => {
      const name = _seriesName(it);
      selected[name] = _visibleCodes.includes(it.code);
    });
    if (showAgg) selected['综合平均净值'] = true;

    _chart.setOption({
      backgroundColor: '#fff',
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#fff',
        borderColor: '#e5e7eb',
        textStyle: { color: '#1a1a1a', fontSize: 11 },
        axisPointer: { type: 'cross', lineStyle: { color: '#9ca3af', type: 'dashed' } },
        formatter: _tooltipFormatter(valid, dates),
      },
      legend: {
        type: 'scroll',
        selected,
        top: 0,
        textStyle: { fontSize: 11 },
        itemWidth: 14,
        itemHeight: 8,
      },
      grid: { left: 50, right: 20, top: 40, bottom: 30 },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: '#9ca3af', fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f0f0f0' } },
        axisLabel: {
          color: '#9ca3af',
          fontSize: 10,
          formatter: v => ((v - 1) * 100).toFixed(1) + '%',
        },
      },
      series,
    });
  }

  function _seriesName(item) {
    const label = item.name || item.stock_name || '';
    return label ? `${item.code} ${label}` : item.code;
  }

  /** tooltip 按收益率排序显示 */
  function _tooltipFormatter(validItems, dates) {
    return (params) => {
      if (!Array.isArray(params) || params.length === 0) return '';
      const date = params[0].axisValue;
      let html = `<div style="font-weight:600;margin-bottom:4px">${date}</div>`;
      // 按收益率排序
      const sorted = params.slice().sort((a, b) => {
        const ra = _getReturn(validItems, a.seriesName);
        const rb = _getReturn(validItems, b.seriesName);
        return rb - ra;
      });
      sorted.forEach(p => {
        const val = p.data != null ? ((p.data - 1) * 100).toFixed(2) + '%' : '—';
        const color = p.color;
        html += `<div style="display:flex;align-items:center;gap:6px;margin:1px 0">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color}"></span>
          <span style="flex:1;color:#4b5563">${p.seriesName}</span>
          <span style="font-family:monospace;font-weight:600;color:${(p.data - 1) >= 0 ? '#ef232a' : '#14b143'}">${val}</span>
        </div>`;
      });
      return html;
    };
  }

  function _getReturn(validItems, seriesName) {
    const it = validItems.find(it => _seriesName(it) === seriesName);
    return it && it.result ? (it.result.total_return || 0) : 0;
  }

  // ── 公共接口 ──────────────────────────────────────────────

  function getVisibleCodes() {
    return _visibleCodes.slice();
  }

  function resize() {
    if (_chart) _chart.resize();
  }

  /** 绑定开关事件（由 backtest.js 调用，也可自动绑定） */
  function bindToggles() {
    const bsToggle = document.getElementById('btBsToggle');
    const aggToggle = document.getElementById('btAggregateToggle');
    if (bsToggle && !bsToggle._btBound) {
      bsToggle._btBound = true;
      bsToggle.addEventListener('change', () => {
        if (_chart) render(_chart.getDom(), _enriched, _options);
      });
    }
    if (aggToggle && !aggToggle._btBound) {
      aggToggle._btBound = true;
      aggToggle.addEventListener('change', () => {
        if (_chart) render(_chart.getDom(), _enriched, _options);
      });
    }
  }

  // 自动尝试绑定
  setTimeout(bindToggles, 500);

  return { render, getVisibleCodes, resize, bindToggles };
})();
