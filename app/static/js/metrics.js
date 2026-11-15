// メトリクス画面（docs/18-host-metrics.md）。タスクマネージャーのように直近の値をグラフで表示する。
// 値は画面のメモリにだけ持ち、表示範囲（直近60秒）より古いものは捨てる。
(() => {
  const root = document.getElementById("metrics");
  const url = root.dataset.url;
  const interval = Number(root.dataset.interval) * 1000;
  const history = Number(root.dataset.history);

  const COLORS = {
    cpu: "#38bdf8",
    memory: "#a78bfa",
    disk: "#34d399",
    diskWrite: "#f472b6",
    network: "#fbbf24",
    networkTx: "#60a5fa",
    temperature: "#fb7185",
  };
  const TITLES = { cpu: "CPU", memory: "メモリ", disk: "ストレージ", network: "ネットワーク", temperature: "CPU 温度" };

  let samples = [];
  let selected = "cpu";
  let timer = null;

  // --- 表示の書式 ---
  const percent = (v) => `${v.toFixed(1)}%`;
  const bytes = (v) => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  };
  const rate = (v) => `${bytes(v)}/s`;
  // ネットワークはビットレート（1000 単位。値はバイト／秒で受け取る）
  const bitrate = (v) => {
    const units = ["bps", "Kbps", "Mbps", "Gbps"];
    let bits = v * 8;
    let i = 0;
    while (bits >= 1000 && i < units.length - 1) {
      bits /= 1000;
      i += 1;
    }
    return `${bits.toFixed(bits >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  };
  const celsius = (v) => (v === null ? "—" : `${v.toFixed(1)}℃`);

  // --- 項目ごとの系列（main：詳細のグラフ、spark：左の小さいグラフ） ---
  const SERIES = {
    cpu: { lines: [["使用率", (s) => s.cpu_percent, COLORS.cpu]], max: 100, format: percent },
    memory: { lines: [["使用率", (s) => s.memory_percent, COLORS.memory]], max: 100, format: percent },
    disk: {
      lines: [
        ["読み取り", (s) => s.disk_read_bps, COLORS.disk],
        ["書き込み", (s) => s.disk_write_bps, COLORS.diskWrite],
      ],
      format: rate,
    },
    network: {
      lines: [
        ["受信", (s) => s.net_rx_bps, COLORS.network],
        ["送信", (s) => s.net_tx_bps, COLORS.networkTx],
      ],
      format: bitrate,
    },
    temperature: { lines: [["温度", (s) => s.temperature_c, COLORS.temperature]], format: celsius, suggestedMin: 30, suggestedMax: 80 },
  };

  const points = (getter) => {
    const last = samples.length ? samples[samples.length - 1].at : 0;
    return samples.map((s) => ({ x: s.at - last, y: getter(s) }));
  };

  const xAxis = (display) => ({
    type: "linear",
    min: -history,
    max: 0,
    display,
    grid: { color: "rgba(148, 163, 184, 0.12)" },
    ticks: { color: "#94a3b8", stepSize: 10, callback: (v) => (v === 0 ? "現在" : `${-v}秒前`) },
  });

  const chart = (canvas, datasets, options = {}) =>
    new Chart(canvas, {
      type: "line",
      data: { datasets },
      options: {
        animation: false,
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        elements: { point: { radius: 0 }, line: { borderWidth: 1.5, tension: 0.2 } },
        ...options,
      },
    });

  const dataset = (label, color, fill) => ({
    label,
    data: [],
    borderColor: color,
    backgroundColor: `${color}33`,
    fill,
  });

  // 左の小さいグラフ（先頭の系列だけ）
  const sparks = {};
  root.querySelectorAll("[data-spark]").forEach((canvas) => {
    const key = canvas.dataset.spark;
    const [label, , color] = SERIES[key].lines[0];
    sparks[key] = chart(canvas, [dataset(label, color, true)], {
      scales: { x: xAxis(false), y: { display: false, min: SERIES[key].suggestedMin === undefined ? 0 : undefined, max: SERIES[key].max, suggestedMin: SERIES[key].suggestedMin, suggestedMax: SERIES[key].suggestedMax } },
    });
  });

  // 詳細のグラフ
  const main = chart(document.getElementById("metrics-main"), [], {
    plugins: { legend: { display: true, labels: { color: "#cbd5e1", boxWidth: 12 } }, tooltip: { enabled: false } },
    scales: {
      x: xAxis(true),
      y: { min: 0, grid: { color: "rgba(148, 163, 184, 0.12)" }, ticks: { color: "#94a3b8" } },
    },
  });

  // コアごとのグラフ（コア数は最初の値で決まる）
  const coreCharts = [];
  const ensureCores = (count) => {
    const container = root.querySelector("[data-cores]");
    while (coreCharts.length < count) {
      const index = coreCharts.length;
      const box = document.createElement("div");
      box.className = "rounded-lg border border-slate-800 p-2";
      box.innerHTML = `<div class="flex justify-between text-xs text-slate-400"><span>コア ${index}</span><span class="tabular-nums" data-core-value></span></div><div class="relative h-16"><canvas></canvas></div>`;
      container.appendChild(box);
      coreCharts.push({
        value: box.querySelector("[data-core-value]"),
        chart: chart(box.querySelector("canvas"), [dataset(`コア ${index}`, COLORS.cpu, true)], {
          scales: { x: xAxis(false), y: { display: false, min: 0, max: 100 } },
        }),
      });
    }
  };

  const stat = (label, value) => `<div><dt class="text-xs text-slate-400">${label}</dt><dd class="mt-1 font-semibold tabular-nums">${value}</dd></div>`;

  const renderDetail = (latest) => {
    const series = SERIES[selected];
    root.querySelector("[data-detail-title]").textContent = TITLES[selected];
    root.querySelectorAll("[data-panel]").forEach((el) => el.classList.toggle("hidden", el.dataset.panel !== selected));

    if (main.$key !== selected) {
      main.data.datasets = series.lines.map(([label, , color]) => dataset(label, color, series.lines.length === 1));
      main.options.scales.y.min = series.suggestedMin === undefined ? 0 : undefined; // 温度は 0 からにしない
      main.options.scales.y.max = series.max;
      main.options.scales.y.suggestedMin = series.suggestedMin;
      main.options.scales.y.suggestedMax = series.suggestedMax;
      main.options.scales.y.ticks.callback = (v) => series.format(v);
      main.$key = selected;
    }
    series.lines.forEach(([, getter], i) => {
      main.data.datasets[i].data = points(getter);
    });
    main.update();

    const value = root.querySelector("[data-detail-value]");
    const caption = root.querySelector("[data-detail-caption]");
    const stats = root.querySelector("[data-stats]");
    if (!latest) {
      value.textContent = "—";
      caption.textContent = "";
      stats.innerHTML = "";
      return;
    }
    if (selected === "cpu") {
      value.textContent = percent(latest.cpu_percent);
      caption.textContent = `全体の使用率（${latest.cpu_cores.length} コア）`;
      stats.innerHTML = stat("使用率", percent(latest.cpu_percent)) + stat("CPU 温度", celsius(latest.temperature_c));
    } else if (selected === "memory") {
      value.textContent = percent(latest.memory_percent);
      caption.textContent = "使用中のメモリ（キャッシュなど解放できる分を除く）";
      stats.innerHTML =
        stat("使用中", bytes(latest.memory_used)) +
        stat("利用可能", bytes(latest.memory_total - latest.memory_used)) +
        stat("合計", bytes(latest.memory_total));
    } else if (selected === "disk") {
      value.textContent = `${rate(latest.disk_read_bps + latest.disk_write_bps)}`;
      caption.textContent = "読み取り・書き込みの速度（物理ディスクの合計）";
      stats.innerHTML = stat("読み取り", rate(latest.disk_read_bps)) + stat("書き込み", rate(latest.disk_write_bps));
    } else if (selected === "network") {
      value.textContent = bitrate(latest.net_rx_bps + latest.net_tx_bps);
      caption.textContent = "受信・送信の速度（仮想のインターフェースを除く）";
      stats.innerHTML = stat("受信", bitrate(latest.net_rx_bps)) + stat("送信", bitrate(latest.net_tx_bps));
    } else {
      value.textContent = celsius(latest.temperature_c);
      caption.textContent =
        latest.temperature_c === null ? "この環境では温度を取得できません" : "SoC の温度（Raspberry Pi 5 は 80℃ を超えると性能を落とします）";
      stats.innerHTML = stat("現在", celsius(latest.temperature_c));
    }
  };

  const render = () => {
    const latest = samples.length ? samples[samples.length - 1] : null;
    root.querySelector("[data-empty]").classList.toggle("hidden", Boolean(latest));

    Object.entries(sparks).forEach(([key, spark]) => {
      spark.data.datasets[0].data = points(SERIES[key].lines[0][1]);
      spark.update();
    });
    const summary = (key, text) => {
      root.querySelector(`[data-summary="${key}"]`).textContent = latest ? text : "—";
    };
    if (latest) {
      summary("cpu", percent(latest.cpu_percent));
      summary("memory", `${percent(latest.memory_percent)}（${bytes(latest.memory_used)} / ${bytes(latest.memory_total)}）`);
      summary("disk", `R ${rate(latest.disk_read_bps)} W ${rate(latest.disk_write_bps)}`);
      summary("network", `↓ ${bitrate(latest.net_rx_bps)} ↑ ${bitrate(latest.net_tx_bps)}`);
      summary("temperature", latest.temperature_c === null ? "取得できません" : celsius(latest.temperature_c));

      ensureCores(latest.cpu_cores.length);
      coreCharts.forEach((core, i) => {
        core.chart.data.datasets[0].data = points((s) => s.cpu_cores[i] ?? null);
        core.chart.update();
        core.value.textContent = latest.cpu_cores[i] === undefined ? "—" : percent(latest.cpu_cores[i]);
      });

      root.querySelector("[data-storage-bar]").style.width = `${latest.storage_percent}%`;
      root.querySelector("[data-storage-text]").textContent =
        `${bytes(latest.storage_used)} / ${bytes(latest.storage_total)} 使用（${percent(latest.storage_percent)}、空き ${bytes(latest.storage_total - latest.storage_used)}）`;
    }
    renderDetail(latest);
  };

  const poll = async () => {
    const last = samples.length ? samples[samples.length - 1].at : null;
    try {
      const response = await fetch(last === null ? url : `${url}?since=${last}`, { headers: { Accept: "application/json" } });
      if (response.redirected || !response.ok) return; // ログアウトなど
      const body = await response.json();
      samples = samples.concat(body.samples);
      // 表示範囲より古い値は捨てる
      const newest = samples.length ? samples[samples.length - 1].at : 0;
      samples = samples.filter((s) => s.at > newest - history - interval / 1000);
      render();
    } catch (error) {
      console.warn("metrics: failed to fetch", error);
    }
  };

  const start = () => {
    if (timer !== null) return;
    poll();
    timer = setInterval(poll, interval);
  };
  const stop = () => {
    clearInterval(timer);
    timer = null;
  };
  // 画面が見えていない間は取得しない
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));

  root.querySelectorAll(".metric-tab").forEach((button) => {
    button.addEventListener("click", () => {
      selected = button.dataset.metric;
      root.querySelectorAll(".metric-tab").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      render();
    });
  });

  render();
  start();
})();
