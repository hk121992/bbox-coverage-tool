/* ui.js — Sidebar controls, stats, planner */

const UIModule = {
  _debounceTimer: null,

  init() {
    this.setupSidebarToggle();
    this.setupModeToggle();
    this.setupTimeSlider();
    this.setupPlannerPanel();
  },

  setupSidebarToggle() {
    var sidebar = document.getElementById('sidebar');
    var toggleBtn = document.getElementById('sidebar-toggle');
    var expandBtn = document.getElementById('sidebar-expand');

    function isMobile() { return window.innerWidth <= 600; }
    function invalidateMap() { setTimeout(function() { MapModule.map.invalidateSize(); }, 300); }

    function collapseSidebar() {
      if (isMobile()) { sidebar.classList.remove('mobile-open'); }
      else { sidebar.classList.add('collapsed'); }
      document.body.classList.add('sidebar-collapsed');
      expandBtn.innerHTML = '&#187;';
      invalidateMap();
    }

    function expandSidebar() {
      if (isMobile()) { sidebar.classList.add('mobile-open'); }
      else { sidebar.classList.remove('collapsed'); }
      document.body.classList.remove('sidebar-collapsed');
      expandBtn.innerHTML = '&#171;';
      invalidateMap();
    }

    // On mobile, sidebar is hidden by CSS transform — open it immediately on load
    if (isMobile()) { expandSidebar(); }

    expandBtn.addEventListener('click', function() {
      if (document.body.classList.contains('sidebar-collapsed')) { expandSidebar(); }
      else { collapseSidebar(); }
    });

    toggleBtn.addEventListener('click', collapseSidebar);
  },

  setupModeToggle() {
    var self = this;
    var btns = document.querySelectorAll('.mode-btn');
    var warning = document.getElementById('demand-unavailable');

    btns.forEach(function(btn) {
      btn.addEventListener('click', function() {
        var mode = btn.getAttribute('data-mode');

        if (mode === 'demand') {
          var hasDemand = App.data.centroids && App.data.centroids[0] && App.data.centroids[0].demand != null;
          if (!hasDemand || !App.precomputedDemand) {
            warning.style.display = 'block';
            return;
          }
        }

        warning.style.display = 'none';
        App.state.coverageMode = mode;
        btns.forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');

        var results = computeCoverage(App.data.bbox, App.data.centroids, App.state.travelMinutes);
        App.state.coverageResults = results;
        MapModule.updateSectorColors(results);
        self.updateStats(results);
        self.syncTargetSliderMin();
        self.updatePlannerResults();
      });
    });
  },

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

        var results = computeCoverage(App.data.bbox, App.data.centroids, val);
        App.state.coverageResults = results;
        MapModule.updateSectorColors(results);
        self.updateStats(results);
        self.syncTargetSliderMin();
        self.updatePlannerResults();
      }, 300);
    });
  },

  // Snap raw slider value: bottom stop = minVal, top stop = 99, else nearest 5%
  _snapSliderValue: function(raw, minVal) {
    if (raw <= minVal + 2.5) return minVal;
    if (raw >= 97) return 99;
    return Math.round(raw / 5) * 5;
  },

  syncTargetSliderMin() {
    var results = App.state.coverageResults;
    if (!results) return;

    // min = exact current coverage — safe as attribute because current < 99% always
    var currentPct = parseFloat(results.coveragePercent.toFixed(2));

    var sliders = [
      { slider: document.getElementById('planner-target-slider'), label: document.getElementById('planner-target-value'), stateKey: 'plannerTargetCoverage' },
      { slider: document.getElementById('sm-target-slider'),      label: document.getElementById('sm-target-value'),      stateKey: 'smTargetCoverage' },
    ];

    sliders.forEach(function(s) {
      s.slider.min = currentPct;
      if (parseFloat(s.slider.value) < currentPct) {
        s.slider.value = currentPct;
        var snapped = currentPct >= 97 ? 99 : Math.round(currentPct / 5) * 5;
        s.label.textContent = snapped + '%';
        App.state[s.stateKey] = snapped;
      }
    });
  },

  updateStats(results) {
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
      var data = popResults.byRegion[rgn];
      if (data) {
        var pct = data.total > 0 ? (data.covered / data.total * 100).toFixed(1) : '0.0';
        document.getElementById('stat-' + regionNames[rgn]).textContent =
          pct + '% (' + data.covered.toLocaleString() + ' / ' + data.total.toLocaleString() + ')';
      }
    }
  },

  setupPlannerPanel() {
    var targetSlider = document.getElementById('planner-target-slider');
    var targetLabel  = document.getElementById('planner-target-value');
    var smCheckbox   = document.getElementById('use-supermarkets');
    var smTargetGroup  = document.getElementById('supermarket-target-group');
    var smTargetSlider = document.getElementById('sm-target-slider');
    var smTargetLabel  = document.getElementById('sm-target-value');
    var smBreakdown  = document.getElementById('plan-sm-breakdown');
    var legendSm     = document.getElementById('legend-sm');
    var self = this;

    targetSlider.addEventListener('input', function() {
      var minCov = parseFloat(targetSlider.min);
      var val = self._snapSliderValue(parseFloat(targetSlider.value), minCov);
      targetSlider.value = val;
      targetLabel.textContent = (val === minCov) ? minCov.toFixed(1) + '%' : val + '%';
      App.state.plannerTargetCoverage = val;

      // B (SM target) must stay >= A
      if (App.state.useSupermarkets && parseFloat(smTargetSlider.value) < val) {
        smTargetSlider.value = val;
        smTargetLabel.textContent = (val === minCov) ? minCov.toFixed(1) + '%' : val + '%';
        App.state.smTargetCoverage = val;
      }
      self.updatePlannerResults();
    });

    smCheckbox.addEventListener('change', function() {
      App.state.useSupermarkets = smCheckbox.checked;
      smTargetGroup.style.display = smCheckbox.checked ? 'block' : 'none';
      smBreakdown.style.display   = smCheckbox.checked ? '' : 'none';
      legendSm.style.display      = smCheckbox.checked ? '' : 'none';

      if (smCheckbox.checked) {
        // B can equal A — enforce via value clamping, not min attribute
        var optTarget = parseInt(targetSlider.value);
        if (parseInt(smTargetSlider.value) < optTarget) {
          smTargetSlider.value = optTarget;
          smTargetLabel.textContent = optTarget + '%';
          App.state.smTargetCoverage = optTarget;
        }
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
      var minB = App.state.plannerTargetCoverage; // A is the floor for B
      var val = self._snapSliderValue(parseFloat(smTargetSlider.value), minB);
      if (val < minB) val = minB; // hard clamp
      smTargetSlider.value = val;
      smTargetLabel.textContent = (val % 5 !== 0) ? val.toFixed(1) + '%' : val + '%';
      App.state.smTargetCoverage = val;
      self.updatePlannerResults();
    });

    document.getElementById('export-csv').addEventListener('click', function() {
      UIModule.exportCSV();
    });
  },

  updatePlannerResults() {
    var source = App.state.coverageMode === 'demand' ? App.precomputedDemand : App.precomputed;
    if (!source) return;
    if (!source[String(App.state.plannerTravelMinutes)]) {
      document.getElementById('plan-total-cov').textContent = 'N/A';
      document.getElementById('plan-optimal-cov').textContent = 'N/A';
      document.getElementById('plan-additional').textContent = '--';
      document.getElementById('plan-total-lockers').textContent = '--';
      MapModule.clearProposed();
      return;
    }

    var optResult = getPlacementsForState();
    var optPlacements = optResult.placements;
    document.getElementById('plan-optimal-cov').textContent = optResult.finalCoverage.toFixed(1) + '%';
    MapModule.showProposedLockers(optPlacements);

    var totalPlacements = optPlacements.length;
    var finalCoverage = optResult.finalCoverage;
    var smPlacements = [];
    var redundantEl = document.getElementById('plan-redundant-count');

    if (App.state.useSupermarkets) {
      var smResult = getSmPlacementsForState();
      smPlacements = smResult.placements;
      totalPlacements += smPlacements.length;
      finalCoverage = smResult.finalCoverage;
      document.getElementById('plan-sm-cov').textContent = smResult.finalCoverage.toFixed(1) + '%';

      if (redundantEl) {
        var rCount = smResult.redundant ? smResult.redundant.length : 0;
        redundantEl.style.display = rCount > 0 ? '' : 'none';
        redundantEl.textContent = rCount + ' optimal placement' + (rCount !== 1 ? 's' : '') +
          ' could be replaced by supermarkets';
      }

      MapModule.showSupermarketPlacements(smPlacements);
    } else {
      MapModule.clearSupermarketPlacements();
      if (redundantEl) redundantEl.style.display = 'none';
    }

    var existingCount = App.data.bbox ? App.data.bbox.length : 0;
    document.getElementById('plan-total-cov').textContent = finalCoverage.toFixed(1) + '%';
    document.getElementById('plan-additional').textContent = totalPlacements.toLocaleString();
    document.getElementById('plan-total-lockers').textContent = (existingCount + totalPlacements).toLocaleString();

    var useDemandMode = App.state.coverageMode === 'demand';
    var activeSource = useDemandMode
      ? (App.precomputedDemand ? App.precomputedDemand[String(App.state.plannerTravelMinutes)] : null)
      : (App.precomputed      ? App.precomputed[String(App.state.plannerTravelMinutes)]      : null);

    var activeCurve = [];
    if (activeSource) {
      activeCurve.push({ lockers: 0, coverage: activeSource.startCoverage });
      for (var ci = 0; ci < activeSource.placements.length; ci++) {
        activeCurve.push({ lockers: ci + 1, coverage: activeSource.placements[ci].cum });
      }
    }

    // SM curve starts at the end of the optimal curve so the two segments join visually
    var smChartCurve = null;
    if (App.state.useSupermarkets && smPlacements.length > 0) {
      var optCount = optPlacements.length;
      smChartCurve = [{ lockers: optCount, coverage: optResult.finalCoverage }];
      for (var si = 0; si < smPlacements.length; si++) {
        smChartCurve.push({ lockers: optCount + si + 1, coverage: smPlacements[si].cum });
      }
    }

    ChartModule.updateChart('planner-chart', activeCurve, smChartCurve, useDemandMode);
    this.updatePlannerTable(optPlacements, smPlacements);
    this._lastOptPlacements = optPlacements;
    this._lastSmPlacements = smPlacements;
  },

  updatePlannerTable(optPlacements, smPlacements) {
    var tbody = document.querySelector('#planner-table tbody');
    tbody.innerHTML = '';

    function makeRow(cells, lat, lng) {
      var tr = document.createElement('tr');
      tr.innerHTML = cells;
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', function() {
        MapModule.map.setView([lat, lng], 14);
        MapModule.highlightLocation(lat, lng);
      });
      return tr;
    }

    optPlacements.slice(0, 50).forEach(function(p, i) {
      tbody.appendChild(makeRow(
        '<td>' + (i + 1) + '</td><td>' + (p.sc || '') + '</td><td>' +
        p.gain.toLocaleString() + '</td><td>' + p.cum.toFixed(1) + '%</td>',
        p.lat, p.lng
      ));
    });

    if (smPlacements && smPlacements.length > 0) {
      var divider = document.createElement('tr');
      divider.innerHTML = '<td colspan="4" style="background:var(--sidebar-border);color:var(--text-muted);font-size:10px;text-transform:uppercase;letter-spacing:0.5px;padding:6px 8px;font-weight:600">Supermarket Top-Up</td>';
      tbody.appendChild(divider);

      smPlacements.slice(0, 50).forEach(function(s, i) {
        tbody.appendChild(makeRow(
          '<td>' + (i + 1) + '</td><td>' + (s.name || 'Unknown') + '</td><td>' +
          s.gain.toLocaleString() + '</td><td>' + s.cum.toFixed(1) + '%</td>',
          s.lat, s.lng
        ));
      });
    }
  },

  exportCSV() {
    var optPlacements = this._lastOptPlacements || [];
    var smPlacements  = this._lastSmPlacements  || [];
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
