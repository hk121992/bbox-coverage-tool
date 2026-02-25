/* app.js — Main orchestration, data loading, coverage computation, spatial index */

// --- Global State ---
var App = {
  data: {
    bbox: null,
    supermarkets: null,
    centroids: null,
    sectors: null,
  },
  precomputed: null,       // loaded from placements.json (population)
  precomputedDemand: null, // loaded from placements_demand.json
  precomputedSm: null,     // loaded from placements_sm.json (keyed: "{t}_{a}_{mode}")
  state: {
    mode: 'current',
    coverageMode: 'population', // 'population' | 'demand'
    travelMinutes: 5,
    sectorRegionMap: null,
    coverageResults: null,
    bboxSpatialIndex: null,
    // Planner state
    plannerTravelMinutes: 5,
    plannerTargetCoverage: 95,
    useSupermarkets: false,
    smTargetCoverage: 99,
  },
  map: null,
};

// --- Spatial Index Utilities ---

var BASE_RADII = { urban: 400, suburban: 600, rural: 4000 };
var CELL_SIZE = 0.01;

function buildSpatialIndex(points, cellSize) {
  cellSize = cellSize || CELL_SIZE;
  var index = new Map();
  for (var i = 0; i < points.length; i++) {
    var p = points[i];
    var key = Math.floor(p.lat / cellSize) + ',' + Math.floor(p.lng / cellSize);
    if (!index.has(key)) index.set(key, []);
    index.get(key).push(p);
  }
  return index;
}

function getNearbyCells(index, lat, lng, radiusDeg, cellSize) {
  cellSize = cellSize || CELL_SIZE;
  var results = [];
  var cellRadius = Math.ceil(radiusDeg / cellSize);
  var baseLat = Math.floor(lat / cellSize);
  var baseLng = Math.floor(lng / cellSize);

  for (var dlat = -cellRadius; dlat <= cellRadius; dlat++) {
    for (var dlng = -cellRadius; dlng <= cellRadius; dlng++) {
      var key = (baseLat + dlat) + ',' + (baseLng + dlng);
      var cell = index.get(key);
      if (cell) {
        for (var k = 0; k < cell.length; k++) {
          results.push(cell[k]);
        }
      }
    }
  }
  return results;
}

