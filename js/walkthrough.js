/* walkthrough.js — Step-by-step guided tour */

var WalkthroughTour = {
  _step: 0,
  _prevSpotlight: null,
  _savedTarget: null,
  _animFrame: null,

  steps: [
    {
      target: null,
      position: 'center',
      title: 'bbox Coverage Tool',
      body: 'Plan where to place new parcel lockers across Belgium for maximum population and demand coverage.',
    },
    {
      target: '.coverage-card',
      position: 'right',
      title: 'Current Coverage',
      body: 'How much of Belgium is already within reach of a bbox locker. Population counts heads; demand-weighted adjusts for age and income.',
    },
    {
      target: '#time-slider',
      position: 'right',
      title: 'Travel Time',
      body: 'Maximum travel time to a locker. Adjusts reach per zone: 400m urban, 600m suburban, 4km rural at 5 min. Everything updates instantly.',
    },
    {
      target: '.mode-toggle',
      position: 'right',
      title: 'Network Planner',
      body: 'Choose what to optimise for, set a target coverage %, and the planner shows how many new lockers are needed to get there.',
      onEnter: function() {
        var slider = document.getElementById('planner-target-slider');
        var label = document.getElementById('planner-target-value');
        if (!slider) return;

        var current = parseInt(slider.value);
        var target = 95;
        if (current >= target) return;

        var startTime = null;
        var duration = 2500;
        var self = WalkthroughTour;
        var lastUpdateVal = current;

        function tick(timestamp) {
          if (!startTime) startTime = timestamp;
          var elapsed = timestamp - startTime;
          var progress = Math.min(elapsed / duration, 1);
          // Ease-out cubic
          var eased = 1 - Math.pow(1 - progress, 3);
          var val = Math.round(current + (target - current) * eased);

          slider.value = val;
          label.textContent = val + '%';
          App.state.plannerTargetCoverage = val;

          // Update planner every 5% change
          if (val - lastUpdateVal >= 5 || progress >= 1) {
            lastUpdateVal = val;
            UIModule.updatePlannerResults();
          }

          if (progress < 1) {
            self._animFrame = requestAnimationFrame(tick);
          } else {
            self._animFrame = null;
            slider.value = target;
            label.textContent = target + '%';
            App.state.plannerTargetCoverage = target;
            UIModule.updatePlannerResults();
          }
        }

        if (self._animFrame) cancelAnimationFrame(self._animFrame);
        self._animFrame = requestAnimationFrame(tick);
      },
    },
    {
      target: '.chart-container',
      position: 'right',
      title: 'Diminishing Returns',
      body: 'Each new locker covers fewer people. The curve shows where adding more stops being cost-effective. Orange = population, blue = demand.',
    },
    {
      target: '#planner-table',
      position: 'right',
      title: 'Proposed Locations',
      body: 'Ranked by impact. Click a row to fly to it on the map. Export to CSV for further analysis.',
    },
  ],

  _onKeyDown: null,

  start: function() {
    this._step = 0;

    // Keyboard navigation: Enter/Space = next, Left arrow = back, Escape = close
    var self = this;
    this._onKeyDown = function(e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (self._step === self.steps.length - 1) {
          self._close();
        } else {
          self._step++;
          self._show();
        }
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        if (self._step > 0) {
          self._step--;
          self._show();
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        self._close();
      }
    };
    document.addEventListener('keydown', this._onKeyDown);

    // Save current planner target, then set to start coverage so Additional = 0
    var slider = document.getElementById('planner-target-slider');
    var label = document.getElementById('planner-target-value');
    if (slider) {
      this._savedTarget = parseInt(slider.value);
      // Use the actual start coverage so no placements are generated
      var source = App.state.coverageMode === 'demand' ? App.precomputedDemand : App.precomputed;
      var startCov = 0;
      if (source && source[String(App.state.plannerTravelMinutes)]) {
        startCov = Math.floor(source[String(App.state.plannerTravelMinutes)].startCoverage);
      }
      slider.value = startCov;
      label.textContent = startCov + '%';
      App.state.plannerTargetCoverage = startCov;
      MapModule.clearProposed();
      UIModule.updatePlannerResults();
    }

    this._show();
  },

  _show: function() {
    // Stop any running animation from previous step
    if (this._animFrame) {
      cancelAnimationFrame(this._animFrame);
      this._animFrame = null;
    }

    var step = this.steps[this._step];
    var overlay = document.getElementById('wt-overlay');
    var tooltip = document.getElementById('wt-tooltip');
    var titleEl = document.getElementById('wt-title');
    var bodyEl = document.getElementById('wt-body');
    var indicator = document.getElementById('wt-step-indicator');
    var backBtn = document.getElementById('wt-back');
    var nextBtn = document.getElementById('wt-next');
    var dismissRow = document.getElementById('wt-dismiss-row');
    var self = this;
    var isLast = this._step === this.steps.length - 1;
    var isFirst = this._step === 0;

    // Remove previous spotlight
    if (this._prevSpotlight) {
      this._prevSpotlight.classList.remove('wt-spotlight');
      this._prevSpotlight = null;
    }

    // Content
    indicator.textContent = 'Step ' + (this._step + 1) + ' of ' + this.steps.length;
    titleEl.textContent = step.title;
    bodyEl.textContent = step.body;

    // Buttons
    backBtn.style.display = isFirst ? 'none' : '';
    nextBtn.textContent = isLast ? 'Start exploring' : 'Next';
    dismissRow.style.display = isLast ? '' : 'none';

    // Fire onEnter callback if defined
    if (step.onEnter) {
      step.onEnter();
    }

    // Show overlay and tooltip
    overlay.classList.remove('hidden');
    tooltip.classList.remove('hidden');

    // Position
    if (!step.target || step.position === 'center') {
      // Centre on screen
      tooltip.style.top = '50%';
      tooltip.style.left = '50%';
      tooltip.style.transform = 'translate(-50%, -50%)';
    } else {
      tooltip.style.transform = '';
      var el = document.querySelector(step.target);
      if (el) {
        // Scroll sidebar to show element
        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

        // Spotlight
        el.classList.add('wt-spotlight');
        this._prevSpotlight = el;

        // Position tooltip near the element
        setTimeout(function() {
          self._positionTooltip(tooltip, el, step.position);
        }, 50);
      } else {
        tooltip.style.top = '50%';
        tooltip.style.left = '50%';
        tooltip.style.transform = 'translate(-50%, -50%)';
      }
    }

    // Wire buttons (remove old listeners by cloning)
    var newNext = nextBtn.cloneNode(true);
    nextBtn.parentNode.replaceChild(newNext, nextBtn);
    var newBack = backBtn.cloneNode(true);
    backBtn.parentNode.replaceChild(newBack, backBtn);

    newNext.addEventListener('click', function() {
      if (isLast) {
        self._close();
      } else {
        self._step++;
        self._show();
      }
    });

    newBack.addEventListener('click', function() {
      if (self._step > 0) {
        self._step--;
        self._show();
      }
    });
  },

  _positionTooltip: function(tooltip, target, side) {
    var rect = target.getBoundingClientRect();
    var tw = 320;
    var pad = 16;

    if (side === 'right') {
      // Place to the right of the sidebar element
      var left = rect.right + pad;
      if (left + tw > window.innerWidth) {
        // Fall back: place below
        left = Math.min(rect.left, window.innerWidth - tw - pad);
      }
      var top = Math.max(pad, Math.min(rect.top, window.innerHeight - 260));
      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
    } else if (side === 'left') {
      // Place to the left of the map
      var left = rect.left - tw - pad;
      if (left < 0) left = pad;
      var top = Math.max(pad, Math.min(rect.top + rect.height / 2 - 80, window.innerHeight - 260));
      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
    }
  },

  _close: function() {
    // Stop any running animation
    if (this._animFrame) {
      cancelAnimationFrame(this._animFrame);
      this._animFrame = null;
    }

    if (document.getElementById('wt-dont-show').checked) {
      localStorage.setItem('bbox_walkthrough_dismissed', '1');
    }

    if (this._prevSpotlight) {
      this._prevSpotlight.classList.remove('wt-spotlight');
      this._prevSpotlight = null;
    }

    document.getElementById('wt-overlay').classList.add('hidden');
    document.getElementById('wt-tooltip').classList.add('hidden');

    // Remove keyboard listener
    if (this._onKeyDown) {
      document.removeEventListener('keydown', this._onKeyDown);
      this._onKeyDown = null;
    }

    // Ensure final planner state is up to date
    UIModule.updatePlannerResults();
  },
};
