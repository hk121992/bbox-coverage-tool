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
      target: '.table-container',
      spotlight: '.table-container',
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
      // Centre on screen — use JS on mobile to avoid transform overflow
      if (window.innerWidth <= 600) {
        tooltip.style.transform = '';
        var pad = 16;
        tooltip.style.left = pad + 'px';
        tooltip.style.top = Math.round(window.innerHeight / 2 - (tooltip.offsetHeight || 220) / 2) + 'px';
      } else {
        tooltip.style.top = '50%';
        tooltip.style.left = '50%';
        tooltip.style.transform = 'translate(-50%, -50%)';
      }
    } else {
      tooltip.style.transform = '';
      var el = document.querySelector(step.target);
      if (el) {
        // Spotlight: use explicit step.spotlight selector, or parent .control-group, or element itself
        var spotlightEl = (step.spotlight ? document.querySelector(step.spotlight) : null)
                          || el.closest('.control-group')
                          || el;
        spotlightEl.classList.add('wt-spotlight');
        this._prevSpotlight = spotlightEl;

        // Scroll the sidebar panel so the spotlit element is visible.
        // On mobile: tooltip goes above the element, so we want the element
        // near the bottom of the viewport. We manually scroll the sidebar panel.
        var sidebarPanel = document.getElementById('panel-main');
        if (sidebarPanel && window.innerWidth <= 600) {
          // Scroll element into view first (instant) to get a stable rect
          spotlightEl.scrollIntoView({ block: 'nearest', behavior: 'instant' });
          // Then nudge so element sits ~80px from viewport bottom (leaves room for it to be seen)
          var elRect = spotlightEl.getBoundingClientRect();
          var targetBottom = window.innerHeight - 80;
          if (elRect.bottom > targetBottom) {
            sidebarPanel.scrollTop += elRect.bottom - targetBottom;
          } else if (elRect.top < 0) {
            sidebarPanel.scrollTop += elRect.top - 16;
          }
          // Position tooltip after a short settle
          setTimeout(function() {
            self._positionTooltip(tooltip, spotlightEl, step.position);
          }, 100);
        } else {
          spotlightEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          setTimeout(function() {
            self._positionTooltip(tooltip, spotlightEl, step.position);
          }, 400);
        }
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
    var pad = 16;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var isMobile = vw <= 600;

    // On mobile the tooltip spans nearly full width — measure after layout
    var tw = tooltip.offsetWidth || (isMobile ? vw - pad * 2 : 320);
    var th = tooltip.offsetHeight || 220;

    var left, top;

    if (!isMobile && side === 'right') {
      // Desktop: prefer right of element
      var rightLeft = rect.right + pad;
      if (rightLeft + tw <= vw - pad) {
        left = rightLeft;
        top = Math.max(pad, Math.min(rect.top + rect.height / 2 - th / 2, vh - th - pad));
      } else {
        // Fallback: centred, above element
        left = Math.max(pad, Math.min(rect.left + rect.width / 2 - tw / 2, vw - tw - pad));
        top = Math.max(pad, rect.top - th - pad);
      }
    } else if (!isMobile && side === 'left') {
      left = Math.max(pad, rect.left - tw - pad);
      top = Math.max(pad, Math.min(rect.top + rect.height / 2 - th / 2, vh - th - pad));
    } else {
      // Mobile (or unknown side): place above element if room, else below
      left = pad;
      var aboveTop = rect.top - th - pad;
      var belowTop = rect.bottom + pad;
      if (aboveTop >= pad) {
        top = aboveTop;
      } else if (belowTop + th <= vh - pad) {
        top = belowTop;
      } else {
        // Not enough room above or below — anchor to top of screen
        top = pad;
      }
    }

    tooltip.style.left = left + 'px';
    tooltip.style.top = top + 'px';
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
