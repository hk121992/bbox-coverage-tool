/* map.js — Leaflet map setup, layers, markers, sector polygons */

const MapModule = {
  map: null,
  layers: {
    bboxMarkers: null,
    sectors: null,
    proposed: null,
    supermarkets: null,
    smPlacements: null,
  },
  _nearestDistMap: null,

  init(containerId) {
    this.map = L.map(containerId, {
      center: [50.5, 4.35],
      zoom: 8,
      zoomControl: false,
    });

    L.control.zoom({ position: 'topright' }).addTo(this.map);

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
        layer.on('mouseover', function(e) {
          const p = feature.properties;
          if (p.pop === 0) return;

          this.setStyle({ weight: 2, fillOpacity: 0.4, opacity: 0.8 });
          this.bringToFront();

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

          const tooltip =
            '<strong>' + (p.sn || p.sc) + '</strong><br>' +
            p.mun + (p.prov ? ' (' + p.prov + ')' : ' (Brussels)') + '<br>' +
            'Pop: ' + p.pop.toLocaleString() + '<br>' +
            demandLine +
            'Density: ' + Math.round(p.dens) + '/km\u00B2 (' + p.zone + ')<br>' +
            'Status: ' + (isCovered
              ? '<span style="color:#22c55e">\u2713 Covered</span>'
              : '<span style="color:#ef4444">\u2717 Uncovered</span>') + '<br>' +
            'Nearest bbox: ' + distStr;

          layer.bindTooltip(tooltip, { sticky: true, className: 'sector-tooltip' }).openTooltip(e.latlng);
        });

        layer.on('mouseout', function() {
          self.layers.sectors.resetStyle(this);
          this.closeTooltip();
          this.unbindTooltip();
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
};
