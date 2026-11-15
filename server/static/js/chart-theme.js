/* 报告图表的统一主题（B1）
 *
 * 背景：报告里的图表此前从未调用 echarts.registerTheme，颜色用的是 ECharts 默认色板
 * （蓝 / 琥珀 / 绿），与站内的品牌绿 + 涨跌红绿 + 灰阶体系完全脱节。
 *
 * 取色原则与 K 线一致：
 *   - 颜色表达语义，不表达装饰：红=涨/买，绿=跌/卖，灰系统表达"重要性递减"
 *   - 主角用品牌绿，其余沿明度梯下降；不给每个系列发一个色相
 *
 * 用法：
 *   SRChartTheme.register(echarts)   // 幂等，ECharts 加载后调一次
 *   echarts.init(el, SRChartTheme.NAME)
 */
(function (global) {
  "use strict";

  var NAME = "stockradar";

  // 系列配色：主角品牌绿 → 中灰 → 浅灰 → 深墨。
  // 报告图是静态的（没有 tooltip 兜底），所以明度阶梯要拉开：
  // 第二档 #8f8e88 与第三档 #c2c1ba 的差必须肉眼可分。
  var PALETTE = ["#176b5b", "#8f8e88", "#c2c1ba", "#1c1c1a", "#6b7280", "#a9a8a1"];

  var COLORS = {
    brand: "#176b5b",
    up: "#e74c3c",
    down: "#176b5b",
    text: "#1c2430",
    axis: "#6b7280",      // 轴标签 / 轴名
    legend: "#6b7280",
    grid: "#f0f1ee",      // 分割线
    axisLine: "#e5e7eb",
    tooltipBg: "rgba(255,255,255,0.96)",
    muted: "#a9a8a1",
  };

  var registered = false;

  function buildTheme() {
    return {
      color: PALETTE,
      backgroundColor: "transparent",
      textStyle: { fontFamily: "-apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif" },
      title: { textStyle: { color: COLORS.text, fontSize: 12, fontWeight: 600 } },
      legend: { textStyle: { color: COLORS.legend, fontSize: 11 }, itemWidth: 12, itemHeight: 8 },
      tooltip: {
        backgroundColor: COLORS.tooltipBg,
        borderColor: COLORS.axisLine,
        borderWidth: 1,
        textStyle: { color: COLORS.text, fontSize: 12 },
        axisPointer: { lineStyle: { color: COLORS.muted } },
      },
      categoryAxis: {
        axisLine: { lineStyle: { color: COLORS.axisLine } },
        axisTick: { show: false },
        axisLabel: { color: COLORS.axis, fontSize: 10 },
        splitLine: { show: false },
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: COLORS.axis, fontSize: 10 },
        splitLine: { lineStyle: { color: COLORS.grid } },
        nameTextStyle: { color: COLORS.legend, fontSize: 10 },
      },
      line: { symbol: "none", smooth: true, lineStyle: { width: 1.4 } },
      bar: { itemStyle: { barMaxWidth: 48 } },
    };
  }

  function register(echarts) {
    if (!echarts || registered) return;
    try {
      echarts.registerTheme(NAME, buildTheme());
      registered = true;
    } catch (e) {
      /* 主题注册失败不该挡住图表渲染 */
    }
  }

  global.SRChartTheme = {
    NAME: NAME,
    PALETTE: PALETTE,
    colors: COLORS,
    register: register,
  };
})(window);
