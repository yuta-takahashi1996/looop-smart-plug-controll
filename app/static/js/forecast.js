// 電力予報画面の料金グラフ（Chart.js）。請求単価と電源料金単価を表示する
(() => {
  const data = JSON.parse(document.getElementById("forecast-data").textContent);
  const canvas = document.getElementById("forecast-chart");

  // 現在時刻を縦線で描画する
  const overlays = {
    id: "forecastOverlays",
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
          label: "請求単価",
          data: data.billing,
          borderColor: "#38bdf8",
          backgroundColor: "rgba(56, 189, 248, 0.15)",
          stepped: "middle",
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
        },
        {
          label: "電源料金単価",
          data: data.energy,
          borderColor: "#94a3b8",
          borderDash: [4, 4],
          borderWidth: 1.5,
          stepped: "middle",
          pointRadius: 0,
          pointHoverRadius: 3,
          fill: false,
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
            label: (item) => `${item.dataset.label}：${item.parsed.y.toFixed(2)} 円/kWh`,
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
