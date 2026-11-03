// 電力予報画面の料金グラフ（Chart.js）。選択中の単価と、でんき予報の区分（背景色）を表示する
(() => {
  const data = JSON.parse(document.getElementById("forecast-data").textContent);
  const canvas = document.getElementById("forecast-chart");

  const LEVEL_COLORS = {
    GOOD: "rgba(16, 185, 129, 0.16)",
    ADVISORY: "rgba(251, 191, 36, 0.22)",
    WARNING: "rgba(244, 63, 94, 0.28)",
  };

  // でんき予報の区分を背景色で、現在時刻を縦線で描画する
  const overlays = {
    id: "forecastOverlays",
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      const step = scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0);
      ctx.save();
      data.levels.forEach((level, i) => {
        const color = LEVEL_COLORS[level];
        if (!color) return;
        ctx.fillStyle = color;
        ctx.fillRect(scales.x.getPixelForValue(i) - step / 2, chartArea.top, step, chartArea.bottom - chartArea.top);
      });
      ctx.restore();
    },
    afterDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      const step = scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0);
      // OFF 予定を上端の帯で表示（有効：塗りつぶし、無効：破線の枠）
      ctx.save();
      data.plan.forEach((state, i) => {
        if (!state) return;
        const x = scales.x.getPixelForValue(i) - step / 2;
        if (state === "ENABLED") {
          ctx.fillStyle = "#34d399";
          ctx.fillRect(x, chartArea.top, step, 8);
        } else {
          ctx.strokeStyle = "#94a3b8";
          ctx.setLineDash([3, 2]);
          ctx.strokeRect(x + 0.5, chartArea.top + 0.5, step - 1, 7);
        }
      });
      ctx.restore();
      if (data.nowIndex === null) return;
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
          label: data.label,
          data: data.prices,
          borderColor: "#38bdf8",
          backgroundColor: "rgba(56, 189, 248, 0.12)",
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
            label: (item) => `${item.dataset.label}：${item.parsed.y.toFixed(2)} 円/kWh`,
            afterLabel: (item) =>
              ({ GOOD: "でんき日和", ADVISORY: "でんき注意報", WARNING: "でんき警報" })[data.levels[item.dataIndex]] || "",
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
