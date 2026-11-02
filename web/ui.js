/* MacTV · shared layer for sound, vibration and utilities.
   Used by the TV (launcher.html) and the remote (remote.html). Each one
   used to have its own blip(); this is a single implementation for both. */
(function (global) {
  "use strict";

  var reduced = false;
  try { reduced = matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}

  /* ------------------------------------------------------------------ sound
     Everything is synthesized on the fly. The recipe isn't a bell's: bells
     have high inharmonic partials and sound like a cheap notification.
     What sounds like an expensive interface is three short, warm layers:

       1. transient    narrow-band noise, 10-30 ms. It's the articulation:
                       the wooden "tock". It sits in the mids (0.4-2 kHz),
                       not the highs, because noise filtered from above
                       whistles.
       2. body         a near-pure sine or triangle, with exponential decay
                       and often a small glissando. Dropping the pitch 5-10%
                       while it decays is what turns a beep into a "pop"
                       with some give in it.
       3. weight       a low sine at 70-150 Hz, very short. You don't hear
                       it as a note: you feel it. Without it the sound
                       floats.

     On top of that, a short, dark reverb tail (per-voice send) so nothing
     sounds dry, and gentle saturation on the master that glues the layers
     together. The intervals are just fifths and fourths: rising to open
     and confirm, falling to go back and close. */

  var ac = null, bus = null, wet = null, noiseBuf = null, muted = false;
  try { muted = localStorage.getItem("mactv.mute") === "1"; } catch (e) {}

  /* Gentle saturation curve (tanh). At the levels used here it barely
     distorts; its job is to round off transients and act as a ceiling. */
  function satCurve() {
    var n = 2048, c = new Float32Array(n), x, k = 1.8;
    for (var i = 0; i < n; i++) {
      x = i / (n - 1) * 2 - 1;
      /* divided by k, not by tanh(k): that keeps the slope at the origin at
         1, so quiet signals pass through intact instead of gaining 6 dB.
         The ceiling ends up at tanh(k)/k, which is what limits it. */
      c[i] = Math.tanh(k * x) / k;
    }
    return c;
  }

  /* Room impulse: noise that decays fast and is already low-passed by a
     simple pole, so the tail is dark instead of a hiss. 180 ms is enough. */
  function room() {
    var n = Math.floor(ac.sampleRate * .18), b = ac.createBuffer(2, n, ac.sampleRate);
    for (var ch = 0; ch < 2; ch++) {
      var d = b.getChannelData(ch), lp = 0, k;
      for (var i = 0; i < n; i++) {
        lp += .22 * ((Math.random() * 2 - 1) - lp);
        k = Math.pow(1 - i / n, 3.2);
        d[i] = lp * k;
      }
    }
    return b;
  }

  function ctx() {
    if (muted) return null;
    if (!ac) {
      var AC = global.AudioContext || global.webkitAudioContext;
      if (!AC) return null;
      try { ac = new AC(); } catch (e) { return null; }

      /* master: bus -> saturation -> shelf that takes off the edge -> output.
         No DynamicsCompressor: it eats a fixed 12 dB even when the signal
         sits well under the threshold, and there's nothing to limit here. */
      var out = ac.createGain(); out.gain.value = .95;
      var tame = ac.createBiquadFilter();
      tame.type = "highshelf"; tame.frequency.value = 5000; tame.gain.value = -8;
      var glue = ac.createWaveShaper();
      glue.curve = satCurve();
      try { glue.oversample = "2x"; } catch (e) {}
      /* 1.6, not 1: these signals sit in a low register, and at equal dBFS
         low end sounds quieter (equal-loudness curves). Measured, the
         loudest peak (confirm) sits at -17 dBFS and saturation only takes
         1 dB off it. */
      bus = ac.createGain(); bus.gain.value = 1.6;
      bus.connect(glue); glue.connect(tame); tame.connect(out); out.connect(ac.destination);

      /* reverb send, in parallel and after the shelf so the tail enters
         already dark */
      wet = ac.createGain(); wet.gain.value = .55;
      try {
        var rv = ac.createConvolver(); rv.buffer = room();
        wet.connect(rv); rv.connect(out);
      } catch (e) { wet.connect(out); }
    }
    if (ac.state === "suspended") ac.resume();
    return ac;
  }

  function noise() {
    if (!noiseBuf) {
      var n = Math.floor(ac.sampleRate * .08);
      noiseBuf = ac.createBuffer(1, n, ac.sampleRate);
      var d = noiseBuf.getChannelData(0);
      for (var i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    }
    return noiseBuf;
  }

  /* Layers 1 and 3 share a shape: instant attack, exponential decay.
     o: at, gain, dur, freq, q  ->  the "tock" */
  function tock(o) {
    var t = o.at, dur = o.dur || .02;
    var src = ac.createBufferSource(); src.buffer = noise();
    var bp = ac.createBiquadFilter();
    bp.type = "bandpass"; bp.frequency.value = o.freq || 1000; bp.Q.value = o.q || 1;
    var g = ac.createGain();
    g.gain.setValueAtTime(1e-4, t);
    g.gain.linearRampToValueAtTime(o.gain, t + .0012);
    g.gain.exponentialRampToValueAtTime(1e-4, t + dur);
    src.connect(bp); bp.connect(g); g.connect(bus);
    src.start(t); src.stop(t + dur + .01);
  }

  /* Layer 2: the note. o: at, gain, dur, attack, wave, bright, h2, to, bend, send
     - to    final pitch factor (1.5 goes up a fifth, .667 goes down one)
     - bend  how long it takes to get there; defaults to half the decay
     - h2    octave above, at that relative level (gives wood, not glass) */
  function tone(f, o) {
    var t = o.at, dur = o.dur, i, osc;

    var amp = ac.createGain();
    amp.gain.setValueAtTime(1e-4, t);
    amp.gain.linearRampToValueAtTime(o.gain, t + (o.attack || .004));
    amp.gain.exponentialRampToValueAtTime(1e-4, t + dur);

    var lp = ac.createBiquadFilter();
    lp.type = "lowpass"; lp.Q.value = .4;
    lp.frequency.setValueAtTime(Math.min(15000, f * (o.bright || 2.4)), t);
    lp.frequency.exponentialRampToValueAtTime(Math.max(160, f * 1.2), t + dur);
    amp.connect(lp); lp.connect(bus);

    if (o.send) {
      var s = ac.createGain(); s.gain.value = o.send;
      lp.connect(s); s.connect(wet);
    }

    var parts = [[1, 1]];
    if (o.h2) parts.push([2, o.h2]);
    for (i = 0; i < parts.length; i++) {
      osc = ac.createOscillator();
      osc.type = o.wave || "sine";
      osc.frequency.setValueAtTime(f * parts[i][0], t);
      if (o.to) {
        osc.frequency.exponentialRampToValueAtTime(
          f * parts[i][0] * o.to, t + (o.bend || dur * .5));
      }
      var pg = ac.createGain(); pg.gain.value = parts[i][1];
      osc.connect(pg); pg.connect(amp);
      osc.start(t); osc.stop(t + dur + .03);
    }
  }

  /* Pitches tuned against each other. Low register on purpose: the
     previous version lived in the eighth and ninth octave and that's
     why it sounded like a toy. */
  var N = { E2: 82.41, G2: 98, A2: 110, B2: 123.47, C3: 130.81, E3: 164.81,
            F3: 174.61, G3: 196, A3: 220, C4: 261.63, D4: 293.66, E4: 329.63,
            G4: 392, A4: 440, B4: 493.88, C5: 523.25, E5: 659.25, G5: 783.99 };
  var flip = 0;

  var CUES = {
    /* navigate: a wooden tock. Two near-equal pitches so repeating doesn't
       sound like a metronome; the difference is felt without drawing
       attention */
    nav: function (t) {
      var f = (flip ^= 1) ? N.B4 : 466.16;
      tock({ at: t, gain: .055, freq: 1450, q: .9, dur: .016 });
      tone(f, { at: t, dur: .055, gain: .05, wave: "triangle", bright: 2.1,
                to: .94, bend: .05 });
      tone(N.A2, { at: t, dur: .05, gain: .04, bright: 1.5 });
    },
    /* edge of the list: the same wood but muted and low. Says "there's
       nothing beyond this" */
    edge: function (t) {
      tock({ at: t, gain: .04, freq: 430, q: .7, dur: .03 });
      tone(N.F3, { at: t, dur: .12, gain: .08, wave: "triangle", bright: 1.6,
                   to: .87, bend: .1 });
      tone(N.E2, { at: t, dur: .1, gain: .05, bright: 1.5 });
    },
    /* open something: a pop that rises from the fourth to the tonic and
       stays up there */
    select: function (t) {
      tock({ at: t, gain: .05, freq: 1700, q: 1, dur: .013 });
      tone(N.G4, { at: t, dur: .085, gain: .085, bright: 2.4, to: 1.335, bend: .045 });
      tone(N.C5, { at: t + .045, dur: .3, gain: .075, bright: 2.8, attack: .01,
                   h2: .1, send: .28 });
      tone(N.C3, { at: t, dur: .13, gain: .05, bright: 1.6 });
    },
    /* important action confirmed: ascending fifth with an airy tail */
    confirm: function (t) {
      tock({ at: t, gain: .045, freq: 1600, q: 1, dur: .012 });
      tone(N.C5, { at: t, dur: .15, gain: .095, bright: 2.6, h2: .08, send: .18 });
      tone(N.G5, { at: t + .085, dur: .52, gain: .08, bright: 3, attack: .009,
                   h2: .12, send: .45 });
      tone(N.C3, { at: t, dur: .17, gain: .055, bright: 1.6 });
    },
    /* go back: the same pop, reversed. Drops a fifth while it decays */
    back: function (t) {
      tock({ at: t, gain: .04, freq: 1050, q: .9, dur: .015 });
      tone(N.A4, { at: t, dur: .13, gain: .09, bright: 2.2, to: .667, bend: .075,
                   send: .15 });
      tone(N.A2, { at: t, dur: .1, gain: .045, bright: 1.5 });
    },
    /* open a panel: grows and rises a fifth, no transient, timed to the
       animation */
    sheet: function (t) {
      tone(N.C4, { at: t, dur: .34, gain: .07, bright: 2.4, attack: .05,
                   to: 1.5, bend: .17, h2: .09, send: .35 });
      tone(N.C3, { at: t, dur: .2, gain: .035, bright: 1.5, attack: .035 });
    },
    /* close a panel: the same thing downward and shorter */
    close: function (t) {
      tone(N.G4, { at: t, dur: .26, gain: .06, bright: 2.2, attack: .022,
                   to: .667, bend: .15, send: .28 });
      tone(N.G2, { at: t, dur: .16, gain: .03, bright: 1.5, attack: .022 });
    },
    /* remote button: a key. More transient than note */
    press: function (t) {
      tock({ at: t, gain: .075, freq: 2000, q: .8, dur: .012 });
      tone(N.E5, { at: t, dur: .05, gain: .06, wave: "triangle", bright: 1.9,
                   to: .88, bend: .038 });
      tone(N.B2, { at: t, dur: .045, gain: .04, bright: 1.5 });
    },
    /* repeat while held: the same key, at half strength */
    tick: function (t) {
      tock({ at: t, gain: .042, freq: 1950, q: .8, dur: .01 });
      tone(N.E5, { at: t, dur: .034, gain: .03, wave: "triangle", bright: 1.9,
                   to: .9, bend: .028 });
    },
    /* something failed: two low, dull notes, the second a semitone lower */
    error: function (t) {
      tock({ at: t, gain: .032, freq: 390, q: .7, dur: .026 });
      tone(N.F3, { at: t, dur: .18, gain: .085, wave: "triangle", bright: 1.7,
                   send: .18 });
      tone(N.E3, { at: t + .13, dur: .28, gain: .07, wave: "triangle", bright: 1.6,
                   send: .26 });
      tone(N.E2, { at: t, dur: .2, gain: .045, bright: 1.4 });
    }
  };

  function cue(kind) {
    var c = CUES[kind]; if (!c) return;
    if (!ctx()) return;
    try { c(ac.currentTime + .001); } catch (e) {}
  }

  /* --------------------------------------------------------------- vibration
     navigator.vibrate exists on Android/Chrome. iOS Safari doesn't
     implement it: the only lever left there is toggling an <input switch>
     control, which since Safari 17.4 fires a system tick. It's a fixed
     tick, can't be graded, and if the browser doesn't cooperate nothing
     happens. */

  var canVibrate = !!(global.navigator && navigator.vibrate);
  var iosSwitch = null, iosOK = false, hasSwitch = false, looksIOS = false;
  try {
    /* Two signals, and either one is enough. The "switch" IDL property only
       shows up where WebKit reflects it, and there's no guarantee it
       reflects it even if it supports the attribute; that's why it can't be
       the only check. The second one is the platform: iPad identifies as
       Mac, hence maxTouchPoints. */
    hasSwitch = "switch" in document.createElement("input");
    looksIOS = /iP(hone|od|ad)/.test(navigator.platform || "") ||
               (navigator.maxTouchPoints > 1 && /Mac/.test(navigator.platform || ""));
    iosOK = !canVibrate && (hasSwitch || looksIOS);
  } catch (e) {}

  function switchEl() {
    if (iosSwitch) return iosSwitch;
    try {
      var l = document.createElement("label");
      l.setAttribute("aria-hidden", "true");
      l.style.cssText = "position:fixed;left:-99px;top:-99px;width:1px;height:1px;" +
                        "opacity:0";
      var i = document.createElement("input");
      i.type = "checkbox"; i.setAttribute("switch", ""); i.tabIndex = -1;
      l.appendChild(i); document.body.appendChild(l);
      iosSwitch = l;
    } catch (e) { iosOK = false; }
    return iosSwitch;
  }

  /* Graded patterns: a light brush to navigate, a solid hit to confirm. */
  var BUZZ = { nav: 7, edge: 22, select: 13, confirm: [11, 38, 20], back: 9,
               sheet: 10, close: 8, press: 11, tick: 5, error: [26, 50, 26] };
  var hapticsOn = true;
  try { hapticsOn = localStorage.getItem("mactv.haptics") !== "0"; } catch (e) {}

  function haptic(kind) {
    if (!hapticsOn) return;
    var p = BUZZ[kind]; if (p == null) return;
    if (canVibrate) { try { navigator.vibrate(p); } catch (e) {} return; }
    if (!iosOK || surfaceMode) return;
    var s = switchEl(); if (!s) return;
    /* Has to be click() on the label: the tick comes from the control
       running its activation behavior. This path got closed off in
       iOS 26.5; it's kept because on 17.4-26.4 it's still the most
       convenient one. If overlay surfaces are already in play (surfaceMode)
       they produce the tick and nothing happens here, or it would fire
       twice. */
    try { s.click(); } catch (e) {}
  }

  /* --------------------------------------------- touch surface with a switch
     Since iOS 26.5 the tick only fires if the finger actually touches an
     <input switch>: Apple closed off programmatic activation. The only way
     out is placing a real, invisible switch on top of the button and
     listening for the gesture there.

     It can't go inside a <button> (a control inside another is invalid HTML
     and iOS won't activate it), so it's placed as an absolute sibling in
     the offsetParent, matching the button's geometry exactly. Since it
     stays in the same stacking context, any panel that opens over it covers
     it just like it would cover the button. */

  var surfaceMode = false, pairs = [];

  function place(pair) {
    var el = pair.host, st = pair.ov.style;
    st.left = el.offsetLeft + "px";
    st.top = el.offsetTop + "px";
    st.width = el.offsetWidth + "px";
    st.height = el.offsetHeight + "px";
  }

  function replace() { for (var i = 0; i < pairs.length; i++) place(pairs[i]); }

  function surface(el) {
    if (!iosOK || !hapticsOn) return el;
    try {
      var p = el.offsetParent;
      if (!p || p === document.body) return el;
      var lab = document.createElement("label");
      lab.setAttribute("aria-hidden", "true");
      lab.style.cssText = "position:absolute;margin:0;padding:0;display:block;" +
                          "touch-action:manipulation;-webkit-tap-highlight-color:transparent";
      var i = document.createElement("input");
      i.type = "checkbox"; i.setAttribute("switch", ""); i.tabIndex = -1;
      i.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;" +
                        "margin:0;opacity:0";
      lab.appendChild(i);
      p.appendChild(lab);
      var pair = { host: el, ov: lab };
      pairs.push(pair); place(pair);
      if (!surfaceMode) {
        surfaceMode = true;
        addEventListener("resize", replace, { passive: true });
        addEventListener("orientationchange", replace, { passive: true });
        addEventListener("load", replace, { passive: true });
        if (global.ResizeObserver) {
          try { new ResizeObserver(replace).observe(document.documentElement); }
          catch (e) {}
        }
      }
      return lab;
    } catch (e) { return el; }
  }

  /* What the pages call: one cue, both channels. */
  function feedback(kind) { cue(kind); haptic(kind); }

  /* iOS and Chrome only let audio start inside a gesture. */
  function unlock() {
    ctx();
    ["pointerdown", "touchstart", "keydown"].forEach(function (e) {
      removeEventListener(e, unlock, true);
    });
  }
  ["pointerdown", "touchstart", "keydown"].forEach(function (e) {
    addEventListener(e, unlock, true);
  });

  global.MacTV = {
    cue: cue, haptic: haptic, feedback: feedback, reduced: reduced,
    surface: surface,
    vibrates: canVibrate || iosOK,
    diag: function () {
      return { canVibrate: canVibrate, hasSwitch: hasSwitch, looksIOS: looksIOS,
               iosOK: iosOK, hapticsOn: hapticsOn,
               platform: (navigator.platform || "?"),
               maxTouchPoints: (navigator.maxTouchPoints || 0),
               switchInDom: !!iosSwitch, surfaceMode: surfaceMode,
               surfaces: pairs.length };
    },
    mute: function (v) {
      muted = !!v;
      try { localStorage.setItem("mactv.mute", muted ? "1" : "0"); } catch (e) {}
    },
    muted: function () { return muted; },
    haptics: function (v) {
      if (v === undefined) return hapticsOn;
      hapticsOn = !!v;
      try { localStorage.setItem("mactv.haptics", hapticsOn ? "1" : "0"); } catch (e) {}
    }
  };
})(this);
