// 電力予報画面の料金グラフ（Chart.js）
(() => {
  const data = JSON.parse(document.getElementById("forecast-data").textContent);
  const canvas = document.getElementById("forecast-chart");

  // OFF予定のコマを背景の網掛けで、現在時刻を縦線で描画する
  const overlays = {
    id: "forecastOverlays",
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      const step = scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0);
      ctx.save();
      ctx.fillStyle = "rgba(16, 185, 129, 0.18)";
      data.off.forEach((off, i) => {
        if (!off) return;
        const x = scales.x.getPixelForValue(i) - step / 2;
        ctx.fillRect(x, chartArea.top, step, chartArea.bottom - chartArea.top);
      });
      ctx.restore();
    },
    afterDatasetsDraw(chart) {
      if (data.nowIndex === null) return;
      const { ctx, chartArea, scales } = chart;
      const step = scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0);
      const x = scales.x.getPixelForValue(0) - step / 2 + data.nowIndex * step;
      ctx.save();
      ctx.strokeStyle = "#fbbf24";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  new Chart(canvas, {
    type: "line",
    data: {
      labels: data.labels,
      datasets: [
        {
          label: "電源料金単価（円/kWh）",
          data: data.prices,
          borderColor: "#38bdf8",
          backgroundColor: "rgba(56, 189, 248, 0.15)",
          stepped: "middle",
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) =>
              `${item.parsed.y.toFixed(2)} 円/kWh（${data.off[item.dataIndex] ? "OFF予定" : "ON"}）`,
          },
        },
      },
      scales: {
        x: {
          offset: true,
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
          grid: { color: "rgba(148, 163, 184, 0.08)" },
        },
        y: {
          beginAtZero: true,
          ticks: { color: "#94a3b8" },
          grid: { color: "rgba(148, 163, 184, 0.12)" },
          title: { display: true, text: "円/kWh", color: "#94a3b8" },
        },
      },
    },
    plugins: [overlays],
  });
})();
