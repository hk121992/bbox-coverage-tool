/* ui.js — Sidebar controls, stats, planner */

const UIModule = {
  _debounceTimer: null,

  init() {
    this.setupSidebarToggle();
    this.setupModeToggle();
    this.setupTimeSlider();
    this.setupPlannerPanel();
  },

  // --- Sidebar Toggle ---
  setupSidebarToggle() {
    var sidebar = document.getElementById('sidebar');
    var toggleBtn = document.getElementById('sidebar-toggle');
    var expandBtn = document.getElementById('sidebar-expand');

    toggleBtn.addEventListener('click', function() {
      sidebar.classList.add('collapsed');
      expandBtn.style.display = 'block';
      setTimeout(function() { MapModule.map.invalidateSize(); }, 300);
    });

    expandBtn.addEventListener('click', function() {
      sidebar.classList.remove('collapsed');
      expandBtn.style.display = 'none';
      setTimeout(function() { MapModule.map.invalidateSize(); }, 300);
    });
  },

  // --- Coverage Mode Toggle ---
  setupModeToggle() {
    var self = this;
    var btns = document.querySelectorAll('.mode-btn');
    var warning = document.getElementById('demand-unavailable');

    btns.forEach(function(btn) {
      btn.addEventListener('click', function() {
        var mode = btn.getAttribute('data-mode');

        if (mode === 'demand') {
          // Check demand data is available in centroids
          var hasDemand = App.data.centroids && App.data.centroids[0] && App.data.centroids[0].demand != null;
          if (!hasDemand) {
            warning.style.display = 'block';
            return;
          }
          // Check precomputed demand placements are available
          if (!App.precomputedDemand) {
            warning.style.display = 'block';
            return;
          }
        }

        warning.style.display = 'none';
        App.state.coverageMode = mode;

        btns.forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');

        // Recompute in the selected mode and refresh map + planner
        var results = computeCoverage(App.data.bbox, App.data.centroids, App.state.travelMinutes);
        App.state.coverageResults = results;
        MapModule.updateSectorColors(results);
        self.updateStats(results);
        self.syncTargetSliderMin();
        self.updatePlannerResults();
      });
    });
  },

  // --- Travel Time Slider (drives everything) ---
  setupTimeSlider() {
    var slider = document.getElementById('time-slider');
    var valueLabel = document.getElementById('time-value');
    var self = this;

    slider.addEventListener('input', function() {
      var val = parseInt(slider.value);
      valueLabel.textContent = val + ' min';

      clearTimeout(self._debounceTimer);
      self._debounceTimer = setTimeout(function() {
        App.state.travelMinutes = val;
        App.state.plannerTravelMinutes = val;

        // Recompute current coverage
        var results = computeCoverage(App.data.bbox, App.data.centroids, val);
        App.state.coverageResults = results;
        MapModule.updateSectorColors(results);
        self.updateStats(results);

        // Update planner target slider min to current coverage
        self.syncTargetSliderMin();

        // Update planner results
        self.updatePlannerResults();
      }, 300);
    });
  },

  // --- Sync planner target slider min to current coverage ---
  syncTargetSliderMin() {
    var results = App.state.coverageResults;
    if (!results) return;
    // min = exact current coverage for both sliders — neither can go below what already exists
    // Safe as attribute because current coverage is always < 99%, so min ≠ max
    var currentPct = parseFloat(results.coveragePercent.toFixed(2));
    var targetSlider = document.getElementById('planner-target-slider');
    var targetLabel = document.getElementById('planner-target-value');
    var smTargetSlider = document.getElementById('sm-target-slider');
    targetSlider.min = currentPct;
    smTargetSlider.min = currentPct;
    // If current target A is below the new min, snap it up
    var curTarget = parseFloat(targetSlider.value);
    if (curTarget < currentPct) {
      targetSlider.value = currentPct;
      var snapped = currentPct >= 97 ? 99 : Math.round(currentPct / 5) * 5;
      targetLabel.textContent = snapped + '%';
      App.state.plannerTargetCoverage = snapped;
    }
    // If current target B is below the new min, snap it up too
    var smTargetLabel = document.getElementById('sm-target-value');
    var curSm = parseFloat(smTargetSlider.value);
    if (curSm < currentPct) {
      smTargetSlider.value = currentPct;
      var snappedSm = currentPct >= 97 ? 99 : Math.round(currentPct / 5) * 5;
      smTargetLabel.textContent = snappedSm + '%';
      App.state.smTargetCoverage = snappedSm;
    }
  },

  // --- Stats Dashboard ---
  updateStats(results) {
    // Population coverage (always compute from population mode)
    var popResults = App.state.coverageMode === 'demand'
      ? computeCoverage(App.data.bbox, App.data.centroids, App.state.travelMinutes, 'population')
      : results;

    document.getElementById('stat-pop-pct').textContent = popResults.coveragePercent.toFixed(1) + '%';
    document.getElementById('stat-pop-sub').textContent =
      popResults.coveredPop.toLocaleString() + ' / ' + popResults.totalPop.toLocaleString();
    document.getElementById('stat-uncovered-pop').textContent =
      (popResults.totalPop - popResults.coveredPop).toLocaleString();
    document.getElementById('stat-uncovered-pct').textContent =
      (100 - popResults.coveragePercent).toFixed(1) + '%';

    // Demand coverage (always compute if data available)
    var hasDemand = App.data.centroids && App.data.centroids[0] && App.data.centroids[0].demand != null;
    if (hasDemand) {
      var demResults = App.state.coverageMode === 'demand'
        ? results
        : computeCoverage(App.data.bbox, App.data.centroids, App.state.travelMinutes, 'demand');
      document.getElementById('stat-dem-pct').textContent = demResults.coveragePercent.toFixed(1) + '%';
      document.getElementById('stat-dem-uncovered').textContent =
        (100 - demResults.coveragePercent).toFixed(1) + '%';
      document.getElementById('stat-dem-uncovered-pct').textContent = 'uncovered';
    } else {
      document.getElementById('stat-dem-pct').textContent = 'N/A';
      document.getElementById('stat-dem-uncovered').textContent = '--';
      document.getElementById('stat-dem-uncovered-pct').textContent = '--';
    }

    document.getElementById('stat-locker-count').textContent =
      App.data.bbox ? App.data.bbox.length.toLocaleString() : '--';

    var regionNames = { '02000': 'flanders', '03000': 'wallonia', '04000': 'brussels' };
    for (var rgn in regionNames) {
      var name = regionNames[rgn];
      var data = popResults.byRegion[rgn];
      if (data) {
        var pct = data.total > 0 ? (data.covered / data.total * 100).toFixed(1) : '0.0';
        document.getElementById('stat-' + name).textContent =
          pct + '% (' + data.covered.toLocaleString() + ' / ' + data.total.toLocaleString() + ')';
      }
    }
  },

  // --- Planner Panel ---
  setupPlannerPanel() {
    var targetSlider = document.getElementById('planner-target-slider');
    var targetLabel = document.getElementById('planner-target-value');
    var smCheckbox = document.getElementById('use-supermarkets');
    var smTargetGroup = document.getElementById('supermarket-target-group');
    var smTargetSlider = document.getElementById('sm-target-slider');
    var smTargetLabel = document.getElementById('sm-target-value');
    var smBreakdown = document.getElementById('plan-sm-breakdown');
    var legendSm = document.getElementById('legend-sm');
    var exportBtn = document.getElementById('export-csv');
    var self = this;

    targetSlider.addEventListener('input', function() {
      var raw = parseFloat(targetSlider.value);
      var minCov = parseFloat(targetSlider.min); // exact current coverage
      // Snap: bottom stop = current coverage, top stop = 99, else nearest 5%
      var val;
      if (raw <= minCov + 2.5) {
        val = minCov; // snap to exact current coverage at bottom
      } else if (raw >= 97) {
        val = 99;
      } else {
        val = Math.round(raw / 5) * 5;
      }
      targetSlider.value = val;
      // Label: show rounded value (current coverage shown as-is if at min)
      var label = (val === minCov) ? minCov.toFixed(1) + '%' : val + '%';
      targetLabel.textContent = label;
      App.state.plannerTargetCoverage = val;

      // SM target must be >= A — clamp by value, not min attribute
      if (App.state.useSupermarkets) {
        var smVal = parseFloat(smTargetSlider.value);
        if (smVal < val) {
          smTargetSlider.value = val;
          smTargetLabel.textContent = (val === minCov) ? minCov.toFixed(1) + '%' : val + '%';
          App.state.smTargetCoverage = val;
        }
      }
      self.updatePlannerResults();
    });

    smCheckbox.addEventListener('change', function() {
      App.state.useSupermarkets = smCheckbox.checked;
      smTargetGroup.style.display = smCheckbox.checked ? 'block' : 'none';
      smBreakdown.style.display = smCheckbox.checked ? '' : 'none';
      legendSm.style.display = smCheckbox.checked ? '' : 'none';

      if (smCheckbox.checked) {
        var optTarget = parseInt(targetSlider.value);
        // SM target must be >= optTarget (B can equal A)
        // Don't touch smTargetSlider.min — enforce via value clamping only
        var curSm = parseInt(smTargetSlider.value);
        if (curSm < optTarget) {
          smTargetSlider.value = optTarget;
          smTargetLabel.textContent = optTarget + '%';
          App.state.smTargetCoverage = optTarget;
        }
        // Show supermarket markers on map
        if (!MapModule.layers.supermarketLayer) {
          MapModule.addSupermarketMarkers(App.data.supermarkets);
        }
        MapModule.toggleSupermarkets(true);
      } else {
        MapModule.toggleSupermarkets(false);
        MapModule.clearSupermarketPlacements();
      }

      self.updatePlannerResults();
    });

    smTargetSlider.addEventListener('input', function() {
      var raw = parseFloat(smTargetSlider.value);
      var minB = App.state.plannerTargetCoverage; // A is the effective minimum for B
      // Snap: bottom stop = A (exact), top stop = 99, else nearest 5%
      var val;
      if (raw <= minB + 2.5) {
        val = minB; // snap to exact A at bottom
      } else if (raw >= 97) {
        val = 99;
      } else {
        val = Math.round(raw / 5) * 5;
      }
      // Hard clamp: never below A
      if (val < minB) val = minB;
      smTargetSlider.value = val;
      var label = (val % 5 !== 0) ? val.toFixed(1) + '%' : val + '%';
      smTargetLabel.textContent = label;
      App.state.smTargetCoverage = val;
      self.updatePlannerResults();
    });

    exportBtn.addEventListener('click', function() {
      UIModule.exportCSV();
    });
  },

  // --- Planner: instant results update ---
  updatePlannerResults() {
    var source = App.state.coverageMode === 'demand' ? App.precomputedDemand : App.precomputed;
    if (!source) return;
    // If no precomputed data for this travel time in this mode, clear and bail
    if (!source[String(App.state.plannerTravelMinutes)]) {
      document.getElementById('plan-total-cov').textContent = 'N/A';
      document.getElementById('plan-optimal-cov').textContent = 'N/A';
      document.getElementById('plan-additional').textContent = '--';
      document.getElementById('plan-total-lockers').textContent = '--';
      MapModule.clearProposed();
      return;
    }

    // Phase 1: optimal placements (instant from precomputed data)
    var optResult = getPlacementsForState();
    var optPlacements = optResult.placements;

    document.getElementById('plan-optimal-cov').textContent = optResult.finalCoverage.toFixed(1) + '%';

    // Show optimal markers on map
    MapModule.showProposedLockers(optPlacements);

    var totalPlacements = optPlacements.length;
    var combinedCurve = optResult.coverageCurve.slice(); // copy to avoid mutating
    var smPlacements = [];
    var finalCoverage = optResult.finalCoverage;

    // Phase 2: supermarket top-up (precomputed)
    if (App.state.useSupermarkets) {
      var smResult = getSmPlacementsForState();
      smPlacements = smResult.placements;
      totalPlacements += smPlacements.length;
      finalCoverage = smResult.finalCoverage;

      document.getElementById('plan-sm-cov').textContent = smResult.finalCoverage.toFixed(1) + '%';

      // Show redundant optimal placements count if any
      var redundantEl = document.getElementById('plan-redundant-count');
      if (redundantEl) {
        var rCount = smResult.redundant ? smResult.redundant.length : 0;
        redundantEl.style.display = rCount > 0 ? '' : 'none';
        redundantEl.textContent = rCount + ' optimal placement' + (rCount !== 1 ? 's' : '') +
          ' could be replaced by supermarkets';
      }

      // Combine coverage curves (SM curve continues from opt endpoint)
      var smCurve = smResult.coverageCurve;
      for (var i = 1; i < smCurve.length; i++) {
        combinedCurve.push({
          lockers: optPlacements.length + smCurve[i].lockers,
          coverage: smCurve[i].coverage,
        });
      }

      MapModule.showSupermarketPlacements(smPlacements);
    } else {
      MapModule.clearSupermarketPlacements();
      var redundantEl = document.getElementById('plan-redundant-count');
      if (redundantEl) redundantEl.style.display = 'none';
    }

    var existingCount = App.data.bbox ? App.data.bbox.length : 0;
    document.getElementById('plan-total-cov').textContent = finalCoverage.toFixed(1) + '%';
    document.getElementById('plan-additional').textContent = totalPlacements.toLocaleString();
    document.getElementById('plan-total-lockers').textContent = (existingCount + totalPlacements).toLocaleString();

    // Build chart curve for the active mode only (x = additional lockers, 0-based)
    var useDemandMode = App.state.coverageMode === 'demand';
    var activeSource = useDemandMode
      ? (App.precomputedDemand ? App.precomputedDemand[String(App.state.plannerTravelMinutes)] : null)
      : (App.precomputed ? App.precomputed[String(App.state.plannerTravelMinutes)] : null);

    var activeCurve = [];
    if (activeSource) {
      activeCurve.push({ lockers: 0, coverage: activeSource.startCoverage });
      for (var ci = 0; ci < activeSource.placements.length; ci++) {
        activeCurve.push({ lockers: ci + 1, coverage: activeSource.placements[ci].cum });
      }
    }

    // Build SM curve for chart: starts at the end of the optimal curve, continues with SM top-up
    var smChartCurve = null;
    if (App.state.useSupermarkets && smPlacements.length > 0) {
      var optCount = optPlacements.length;
      smChartCurve = [];
      // First point = where optimal ended (joins the two curves visually)
      smChartCurve.push({ lockers: optCount, coverage: optResult.finalCoverage });
      // Subsequent points = SM placements continuing from opt count
      for (var si = 0; si < smPlacements.length; si++) {
        smChartCurve.push({ lockers: optCount + si + 1, coverage: smPlacements[si].cum });
      }
    }

    // Update chart — primary curve + optional SM curve
    ChartModule.updateChart('planner-chart', activeCurve, smChartCurve, useDemandMode);

    // Update table
    this.updatePlannerTable(optPlacements, smPlacements);

    // Store for CSV export
    this._lastOptPlacements = optPlacements;
    this._lastSmPlacements = smPlacements;
  },

  updatePlannerTable(optPlacements, smPlacements) {
    var tbody = document.querySelector('#planner-table tbody');
    tbody.innerHTML = '';

    var topOpt = optPlacements.slice(0, 50);
    for (var i = 0; i < topOpt.length; i++) {
      var p = topOpt[i];
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + (i + 1) + '</td>' +
        '<td>' + (p.sc || '') + '</td>' +
        '<td>' + p.gain.toLocaleString() + '</td>' +
        '<td>' + p.cum.toFixed(1) + '%</td>';

      (function(lat, lng) {
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', function() {
          MapModule.map.setView([lat, lng], 14);
          MapModule.highlightLocation(lat, lng);
        });
      })(p.lat, p.lng);

      tbody.appendChild(tr);
    }

    if (smPlacements && smPlacements.length > 0) {
      var divider = document.createElement('tr');
      divider.innerHTML = '<td colspan="4" style="background:var(--sidebar-border);color:var(--text-muted);font-size:10px;text-transform:uppercase;letter-spacing:0.5px;padding:6px 8px;font-weight:600">Supermarket Top-Up</td>';
      tbody.appendChild(divider);

      var topSm = smPlacements.slice(0, 50);
      for (var i = 0; i < topSm.length; i++) {
        var s = topSm[i];
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td>' + (i + 1) + '</td>' +
          '<td>' + (s.name || 'Unknown') + '</td>' +
          '<td>' + s.gain.toLocaleString() + '</td>' +
          '<td>' + s.cum.toFixed(1) + '%</td>';

        (function(lat, lng) {
          tr.style.cursor = 'pointer';
          tr.addEventListener('click', function() {
            MapModule.map.setView([lat, lng], 14);
            MapModule.highlightLocation(lat, lng);
          });
        })(s.lat, s.lng);

        tbody.appendChild(tr);
      }
    }
  },

  exportCSV() {
    var optPlacements = this._lastOptPlacements || [];
    var smPlacements = this._lastSmPlacements || [];

    if (!optPlacements.length && !smPlacements.length) return;

    var csv = 'rank,type,location,latitude,longitude,marginal_pop_gain,cumulative_coverage_pct\n';

    optPlacements.forEach(function(loc, i) {
      csv += (i + 1) + ',optimal,' + loc.sc + ',' + loc.lat + ',' + loc.lng + ',' +
        loc.gain + ',' + loc.cum + '\n';
    });

    smPlacements.forEach(function(loc, i) {
      csv += (i + 1) + ',supermarket,"' + (loc.name || '') + '",' + loc.lat + ',' + loc.lng + ',' +
        loc.gain + ',' + loc.cum + '\n';
    });

    var blob = new Blob([csv], { type: 'text/csv' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'proposed_bbox_locations.csv';
    a.click();
    URL.revokeObjectURL(url);
  },
};