function haversineDistance(lat1, lng1, lat2, lng2) {
  var R = 6371000;
  var dLat = (lat2 - lat1) * 0.017453293;
  var dLng = (lng2 - lng1) * 0.017453293;
  var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(lat1 * 0.017453293) * Math.cos(lat2 * 0.017453293) *
    Math.sin(dLng / 2) * Math.sin(dLng / 2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// --- Coverage Computation (for Current Coverage mode) ---

function computeCoverage(locations, centroids, travelMinutes, modeOverride) {
  var timeMultiplier = travelMinutes / 5;
  var maxSearchRadius = BASE_RADII.rural * timeMultiplier;
  var maxRadiusDeg = maxSearchRadius / 111000;
  var mode = modeOverride || App.state.coverageMode;
  var useDemand = mode === 'demand';

  var locIndex = buildSpatialIndex(locations);

  var results = {
    covered: [],
    uncovered: [],
    totalPop: 0,
    coveredPop: 0,
    coveragePercent: 0,
    byRegion: {
      '02000': { total: 0, covered: 0 },
      '03000': { total: 0, covered: 0 },
      '04000': { total: 0, covered: 0 },
    },
    nearestDistMap: new Map(),
  };

  for (var i = 0; i < centroids.length; i++) {
    var c = centroids[i];
    if (c.pop === 0) continue;

    // Use demand score if available and mode is demand, else fall back to population
    var weight = (useDemand && c.demand != null) ? c.demand : c.pop;
    if (weight === 0) continue;

    var radiusM = BASE_RADII[c.zone] * timeMultiplier;
    var nearbyLocs = getNearbyCells(locIndex, c.lat, c.lng, maxRadiusDeg);

    var minDist = Infinity;
    for (var j = 0; j < nearbyLocs.length; j++) {
      var loc = nearbyLocs[j];
      var dist = haversineDistance(c.lat, c.lng, loc.lat, loc.lng);
      if (dist < minDist) minDist = dist;
    }

    if (minDist === Infinity) {
      minDist = 999999;
    }

    var isCovered = minDist <= radiusM;
    var rgn = App.state.sectorRegionMap ? App.state.sectorRegionMap.get(c.sc) : null;

    results.totalPop += weight;
    results.nearestDistMap.set(c.sc, minDist);

    if (rgn && results.byRegion[rgn]) {
      results.byRegion[rgn].total += weight;
    }

    if (isCovered) {
      results.coveredPop += weight;
      results.covered.push({ sc: c.sc, pop: weight, zone: c.zone, dist: minDist });
      if (rgn && results.byRegion[rgn]) {
        results.byRegion[rgn].covered += weight;
      }
    } else {
      results.uncovered.push({ sc: c.sc, pop: weight, zone: c.zone, dist: minDist });
    }
  }

  results.coveragePercent = results.totalPop > 0
    ? (results.coveredPop / results.totalPop * 100)
    : 0;

  return results;
}

// --- Planner: get precomputed optimal placements ---

function getPlacementsForState() {
  var travelTime = App.state.plannerTravelMinutes;
  var targetPct = App.state.plannerTargetCoverage;
  var useDemand = App.state.coverageMode === 'demand';

  var source = useDemand ? App.precomputedDemand : App.precomputed;
  if (!source) return { placements: [], startCoverage: 0, finalCoverage: 0, coverageCurve: [] };

  var data = source[String(travelTime)];
  if (!data) return { placements: [], startCoverage: 0, finalCoverage: 0, coverageCurve: [] };

  var placements = data.placements;
  var startCov = data.startCoverage;

  // If target is already met by current network, no placements needed
  if (targetPct <= startCov) {
    return {
      placements: [],
      startCoverage: startCov,
      finalCoverage: startCov,
      coverageCurve: [{ lockers: 0, coverage: startCov }],
    };
  }

  // Find how many placements needed to reach target
  var sliceEnd = placements.length;
  for (var i = 0; i < placements.length; i++) {
    if (placements[i].cum >= targetPct) {
      sliceEnd = i + 1;
      break;
    }
  }

  var sliced = placements.slice(0, sliceEnd);

  // Build coverage curve
  var coverageCurve = [{ lockers: 0, coverage: startCov }];
  for (var j = 0; j < sliced.length; j++) {
    coverageCurve.push({ lockers: j + 1, coverage: sliced[j].cum });
  }

  var finalCov = sliced.length > 0 ? sliced[sliced.length - 1].cum : startCov;

  return {
    placements: sliced,
    startCoverage: startCov,
    finalCoverage: finalCov,
    coverageCurve: coverageCurve,
  };
}

// --- Planner: get precomputed supermarket top-up placements ---
// Target A% = plannerTargetCoverage (optimal locker target)
// Target B% = smTargetCoverage (SM top-up target, B > A)
// Key format in placements_sm.json: "{travelMin}_{roundedA}_{mode}"

function getSmPlacementsForState() {
  if (!App.precomputedSm) return { placements: [], startCoverage: 0, finalCoverage: 0, coverageCurve: [], redundant: [] };

  var travelMin = App.state.plannerTravelMinutes;
  var targetA = App.state.plannerTargetCoverage;
  var targetB = App.state.smTargetCoverage;
  var mode = App.state.coverageMode === 'demand' ? 'demand' : 'pop';

  // Round A down to nearest 5% to find the precomputed key
  var roundedA = Math.floor(targetA / 5) * 5;
  // But if targetA is exactly on a 5% boundary, use it directly
  if (targetA % 5 !== 0) roundedA = Math.floor(targetA / 5) * 5;

  var key = travelMin + '_' + roundedA + '_' + mode;
  var data = App.precomputedSm[key];

  if (!data) {
    // Try to find the nearest available A% key for this travel time
    for (var a = roundedA; a >= 0; a -= 5) {
      var tryKey = travelMin + '_' + a + '_' + mode;
      if (App.precomputedSm[tryKey]) {
        data = App.precomputedSm[tryKey];
        break;
      }
    }
  }

  if (!data) return { placements: [], startCoverage: 0, finalCoverage: 0, coverageCurve: [], redundant: [] };

  var placements = data.placements;
  var startCov = data.startCoverage;

  // Slice SM placements to reach target B%
  var sliceEnd = placements.length;
  for (var i = 0; i < placements.length; i++) {
    if (placements[i].cum >= targetB) {
      sliceEnd = i + 1;
      break;
    }
  }

  var sliced = placements.slice(0, sliceEnd);
  var finalCov = sliced.length > 0 ? sliced[sliced.length - 1].cum : startCov;

  // Build coverage curve (SM lockers on x-axis, starting from 0)
  var coverageCurve = [{ lockers: 0, coverage: startCov }];
  for (var j = 0; j < sliced.length; j++) {
    coverageCurve.push({ lockers: j + 1, coverage: sliced[j].cum });
  }

  return {
    placements: sliced,
    startCoverage: startCov,
    finalCoverage: finalCov,
    coverageCurve: coverageCurve,
    redundant: data.redundant || [],
  };
}

// --- Loading Overlay ---

function showLoading(text, pct) {
  var overlay = document.getElementById('loading-overlay');
  overlay.classList.remove('hidden');
  document.getElementById('loading-text').textContent = text || 'Loading...';
  if (typeof pct === 'number') {
    document.getElementById('progress-fill').style.width = pct + '%';
  }
}

function hideLoading() {
  var overlay = document.getElementById('loading-overlay');
  overlay.classList.add('hidden');
}

// --- Initialization ---

async function init() {
  showLoading('Loading data...', 10);

  try {
    // Phase 1: Load small files in parallel
    var responses = await Promise.all([
      fetch('data/bbox.json').then(function(r) { return r.json(); }),
      fetch('data/supermarkets.json').then(function(r) { return r.json(); }),
      fetch('data/centroids.json').then(function(r) { return r.json(); }),
      fetch('data/placements.json').then(function(r) { return r.json(); }),
      fetch('data/placements_demand.json').then(function(r) { return r.json(); }).catch(function() { return null; }),
      fetch('data/placements_sm.json').then(function(r) { return r.json(); }).catch(function() { return null; }),
    ]);

    App.data.bbox = responses[0];
    App.data.supermarkets = responses[1];
    App.data.centroids = responses[2];
    App.precomputed = responses[3];
    App.precomputedDemand = responses[4];
    App.precomputedSm = responses[5];

    showLoading('Initializing map...', 30);

    // Init map and add bbox markers immediately
    App.map = MapModule.init('map');
    MapModule.addBboxMarkers(App.data.bbox);

    // Build bbox spatial index for distance queries
    App.state.bboxSpatialIndex = buildSpatialIndex(App.data.bbox);

    showLoading('Loading sector boundaries...', 40);

    // Phase 2: Load the large sectors.json
    App.data.sectors = await fetch('data/sectors.json').then(function(r) { return r.json(); });

    showLoading('Building spatial index...', 70);

    // Build region lookup: sc -> rgn
    App.state.sectorRegionMap = new Map();
    for (var i = 0; i < App.data.sectors.features.length; i++) {
      var f = App.data.sectors.features[i];
      App.state.sectorRegionMap.set(f.properties.sc, f.properties.rgn);
    }

    showLoading('Computing initial coverage...', 80);

    // Compute initial coverage
    App.state.coverageResults = computeCoverage(
      App.data.bbox, App.data.centroids, App.state.travelMinutes
    );

    showLoading('Rendering sectors...', 90);

    // Add sector layer
    MapModule.addSectorLayer(App.data.sectors, App.state.coverageResults);

    // Init UI controls
    UIModule.init();

    // Show stats
    UIModule.updateStats(App.state.coverageResults);

    // Sync planner target slider min and show initial planner results
    UIModule.syncTargetSliderMin();
    UIModule.updatePlannerResults();

    hideLoading();

    // Show walkthrough tour unless user dismissed it
    if (!localStorage.getItem('bbox_walkthrough_dismissed')) {
      WalkthroughTour.start();
    }

    console.log('bbox Coverage Tool initialized.');
    console.log('Sectors:', App.data.sectors.features.length);
    console.log('bbox lockers:', App.data.bbox.length);
    console.log('Supermarkets:', App.data.supermarkets.length);
    console.log('Coverage:', App.state.coverageResults.coveragePercent.toFixed(1) + '%');
    console.log('Precomputed placements loaded for travel times 1-15');

  } catch (err) {
    console.error('Initialization error:', err);
    showLoading('Error loading data: ' + err.message, 0);
  }
}

// --- Boot ---
document.addEventListener('DOMContentLoaded', init);
