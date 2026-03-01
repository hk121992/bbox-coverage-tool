/* ui.js — Sidebar controls, stats, planner */

const UIModule = {
  _debounceTimer: null,

  init() {
    this.setupSidebarToggle();
    this.setupModeToggle();
    this.setupTimeSlider();
    this.setupCompetitorPanel();
    this.setupStrategicPanel();
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
        MapModule.setSectorColorMode(App.state.sectorColorMode);
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
        MapModule.setSectorColorMode(App.state.sectorColorMode);
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

    this.updateStrategicStats();
  },

  updateStrategicStats() {
    if (!App.data.strategicQuadrants || !App.state.coverageResults) return;

    var quadSectors = App.data.strategicQuadrants.sectors;
    var coveredSet = new Set(App.state.coverageResults.covered.map(function(c) { return c.sc; }));

    var stats = {
      blue_ocean:    { count: 0, uncoveredCount: 0, uncoveredPop: 0 },
      battleground:  { count: 0, uncoveredCount: 0, uncoveredPop: 0 },
      frontier:      { count: 0, uncoveredCount: 0, uncoveredPop: 0 },
      crowded_niche: { count: 0, uncoveredCount: 0, uncoveredPop: 0 },
    };

    for (var i = 0; i < App.data.centroids.length; i++) {
      var c = App.data.centroids[i];
      if (c.pop === 0) continue;
      var q = quadSectors[c.sc];
      if (!q || !stats[q]) continue;

      stats[q].count++;
      if (!coveredSet.has(c.sc)) {
        stats[q].uncoveredCount++;
        stats[q].uncoveredPop += c.pop;
      }
    }

    ['blue_ocean', 'battleground', 'frontier', 'crowded_niche'].forEach(function(q) {
      var el = document.getElementById('q-' + q);
      if (el) {
        el.innerHTML = '<strong>' + stats[q].uncoveredCount.toLocaleString() + '</strong> uncovered / ' +
          stats[q].count.toLocaleString() + ' total' +
          '<br>' + stats[q].uncoveredPop.toLocaleString() + ' uncov. pop';
      }
    });
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

    var quadFilter = App.state.quadrantFilter;
    var quadSectors = (App.data.strategicQuadrants && App.data.strategicQuadrants.sectors) || null;
    var QUAD_COLORS = { blue_ocean: '#3b82f6', battleground: '#ef4444', frontier: '#a78bfa', crowded_niche: '#6b7280' };
    var QUAD_LABELS = { blue_ocean: 'Blue Ocean', battleground: 'Battleground', frontier: 'Frontier', crowded_niche: 'Crowded Niche' };

    function makeRow(cells, lat, lng, dimmed) {
      var tr = document.createElement('tr');
      tr.innerHTML = cells;
      tr.style.cursor = 'pointer';
      if (dimmed) tr.style.opacity = '0.2';
      tr.addEventListener('click', function() {
        MapModule.map.setView([lat, lng], 14);
        MapModule.highlightLocation(lat, lng);
      });
      return tr;
    }

    var compSectors = (App.data.competitiveCoverage && App.data.competitiveCoverage.sectors) || null;
    var matchCount = 0;
    var totalCount = Math.min(optPlacements.length, 50);

    // Build covered set once for coverage badges
    var coveredSetForTable = new Set();
    if (App.state.coverageResults) {
      App.state.coverageResults.covered.forEach(function(c) { coveredSetForTable.add(c.sc); });
    }

    optPlacements.slice(0, 50).forEach(function(p, i) {
      // Gap badge
      var gapBadge = '';
      if (compSectors && p.sc && compSectors[p.sc]) {
        var gap = compSectors[p.sc].gap;
        var cls = gap >= 1.0 ? 'gap-greenfield' : gap >= 0.7 ? 'gap-moderate' : gap >= 0.3 ? 'gap-competitive' : 'gap-saturated';
        var title = compSectors[p.sc].cc + ' competitor(s), ' + compSectors[p.sc].oc + ' operator(s)';
        gapBadge = '<span class="comp-badge ' + cls + '" title="' + title + '"></span>';
      }

      // Quadrant badge
      var quadBadge = '';
      var pQuad = quadSectors ? quadSectors[p.sc] : null;
      if (pQuad && QUAD_COLORS[pQuad]) {
        quadBadge = '<span class="quad-badge" style="background:' + QUAD_COLORS[pQuad] + '" title="' + (QUAD_LABELS[pQuad] || pQuad) + '"></span>';
      }

      // Coverage badge
      var covBadge = '';
      if (p.sc) {
        var isSectorCovered = coveredSetForTable.has(p.sc);
        covBadge = isSectorCovered
          ? '<span class="cov-badge covered" title="Already covered by existing bbox">&#10003;</span>'
          : '<span class="cov-badge uncovered" title="Uncovered — new opportunity">&#9679;</span>';
      }

      // Filter dimming
      var dimmed = false;
      if (quadFilter && pQuad !== quadFilter) {
        dimmed = true;
      } else if (quadFilter) {
        matchCount++;
      }

      tbody.appendChild(makeRow(
        '<td>' + (i + 1) + '</td><td>' + gapBadge + quadBadge + covBadge + (p.sc || '') + '</td><td>' +
        p.gain.toLocaleString() + '</td><td>' + p.cum.toFixed(1) + '%</td>',
        p.lat, p.lng, dimmed
      ));
    });

    // Filter summary
    var summaryEl = document.getElementById('planner-filter-summary');
    if (summaryEl) {
      if (quadFilter && QUAD_LABELS[quadFilter]) {
        summaryEl.style.display = '';
        summaryEl.textContent = matchCount + ' ' + QUAD_LABELS[quadFilter] + ' placement' +
          (matchCount !== 1 ? 's' : '') + ' (of ' + totalCount + ' shown)';
      } else {
        summaryEl.style.display = 'none';
      }
    }

    if (smPlacements && smPlacements.length > 0) {
      var divider = document.createElement('tr');
      divider.innerHTML = '<td colspan="4" style="background:var(--sidebar-border);color:var(--text-muted);font-size:10px;text-transform:uppercase;letter-spacing:0.5px;padding:6px 8px;font-weight:600">Supermarket Top-Up</td>';
      tbody.appendChild(divider);

      smPlacements.slice(0, 50).forEach(function(s, i) {
        tbody.appendChild(makeRow(
          '<td>' + (i + 1) + '</td><td>' + (s.name || 'Unknown') + '</td><td>' +
          s.gain.toLocaleString() + '</td><td>' + s.cum.toFixed(1) + '%</td>',
          s.lat, s.lng, false
        ));
      });
    }
  },

  setupCompetitorPanel() {
    if (!App.data.competitors) return;

    var masterCheckbox = document.getElementById('show-competitors');
    var togglesDiv = document.getElementById('competitor-operator-toggles');
    var statsDiv = document.getElementById('competitor-stats');
    var colorModeGroup = document.getElementById('sector-color-mode-group');
    var legendComp = document.getElementById('legend-comp');
    var legendGapGreen = document.getElementById('legend-gap-greenfield');
    var legendGapSat = document.getElementById('legend-gap-saturated');
    var opGrid = document.getElementById('comp-op-grid');
    var self = this;

    // Count operators
    var opCounts = {};
    for (var i = 0; i < App.data.competitors.length; i++) {
      var op = App.data.competitors[i].operator;
      opCounts[op] = (opCounts[op] || 0) + 1;
    }

    // Sort by count descending
    var ops = Object.keys(opCounts).sort(function(a, b) { return opCounts[b] - opCounts[a]; });

    // Build operator checkboxes
    var COLORS = MapModule.COMPETITOR_COLORS;
    ops.forEach(function(op) {
      var colors = COLORS[op] || COLORS.other;
      var label = document.createElement('label');
      label.className = 'comp-op-label';
      label.innerHTML =
        '<input type="checkbox" class="comp-op-checkbox" data-operator="' + op + '" checked>' +
        '<span class="comp-op-dot" style="background:' + colors.fill + '"></span>' +
        op + ' (' + opCounts[op] + ')';
      opGrid.appendChild(label);

      label.querySelector('input').addEventListener('change', function() {
        MapModule.toggleCompetitorOperator(op, this.checked);
      });
    });

    // Master toggle
    masterCheckbox.addEventListener('change', function() {
      var checked = masterCheckbox.checked;
      App.state.showCompetitors = checked;
      togglesDiv.style.display = checked ? '' : 'none';
      statsDiv.style.display = checked ? '' : 'none';
      legendComp.style.display = checked ? '' : 'none';

      // Show color mode group if competitors OR strategic data loaded
      var hasStrategic = !!App.data.strategicQuadrants;
      colorModeGroup.style.display = (checked || hasStrategic) ? '' : 'none';

      MapModule.toggleCompetitors(checked);

      if (checked) {
        self.updateCompetitorStats();
      } else {
        // If on competitive mode, switch to strategic (if available) or coverage
        if (App.state.sectorColorMode === 'competitive') {
          var newMode = hasStrategic ? 'strategic' : 'coverage';
          App.state.sectorColorMode = newMode;
          MapModule.setSectorColorMode(newMode);
          document.querySelectorAll('[data-sector-mode]').forEach(function(b) { b.classList.remove('active'); });
          document.getElementById('sc-mode-' + newMode).classList.add('active');
          self._updateGapLegend(false);
          self._updateStrategicLegend(newMode === 'strategic');
        }
      }
    });

    // Sector color mode toggle
    var scModeBtns = document.querySelectorAll('[data-sector-mode]');
    scModeBtns.forEach(function(btn) {
      btn.addEventListener('click', function() {
        var mode = btn.getAttribute('data-sector-mode');
        App.state.sectorColorMode = mode;
        scModeBtns.forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');

        // When switching away from strategic, clear quadrant filter
        if (mode !== 'strategic') {
          App.state.quadrantFilter = null;
          document.querySelectorAll('.quadrant-card').forEach(function(c) { c.classList.remove('active'); });
          var resetBtn = document.getElementById('quadrant-reset');
          var filterNote = document.getElementById('quadrant-filter-note');
          if (resetBtn) resetBtn.style.display = 'none';
          if (filterNote) filterNote.style.display = 'none';
        }

        MapModule.setSectorColorMode(mode);

        // Toggle legend items per mode
        self._updateGapLegend(mode === 'competitive');
        self._updateStrategicLegend(mode === 'strategic');

        // Update planner if filter changed
        self.updatePlannerResults();
      });
    });
  },

  setupStrategicPanel() {
    if (!App.data.strategicQuadrants) return;

    var quadrants = App.data.strategicQuadrants;
    var header = document.getElementById('strategic-header');
    var panel = document.getElementById('strategic-panel');
    var colorModeGroup = document.getElementById('sector-color-mode-group');

    // Show the panel and color mode toggle
    header.style.display = '';
    panel.style.display = '';
    colorModeGroup.style.display = '';

    // Populate quadrant stats
    var summary = quadrants.summary;
    var QUAD_LABELS = {
      blue_ocean: 'Blue Ocean',
      battleground: 'Battleground',
      frontier: 'Frontier',
      crowded_niche: 'Crowded Niche',
    };

    ['blue_ocean', 'battleground', 'frontier', 'crowded_niche'].forEach(function(q) {
      var el = document.getElementById('q-' + q);
      if (el && summary[q]) {
        el.textContent = summary[q].count.toLocaleString() + ' sectors · ' +
          summary[q].uncoveredPop.toLocaleString() + ' uncovered pop';
      }
    });

    // Click handlers for quadrant cards
    var cards = document.querySelectorAll('.quadrant-card');
    var resetBtn = document.getElementById('quadrant-reset');
    var filterNote = document.getElementById('quadrant-filter-note');
    var self = this;

    cards.forEach(function(card) {
      card.addEventListener('click', function() {
        var q = card.getAttribute('data-quadrant');

        if (App.state.quadrantFilter === q) {
          // Deselect — show all quadrants
          App.state.quadrantFilter = null;
          cards.forEach(function(c) { c.classList.remove('active'); });
          resetBtn.style.display = 'none';
          filterNote.style.display = 'none';
        } else {
          // Select this quadrant
          App.state.quadrantFilter = q;
          cards.forEach(function(c) { c.classList.remove('active'); });
          card.classList.add('active');
          resetBtn.style.display = '';
          filterNote.style.display = '';
          filterNote.textContent = 'Filtering placements to ' + QUAD_LABELS[q] + ' sectors';
        }

        // Activate strategic coloring mode
        App.state.sectorColorMode = 'strategic';
        document.querySelectorAll('[data-sector-mode]').forEach(function(b) { b.classList.remove('active'); });
        var stratBtn = document.getElementById('sc-mode-strategic');
        if (stratBtn) stratBtn.classList.add('active');
        MapModule.setSectorColorMode('strategic');

        // Toggle legend items
        self._updateStrategicLegend(true);
        self._updateGapLegend(false);

        // Update planner table with filter
        self.updatePlannerResults();
      });
    });

    resetBtn.addEventListener('click', function() {
      App.state.quadrantFilter = null;
      cards.forEach(function(c) { c.classList.remove('active'); });
      resetBtn.style.display = 'none';
      filterNote.style.display = 'none';
      MapModule.setSectorColorMode('strategic');
      self.updatePlannerResults();
    });

    // "Hide covered sectors" toggle
    var hideCovCheckbox = document.getElementById('strategic-hide-covered');
    if (hideCovCheckbox) {
      hideCovCheckbox.addEventListener('change', function() {
        App.state.strategicHideCovered = hideCovCheckbox.checked;
        if (App.state.sectorColorMode === 'strategic') {
          MapModule.setSectorColorMode('strategic');
        }
      });
    }
  },

  _updateStrategicLegend(show) {
    ['legend-q-blue', 'legend-q-battle', 'legend-q-frontier', 'legend-q-crowded',
     'legend-q-uncovered', 'legend-q-covered'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.style.display = show ? '' : 'none';
    });
  },

  _updateGapLegend(show) {
    ['legend-gap-greenfield', 'legend-gap-saturated'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.style.display = show ? '' : 'none';
    });
  },

  updateCompetitorStats() {
    if (!App.data.competitiveCoverage) return;
    var stats = App.data.competitiveCoverage.stats;
    var meta = App.data.competitiveCoverage.meta;
    document.getElementById('stat-comp-total').textContent =
      meta.totalCompetitors.toLocaleString();
    document.getElementById('stat-comp-sectors').textContent =
      stats.coveredByAny.toLocaleString() + ' / ' + meta.totalSectors.toLocaleString();
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
