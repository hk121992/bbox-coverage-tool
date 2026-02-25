/* walkthrough.js — Step-by-step guided tour */

var WalkthroughTour = {
  _step: 0,
  _prevSpotlight: null,
  _savedTarget: null,
  _animFrame: null,
  _onKeyDown: null,

  steps: [
    {
      target: null,
      position: 'center',
      title: 'bbox Coverage Tool',
      body: 'Plan where to place new parcel lockers across Belgium for maximum population and demand coverage.',
    },
    {
      target: '#time-slider',
      position: 'right',
      title: 'Travel Time',
      body: 'Maximum travel time to a locker. Adjusts reach per zone: 400m urban, 600m suburban, 4km rural at 5 min. Everything updates instantly.',
    },
    {
      target: '.coverage-card',
      position: 'right',
      title: 'Current Coverage',
      body: 'How much of Belgium is already within reach of a bbox locker. Population counts heads; demand-weighted adjusts for age and income.',
    },
    {
      target: '#planner-controls',
      spotlight: '#planner-controls',
      position: 'right',
      title: 'Network Planner',
      body: 'Choose what to optimise for, set a target coverage %, and the planner shows how many new lockers are needed to get there.',
      onEnter: function() {
        var slider = document.getElementById('planner-target-slider');
        var label  = document.getElementById('planner-target-value');
        if (!slider) return;

        var current = parseInt(slider.value);
        var target  = 95;
        if (current >= target) return;

        var startTime    = null;
        var duration     = 2500;
        var self         = WalkthroughTour;
        var lastUpdateVal = current;

        function tick(timestamp) {
          if (!startTime) startTime = timestamp;
          var progress = Math.min((timestamp - startTime) / duration, 1);
          var eased    = 1 - Math.pow(1 - progress, 3); // ease-out cubic
          var val      = Math.round(current + (target - current) * eased);

          slider.value = val;
          label.textContent = val + '%';
          App.state.plannerTargetCoverage = val;

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
      target: '.table-container',
      spotlight: '.table-container',
      position: 'right',
      title: 'Proposed Locations',
      body: 'Ranked by impact. Click a row to fly to it on the map. Export to CSV for further analysis.',
    },
  ],

  _cancelAnimation: function() {
    if (this._animFrame) {
      cancelAnimationFrame(this._animFrame);
      this._animFrame = null;
    }
  },

  _clearSpotlight: function() {
    if (this._prevSpotlight) {
      this._prevSpotlight.classList.remove('wt-spotlight');
      this._prevSpotlight = null;
    }
  },

  _centerTooltip: function(tooltip) {
    if (window.innerWidth <= 600) {
      tooltip.style.transform = '';
      tooltip.style.left = '16px';
      tooltip.style.top  = Math.round(window.innerHeight / 2 - (tooltip.offsetHeight || 220) / 2) + 'px';
    } else {
      tooltip.style.top       = '50%';
      tooltip.style.left      = '50%';
      tooltip.style.transform = 'translate(-50%, -50%)';
    }
  },

  start: function() {
    this._step = 0;

    var self = this;
    this._onKeyDown = function(e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (self._step === self.steps.length - 1) { self._close(); }
        else { self._step++; self._show(); }
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        if (self._step > 0) { self._step--; self._show(); }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        self._close();
      }
    };
    document.addEventListener('keydown', this._onKeyDown);

    var slider = document.getElementById('planner-target-slider');
    var label  = document.getElementById('planner-target-value');
    if (slider) {
      this._savedTarget = parseInt(slider.value);
      var source   = App.state.coverageMode === 'demand' ? App.precomputedDemand : App.precomputed;
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
    this._cancelAnimation();

    var step    = this.steps[this._step];
    var overlay = document.getElementById('wt-overlay');
    var tooltip = document.getElementById('wt-tooltip');
    var self    = this;
    var isLast  = this._step === this.steps.length - 1;
    var isFirst = this._step === 0;

    this._clearSpotlight();

    document.getElementById('wt-step-indicator').textContent =
      'Step ' + (this._step + 1) + ' of ' + this.steps.length;
    document.getElementById('wt-title').textContent = step.title;
    document.getElementById('wt-body').textContent  = step.body;

    var backBtn = document.getElementById('wt-back');
    var nextBtn = document.getElementById('wt-next');
    backBtn.style.display = isFirst ? 'none' : '';
    nextBtn.textContent   = isLast ? 'Start exploring' : 'Next';
    document.getElementById('wt-dismiss-row').style.display = isLast ? '' : 'none';

    if (step.onEnter) step.onEnter();

    overlay.classList.remove('hidden');
    tooltip.classList.remove('hidden');
    tooltip.style.transform = '';

    if (!step.target || step.position === 'center') {
      this._centerTooltip(tooltip);
    } else {
      var el = document.querySelector(step.target);
      if (el) {
        var spotlightEl = (step.spotlight ? document.querySelector(step.spotlight) : null)
                          || el.closest('.control-group')
                          || el;
        spotlightEl.classList.add('wt-spotlight');
        this._prevSpotlight = spotlightEl;

        // On mobile: scroll so element sits ~80px from bottom, leaving room for tooltip above
        var sidebarPanel = document.getElementById('panel-main');
        if (sidebarPanel && window.innerWidth <= 600) {
          spotlightEl.scrollIntoView({ block: 'nearest', behavior: 'instant' });
          var elRect = spotlightEl.getBoundingClientRect();
          var targetBottom = window.innerHeight - 80;
          if (elRect.bottom > targetBottom) {
            sidebarPanel.scrollTop += elRect.bottom - targetBottom;
          } else if (elRect.top < 0) {
            sidebarPanel.scrollTop += elRect.top - 16;
          }
          setTimeout(function() { self._positionTooltip(tooltip, spotlightEl, step.position); }, 100);
        } else {
          spotlightEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          setTimeout(function() { self._positionTooltip(tooltip, spotlightEl, step.position); }, 400);
        }
      } else {
        this._centerTooltip(tooltip);
      }
    }

    // Clone buttons to remove stale listeners before adding new ones
    var newNext = nextBtn.cloneNode(true);
    nextBtn.parentNode.replaceChild(newNext, nextBtn);
    var newBack = backBtn.cloneNode(true);
    backBtn.parentNode.replaceChild(newBack, backBtn);

    newNext.addEventListener('click', function() {
      if (isLast) { self._close(); }
      else { self._step++; self._show(); }
    });
    newBack.addEventListener('click', function() {
      if (self._step > 0) { self._step--; self._show(); }
    });
  },

  _positionTooltip: function(tooltip, target, side) {
    var rect     = target.getBoundingClientRect();
    var pad      = 16;
    var vw       = window.innerWidth;
    var vh       = window.innerHeight;
    var isMobile = vw <= 600;
    var tw = tooltip.offsetWidth  || (isMobile ? vw - pad * 2 : 320);
    var th = tooltip.offsetHeight || 220;
    var left, top;

    if (!isMobile && side === 'right') {
      var rightLeft = rect.right + pad;
      if (rightLeft + tw <= vw - pad) {
        left = rightLeft;
        top  = Math.max(pad, Math.min(rect.top + rect.height / 2 - th / 2, vh - th - pad));
      } else {
        left = Math.max(pad, Math.min(rect.left + rect.width / 2 - tw / 2, vw - tw - pad));
        top  = Math.max(pad, rect.top - th - pad);
      }
    } else if (!isMobile && side === 'left') {
      left = Math.max(pad, rect.left - tw - pad);
      top  = Math.max(pad, Math.min(rect.top + rect.height / 2 - th / 2, vh - th - pad));
    } else {
      // Mobile: above the element if room, else below, else top of screen
      left = pad;
      var aboveTop = rect.top - th - pad;
      var belowTop = rect.bottom + pad;
      if (aboveTop >= pad) { top = aboveTop; }
      else if (belowTop + th <= vh - pad) { top = belowTop; }
      else { top = pad; }
    }

    tooltip.style.left = left + 'px';
    tooltip.style.top  = top  + 'px';
  },

  _close: function() {
    this._cancelAnimation();
    this._clearSpotlight();

    if (document.getElementById('wt-dont-show').checked) {
      localStorage.setItem('bbox_walkthrough_dismissed', '1');
    }

    document.getElementById('wt-overlay').classList.add('hidden');
    document.getElementById('wt-tooltip').classList.add('hidden');

    if (this._onKeyDown) {
      document.removeEventListener('keydown', this._onKeyDown);
      this._onKeyDown = null;
    }

    UIModule.updatePlannerResults();
  },
};
