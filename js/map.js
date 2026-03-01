/* map.js — Leaflet map setup, layers, markers, sector polygons */

const MapModule = {
  map: null,
  layers: {
    bboxMarkers: null,
    sectors: null,
    proposed: null,
    supermarkets: null,
    smPlacements: null,
    competitors: null,
    competitorsByOp: {},
  },
  _nearestDistMap: null,
  _sectorColorMode: 'coverage', // 'coverage' | 'competitive'

  COMPETITOR_COLORS: {
    bpost:        { fill: '#6366f1', stroke: '#4f46e5' },
    dpd:          { fill: '#ec4899', stroke: '#db2777' },
    dhl:          { fill: '#f97316', stroke: '#ea580c' },
    postnl:       { fill: '#eab308', stroke: '#ca8a04' },
    ups:          { fill: '#78350f', stroke: '#92400e' },
    gls:          { fill: '#14b8a6', stroke: '#0d9488' },
    mondialrelay: { fill: '#8b5cf6', stroke: '#7c3aed' },
    inpost:       { fill: '#facc15', stroke: '#eab308' },
    amazon:       { fill: '#06b6d4', stroke: '#0891b2' },
    vinted:       { fill: '#f472b6', stroke: '#ec4899' },
    budbee:       { fill: '#a3e635', stroke: '#84cc16' },
    cubee:        { fill: '#2dd4bf', stroke: '#14b8a6' },
    laposte:      { fill: '#fb923c', stroke: '#f97316' },
    post_lux:     { fill: '#fbbf24', stroke: '#f59e0b' },
    other:        { fill: '#9ca3af', stroke: '#6b7280' },
  },

  init(containerId) {
    this.map = L.map(containerId, {
      center: [50.5, 4.35],
      zoom: 8,
      zoomControl: false,
    });

    L.control.zoom({ position: 'topright' }).addTo(this.map);

    // Fast zoom control — cities + regions
    this._addQuickZoomControl();

    // Custom pane for sector polygons — sits below default overlayPane (z=400)
    // so marker clicks take priority over sector clicks
    this.map.createPane('sectorPane').style.zIndex = 350;

    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
      subdomains: 'abcd',
      maxZoom: 19,
    }).addTo(this.map);

    this.layers.proposed = L.layerGroup().addTo(this.map);
    this.layers.smPlacements = L.layerGroup().addTo(this.map);
    this.layers.supermarkets = L.layerGroup();

    return this;
  },

  _addQuickZoomControl() {
    var map = this.map;

    var QuickZoom = L.Control.extend({
      options: { position: 'topright' },

      onAdd: function() {
        var container = L.DomUtil.create('div', 'quick-zoom-control');

        var locations = [
          { label: 'BE', lat: 50.5, lng: 4.35, zoom: 8, cls: 'qz-country' },
          // Flanders + cities
          { label: 'Flanders', lat: 51.05, lng: 3.73, zoom: 9, cls: 'qz-region' },
          { label: 'Antwerp', lat: 51.22, lng: 4.40, zoom: 13, cls: 'qz-city' },
          { label: 'Ghent', lat: 51.05, lng: 3.72, zoom: 13, cls: 'qz-city' },
          { label: 'Bruges', lat: 51.21, lng: 3.23, zoom: 13, cls: 'qz-city' },
          { label: 'Leuven', lat: 50.88, lng: 4.70, zoom: 13, cls: 'qz-city' },
          // Wallonia + cities
          { label: 'Wallonia', lat: 50.25, lng: 4.85, zoom: 9, cls: 'qz-region' },
          { label: 'Charleroi', lat: 50.41, lng: 4.44, zoom: 13, cls: 'qz-city' },
          { label: 'Li\u00e8ge', lat: 50.63, lng: 5.57, zoom: 13, cls: 'qz-city' },
          { label: 'Namur', lat: 50.47, lng: 4.87, zoom: 13, cls: 'qz-city' },
          // Brussels (region-level, no sub-cities)
          { label: 'Brussels', lat: 50.845, lng: 4.355, zoom: 12, cls: 'qz-region' },
        ];

        locations.forEach(function(loc) {
          var btn = L.DomUtil.create('button', 'qz-btn ' + loc.cls, container);
          btn.textContent = loc.label;
          btn.title = 'Zoom to ' + loc.label;
          L.DomEvent.disableClickPropagation(btn);
          L.DomEvent.on(btn, 'click', function() {
            map.setView([loc.lat, loc.lng], loc.zoom);
          });
        });

        return container;
      },
    });

    new QuickZoom().addTo(map);
  },

  _bboxRadiusForZoom(zoom) {
    // Scale radius with zoom: small at country level, larger at street level
    if (zoom <= 8)  return 3;
    if (zoom <= 10) return 4;
    if (zoom <= 12) return 5;
    if (zoom <= 14) return 7;
    return 9;
  },

  addBboxMarkers(bboxData) {
    this.layers.bboxMarkers = L.layerGroup();
    var self = this;

    for (const b of bboxData) {
      const marker = L.circleMarker([b.lat, b.lng], {
        radius: self._bboxRadiusForZoom(self.map.getZoom()),
        fillColor: '#2563eb',
        fillOpacity: 0.8,
        color: '#1d4ed8',
        weight: 1,
      });
      marker.bindPopup(
        '<strong>' + (b.name || 'bbox locker') + '</strong><br>' +
        '<span style="color:#888">ID: ' + b.id + '</span><br>' +
        '<span style="color:#888">' + b.lat.toFixed(5) + ', ' + b.lng.toFixed(5) + '</span>'
      );
      this.layers.bboxMarkers.addLayer(marker);
    }

    this.map.addLayer(this.layers.bboxMarkers);

    // Update radius on zoom for all circle marker layers
    this.map.on('zoomend', function() {
      var r = self._bboxRadiusForZoom(self.map.getZoom());
      self.layers.bboxMarkers.eachLayer(function(layer) {
        layer.setRadius(r);
      });
      self.layers.proposed.eachLayer(function(layer) {
        if (layer.setRadius) layer.setRadius(r);
      });
      self.layers.smPlacements.eachLayer(function(layer) {
        if (layer.setRadius) layer.setRadius(r);
      });
    });
  },

  addSectorLayer(sectorsGeoJSON, coverageResults) {
    const coveredSet = new Set(coverageResults.covered.map(function(c) { return c.sc; }));
    this._nearestDistMap = coverageResults.nearestDistMap;

    const self = this;

    this.layers.sectors = L.geoJSON(sectorsGeoJSON, {
      pane: 'sectorPane',
      style: function(feature) {
        const p = feature.properties;
        if (p.pop === 0) {
          return {
            fillColor: '#6b7280',
            fillOpacity: 0.05,
            color: '#6b7280',
            weight: 0.3,
            opacity: 0.2,
          };
        }
        const isCovered = coveredSet.has(p.sc);
        return {
          fillColor: isCovered ? '#22c55e' : '#ef4444',
          fillOpacity: 0.20,
          color: isCovered ? '#16a34a' : '#dc2626',
          weight: 0.5,
          opacity: 0.4,
        };
      },
      onEachFeature: function(feature, layer) {
        // Highlight on hover (visual only — no tooltip)
        layer.on('mouseover', function() {
          const p = feature.properties;
          if (p.pop === 0) return;
          this.setStyle({ weight: 2, fillOpacity: 0.4, opacity: 0.8 });
          this.bringToFront();
        });

        layer.on('mouseout', function() {
          self.layers.sectors.resetStyle(this);
        });

        // Show details on click
        layer.on('click', function(e) {
          const p = feature.properties;
          if (p.pop === 0) return;

          const nearestDist = self._nearestDistMap ? (self._nearestDistMap.get(p.sc) || Infinity) : Infinity;
          const isCovered = coveredSet.has(p.sc);
          const distStr = nearestDist < 1000
            ? Math.round(nearestDist) + 'm'
            : (nearestDist / 1000).toFixed(1) + 'km';

          // Look up demand score from centroids if available
          var centroid = null;
          if (App.data && App.data.centroids) {
            for (var ci = 0; ci < App.data.centroids.length; ci++) {
              if (App.data.centroids[ci].sc === p.sc) { centroid = App.data.centroids[ci]; break; }
            }
          }
          var demandLine = '';
          if (centroid && centroid.demand != null) {
            demandLine = 'Demand score: ' + Math.round(centroid.demand).toLocaleString() +
              ' (age ' + (centroid.ageRatio ? (centroid.ageRatio * 100).toFixed(0) + '%' : '?') +
              ', income idx ' + (centroid.incomeIdx ? centroid.incomeIdx.toFixed(2) : '?') + ')<br>';
          }

          // Competitor info line
          var compLine = '';
          if (App.data && App.data.competitiveCoverage && App.data.competitiveCoverage.sectors) {
            var compData = App.data.competitiveCoverage.sectors[p.sc];
            if (compData && compData.oc > 0) {
              // Has actual competitors (non-bpost)
              var opNames = compData.ops.filter(function(o) { return o !== 'bpost'; });
              compLine = 'Competitors: ' + compData.oc + ' operator' +
                (compData.oc !== 1 ? 's' : '') + ' (' + opNames.join(', ') + ')<br>';
            } else if (compData && compData.cc > 0) {
              // Only bpost own-network points nearby
              compLine = 'Competitors: <span style="color:#7c3aed">none (greenfield)</span><br>';
            } else {
              compLine = 'Competitors: <span style="color:#7c3aed">none (greenfield)</span><br>';
            }
          }

          const content =
            '<strong>' + (p.sn || p.sc) + '</strong><br>' +
            p.mun + (p.prov ? ' (' + p.prov + ')' : ' (Brussels)') + '<br>' +
            'Pop: ' + p.pop.toLocaleString() + '<br>' +
            demandLine +
            'Density: ' + Math.round(p.dens) + '/km\u00B2 (' + p.zone + ')<br>' +
            'Status: ' + (isCovered
              ? '<span style="color:#22c55e">\u2713 Covered</span>'
              : '<span style="color:#ef4444">\u2717 Uncovered</span>') + '<br>' +
            'Nearest bbox: ' + distStr + '<br>' +
            compLine;

          L.popup({ className: 'sector-popup', maxWidth: 300, autoPan: true })
            .setLatLng(e.latlng)
            .setContent(content)
            .openOn(self.map);
        });
      },
    });

    // Zoom-based visibility
    const self2 = this;
    this.map.on('zoomend', function() {
      const zoom = self2.map.getZoom();
      if (zoom >= 9 && !self2.map.hasLayer(self2.layers.sectors)) {
        self2.map.addLayer(self2.layers.sectors);
      } else if (zoom < 9 && self2.map.hasLayer(self2.layers.sectors)) {
        self2.map.removeLayer(self2.layers.sectors);
      }
    });

    if (this.map.getZoom() >= 9) {
      this.map.addLayer(this.layers.sectors);
    }
  },

  updateSectorColors(coverageResults) {
    if (!this.layers.sectors) return;

    const coveredSet = new Set(coverageResults.covered.map(function(c) { return c.sc; }));
    this._nearestDistMap = coverageResults.nearestDistMap;

    this.layers.sectors.eachLayer(function(layer) {
      const p = layer.feature.properties;
      if (p.pop === 0) return;

      const isCovered = coveredSet.has(p.sc);
      layer.setStyle({
        fillColor: isCovered ? '#22c55e' : '#ef4444',
        fillOpacity: 0.20,
        color: isCovered ? '#16a34a' : '#dc2626',
        weight: 0.5,
        opacity: 0.4,
      });
    });

    this.layers.sectors.options._coveredSet = coveredSet;
  },

  showProposedLockers(proposedList) {
    this.layers.proposed.clearLayers();
    var self = this;

    for (var i = 0; i < proposedList.length; i++) {
      var loc = proposedList[i];
      var marker = L.circleMarker([loc.lat, loc.lng], {
        radius: self._bboxRadiusForZoom(self.map.getZoom()),
        fillColor: '#f59e0b',
        fillOpacity: 0.8,
        color: '#d97706',
        weight: 1,
      });

      var popupContent = '<strong>Proposed #' + (i + 1) + '</strong><br>';
      if (loc.sc) popupContent += 'Sector: ' + loc.sc + '<br>';
      popupContent += 'Marginal gain: ' + loc.gain.toLocaleString() + ' people<br>';
      popupContent += 'Cumulative: ' + loc.cum.toFixed(1) + '%';

      marker.bindPopup(popupContent);
      this.layers.proposed.addLayer(marker);
    }
  },

  clearProposed() {
    this.layers.proposed.clearLayers();
  },

  showSupermarketPlacements(smList) {
    this.layers.smPlacements.clearLayers();
    var self = this;

    for (var i = 0; i < smList.length; i++) {
      var loc = smList[i];
      var marker = L.circleMarker([loc.lat, loc.lng], {
        radius: self._bboxRadiusForZoom(self.map.getZoom()),
        fillColor: '#16a34a',
        fillOpacity: 0.8,
        color: '#15803d',
        weight: 1,
      });

      marker.bindPopup(
        '<strong>Supermarket #' + (i + 1) + '</strong><br>' +
        (loc.name || 'Unknown') + '<br>' +
        'Marginal gain: ' + loc.gain.toLocaleString() + ' people<br>' +
        'Cumulative: ' + loc.cum.toFixed(1) + '%'
      );

      this.layers.smPlacements.addLayer(marker);
    }
  },

  clearSupermarketPlacements() {
    this.layers.smPlacements.clearLayers();
  },

  addSupermarketMarkers(supermarkets) {
    if (!this.layers.supermarketLayer) {
      this.layers.supermarketLayer = L.layerGroup();
    } else {
      this.layers.supermarketLayer.clearLayers();
    }

    for (const s of supermarkets) {
      const marker = L.circleMarker([s.lat, s.lng], {
        radius: 3,
        fillColor: '#16a34a',
        fillOpacity: 0.8,
        color: '#15803d',
        weight: 1,
      });
      marker.bindPopup(
        '<strong>' + (s.name || 'Unknown supermarket') + '</strong><br>' +
        '<span style="color:#888">' + s.lat.toFixed(5) + ', ' + s.lng.toFixed(5) + '</span>'
      );
      this.layers.supermarketLayer.addLayer(marker);
    }

    this.layers.supermarkets = this.layers.supermarketLayer;
  },

  highlightLocation(lat, lng) {
    // Remove previous highlight
    if (this._highlightLayer) {
      this.map.removeLayer(this._highlightLayer);
      this._highlightLayer = null;
    }
    if (this._highlightTimer) {
      clearTimeout(this._highlightTimer);
    }

    // Add a pulsing ring
    var ring = L.circleMarker([lat, lng], {
      radius: 18,
      fillColor: '#fff',
      fillOpacity: 0.3,
      color: '#fff',
      weight: 3,
      opacity: 0.9,
      className: 'highlight-pulse',
    }).addTo(this.map);

    this._highlightLayer = ring;

    // Auto-remove after 4 seconds
    var self = this;
    this._highlightTimer = setTimeout(function() {
      if (self._highlightLayer) {
        self.map.removeLayer(self._highlightLayer);
        self._highlightLayer = null;
      }
    }, 4000);
  },

  toggleSupermarkets(visible) {
    if (visible && this.layers.supermarkets) {
      this.map.addLayer(this.layers.supermarkets);
    } else if (this.layers.supermarkets) {
      this.map.removeLayer(this.layers.supermarkets);
    }
  },

  // --- Competitor Layer ---

  addCompetitorMarkers(competitors) {
    this.layers.competitors = L.layerGroup();
    this.layers.competitorsByOp = {};
    var self = this;

    // Group by operator
    var byOp = {};
    for (var i = 0; i < competitors.length; i++) {
      var c = competitors[i];
      var op = c.operator || 'other';
      if (!byOp[op]) byOp[op] = [];
      byOp[op].push(c);
    }

    // Create a sub-layergroup per operator
    for (var op in byOp) {
      var opGroup = L.layerGroup();
      var colors = this.COMPETITOR_COLORS[op] || this.COMPETITOR_COLORS.other;

      for (var j = 0; j < byOp[op].length; j++) {
        var pt = byOp[op][j];
        var marker = L.circleMarker([pt.lat, pt.lng], {
          radius: 5,
          fillColor: colors.fill,
          fillOpacity: 0.85,
          color: colors.stroke,
          weight: 1.5,
        });
        var typeLabels = {
          'locker': 'Parcel Locker',
          'parcelshop': 'Parcel Shop',
          'post_office': 'Post Office',
          'post_point': 'Post Point',
        };
        var typeLabel = typeLabels[pt.type] || pt.type || 'Pickup Point';
        var opDisplay = op.charAt(0).toUpperCase() + op.slice(1);
        marker.bindPopup(
          '<strong>' + (pt.name || typeLabel) + '</strong><br>' +
          '<span style="color:#aaa">' + opDisplay + ' · ' + typeLabel + '</span>'
        );
        opGroup.addLayer(marker);
      }

      this.layers.competitorsByOp[op] = opGroup;
      this.layers.competitors.addLayer(opGroup);
    }
  },

  toggleCompetitors(visible) {
    if (visible && this.layers.competitors) {
      this.map.addLayer(this.layers.competitors);
    } else if (this.layers.competitors) {
      this.map.removeLayer(this.layers.competitors);
    }
  },

  toggleCompetitorOperator(op, visible) {
    var opGroup = this.layers.competitorsByOp[op];
    if (!opGroup || !this.layers.competitors) return;

    if (visible) {
      this.layers.competitors.addLayer(opGroup);
    } else {
      this.layers.competitors.removeLayer(opGroup);
    }
  },

  updateSectorColorsCompetitive(competitiveCoverage) {
    if (!this.layers.sectors || !competitiveCoverage) return;

    var sectors = competitiveCoverage.sectors;

    this.layers.sectors.eachLayer(function(layer) {
      var p = layer.feature.properties;
      if (p.pop === 0) return;

      var data = sectors[p.sc];
      var gap = data ? data.gap : 1.0;

      // Gap 1.0 = deep purple (greenfield opportunity)
      // Gap 0.7 = lighter purple
      // Gap 0.3 = warm gray
      // Gap 0.0 = amber (saturated, proven demand)
      var fillColor, borderColor;
      if (gap >= 1.0)      { fillColor = '#7c3aed'; borderColor = '#6d28d9'; }
      else if (gap >= 0.7) { fillColor = '#a78bfa'; borderColor = '#8b5cf6'; }
      else if (gap >= 0.3) { fillColor = '#d1d5db'; borderColor = '#9ca3af'; }
      else                 { fillColor = '#f59e0b'; borderColor = '#d97706'; }

      layer.setStyle({
        fillColor: fillColor,
        fillOpacity: 0.25,
        color: borderColor,
        weight: 0.5,
        opacity: 0.4,
      });
    });
  },

  STRATEGIC_COLORS: {
    blue_ocean:    { fill: '#3b82f6', border: '#2563eb' },
    battleground:  { fill: '#ef4444', border: '#dc2626' },
    frontier:      { fill: '#a78bfa', border: '#8b5cf6' },
    crowded_niche: { fill: '#6b7280', border: '#4b5563' },
  },

  STRATEGIC_COVERED_COLORS: {
    blue_ocean:    { fill: '#93bbf5', border: '#7aa3e6' },
    battleground:  { fill: '#f5a0a0', border: '#e09090' },
    frontier:      { fill: '#cfc4f0', border: '#b8ade0' },
    crowded_niche: { fill: '#a0a4ab', border: '#8a8e95' },
  },

  updateSectorColorsStrategic(strategicQuadrants, highlightQuadrant, coveredSet) {
    if (!this.layers.sectors || !strategicQuadrants) return;

    var sectors = strategicQuadrants.sectors;
    var COLORS = this.STRATEGIC_COLORS;
    var COVERED = this.STRATEGIC_COVERED_COLORS;
    var hideCovered = App.state.strategicHideCovered;

    this.layers.sectors.eachLayer(function(layer) {
      var p = layer.feature.properties;
      if (p.pop === 0) return;

      var quadrant = sectors[p.sc] || 'frontier';
      var isCovered = coveredSet && coveredSet.has(p.sc);
      var isHighlighted = !highlightQuadrant || quadrant === highlightQuadrant;

      // Fully hide covered sectors when toggle is on
      if (isCovered && hideCovered) {
        layer.setStyle({ fillOpacity: 0, opacity: 0 });
        return;
      }

      var colors = isCovered
        ? (COVERED[quadrant] || COVERED.frontier)
        : (COLORS[quadrant] || COLORS.frontier);

      layer.setStyle({
        fillColor: colors.fill,
        fillOpacity: isHighlighted ? (isCovered ? 0.08 : 0.30) : 0.04,
        color: colors.border,
        weight: 0.5,
        opacity: isHighlighted ? (isCovered ? 0.15 : 0.5) : 0.10,
      });
    });
  },

  setSectorColorMode(mode) {
    this._sectorColorMode = mode;
    if (mode === 'competitive' && App.data.competitiveCoverage) {
      this.updateSectorColorsCompetitive(App.data.competitiveCoverage);
    } else if (mode === 'strategic' && App.data.strategicQuadrants) {
      var coveredSet = null;
      if (App.state.coverageResults) {
        coveredSet = new Set(App.state.coverageResults.covered.map(function(c) { return c.sc; }));
      }
      this.updateSectorColorsStrategic(App.data.strategicQuadrants, App.state.quadrantFilter, coveredSet);
    } else if (App.state.coverageResults) {
      this.updateSectorColors(App.state.coverageResults);
    }
  },
};
