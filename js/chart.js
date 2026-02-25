/* chart.js — Chart.js diminishing returns curve */

const ChartModule = {
  charts: {},

  init(canvasId) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return this;

    const chart = new Chart(ctx, {
      type: 'line',
      data: {
        datasets: [
          {
            label: 'Population',
            data: [],
            borderColor: '#f59e0b',
            backgroundColor: 'rgba(245, 158, 11, 0.1)',
            fill: true,
            tension: 0.3,
            pointRadius: 0,
            pointHitRadius: 10,
            borderWidth: 2,
          },
          {
            label: 'With supermarkets',
            data: [],
            borderColor: '#16a34a',
            backgroundColor: 'rgba(22, 163, 74, 0.08)',
            fill: false,
            tension: 0.3,
            pointRadius: 0,
            pointHitRadius: 10,
            borderWidth: 2,
            borderDash: [4, 3],
            hidden: true,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        interaction: {
          mode: 'index',
          intersect: false,
          axis: 'x',
        },
        plugins: {
          legend: {
            display: true,
            position: 'bottom',
            labels: {
              color: '#9ca3af',
              font: { family: "'DM Sans'", size: 11 },
              boxWidth: 20,
              padding: 10,
              usePointStyle: true,
              pointStyleWidth: 16,
            },
          },
          tooltip: {
            backgroundColor: '#1a1a2e',
            titleColor: '#e0e0e0',
            bodyColor: '#e0e0e0',
            borderColor: '#0f3460',
            borderWidth: 1,
            callbacks: {
              title: function(items) {
                return items[0].parsed.x.toLocaleString() + ' additional lockers';
              },
              label: function(ctx) {
                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '% coverage';
              },
            },
          },
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Additional Lockers', color: '#9ca3af', font: { family: "'DM Sans'" } },
            ticks: { color: '#9ca3af', font: { family: "'DM Sans'" } },
            grid: { color: 'rgba(255,255,255,0.05)' },
          },
          y: {
            title: { display: true, text: 'Coverage %', color: '#9ca3af', font: { family: "'DM Sans'" } },
            ticks: { color: '#9ca3af', font: { family: "'DM Sans'" } },
            grid: { color: 'rgba(255,255,255,0.05)' },
            max: 100,
          },
        },
      },
    });

    this.charts[canvasId] = chart;
    return this;
  },

  _downsample(curve) {
    if (curve.length <= 500) return curve;
    var step = Math.ceil(curve.length / 500);
    return curve.filter(function(_, i) {
      return i % step === 0 || i === curve.length - 1;
    });
  },

  updateChart(canvasId, activeCurve, smCurve, isDemand) {
    if (!this.charts[canvasId]) {
      this.init(canvasId);
    }
    const chart = this.charts[canvasId];
    if (!chart) return;

    // Style the primary curve based on mode
    if (isDemand) {
      chart.data.datasets[0].label = 'Demand-weighted';
      chart.data.datasets[0].borderColor = '#38bdf8';
      chart.data.datasets[0].backgroundColor = 'rgba(56, 189, 248, 0.1)';
    } else {
      chart.data.datasets[0].label = 'Population';
      chart.data.datasets[0].borderColor = '#f59e0b';
      chart.data.datasets[0].backgroundColor = 'rgba(245, 158, 11, 0.1)';
    }

    var data = this._downsample(activeCurve);
    chart.data.datasets[0].data = data.map(function(d) {
      return { x: d.lockers, y: d.coverage };
    });

    // SM top-up curve: continues from where optimal ends
    if (smCurve && smCurve.length > 0) {
      var smData = this._downsample(smCurve);
      chart.data.datasets[1].data = smData.map(function(d) {
        return { x: d.lockers, y: d.coverage };
      });
      chart.data.datasets[1].hidden = false;
    } else {
      chart.data.datasets[1].data = [];
      chart.data.datasets[1].hidden = true;
    }

    chart.update();
  },
};
