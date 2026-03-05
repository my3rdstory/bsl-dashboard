document.addEventListener("DOMContentLoaded", function () {
  if (typeof window.Chart === "undefined") {
    return;
  }

  function parseCsvFromCanvas(canvas, attrName) {
    if (!(canvas instanceof HTMLCanvasElement)) {
      return [];
    }
    var raw = String(canvas.getAttribute(attrName) || "").trim();
    if (!raw) {
      return [];
    }
    try {
      if (raw.indexOf(",") === -1) {
        return [raw];
      }
      return raw.split(",").map(function (value) {
        return String(value || "").trim();
      });
    } catch (_error) {
      return [];
    }
  }

  function normalizeNumbers(values) {
    return values.map(function (value) {
      var num = Number(value);
      return Number.isFinite(num) ? num : 0;
    });
  }

  function formatKoreanNumber(value) {
    return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function buildScaleOptions() {
    return {
      maintainAspectRatio: false,
      responsive: true,
      interaction: {
        mode: "index",
        intersect: false,
      },
      plugins: {
        legend: {
          display: false,
        },
        tooltip: {
          enabled: true,
          callbacks: {
            label: function (context) {
              var label = context.dataset.label || "";
              var value = Number(context.parsed.y !== undefined ? context.parsed.y : context.parsed);
              if (!Number.isFinite(value)) {
                value = 0;
              }
              return `${label} ${formatKoreanNumber(value)}${context.dataset.unit || ""}`;
            },
          },
        },
      },
      scales: {
        x: {
          grid: {
            color: "rgba(255,255,255,0.08)",
          },
          ticks: {
            color: "#9ba8b4",
            maxRotation: 0,
            minRotation: 0,
          },
        },
        y: {
          beginAtZero: true,
          grid: {
            color: "rgba(255,255,255,0.08)",
          },
          ticks: {
            color: "#9ba8b4",
            callback: function (value) {
              return formatKoreanNumber(value);
            },
          },
        },
      },
    };
  }

  function drawDuesBarChart() {
    var canvas = document.getElementById("home-dues-chart");
    if (!(canvas instanceof HTMLCanvasElement)) {
      return;
    }
    var labels = parseCsvFromCanvas(canvas, "data-chart-labels");
    var values = normalizeNumbers(
      parseCsvFromCanvas(canvas, "data-chart-values")
    );

    var ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }

    new window.Chart(ctx, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          {
            label: "월별 회비 총량",
            data: values,
            unit: " sats",
            backgroundColor: "rgba(96, 165, 250, 0.33)",
            borderColor: "#61a5fa",
            borderWidth: 1,
            borderRadius: 4,
            borderSkipped: false,
          },
        ],
      },
      options: buildScaleOptions(),
    });
  }

  function drawMemberLineChart() {
    var canvas = document.getElementById("home-member-growth-chart");
    if (!(canvas instanceof HTMLCanvasElement)) {
      return;
    }
    var labels = parseCsvFromCanvas(canvas, "data-chart-labels");
    var values = normalizeNumbers(
      parseCsvFromCanvas(canvas, "data-chart-values")
    );

    var ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }

    new window.Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "회원총수",
            data: values,
            unit: "명",
            borderColor: "#7bd4ff",
            borderWidth: 2,
            fill: false,
            tension: 0.38,
            pointBackgroundColor: "#d8f2ff",
            pointBorderColor: "#a4dcff",
            pointRadius: 2.6,
            pointHoverRadius: 4,
          },
        ],
      },
      options: buildScaleOptions(),
    });
  }

  drawDuesBarChart();
  drawMemberLineChart();
});
