/* MacTV · capa compartida de sonido, vibracion y utilidades.
   La usan la tele (launcher.html) y el mando (remote.html). Antes cada una
   tenia su propio blip(); esto es una sola implementacion para las dos. */
(function (global) {
  "use strict";

  var reduced = false;
  try { reduced = matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}

  /* ------------------------------------------------------------------ sonido
     Todo se sintetiza al vuelo. La receta no es la de una campana: las
     campanas tienen parciales inarmonicos altos y suenan a notificacion
     barata. Lo que suena a interfaz cara son tres capas cortas y calidas:

       1. transitorio  ruido en banda estrecha, 10-30 ms. Es la articulacion:
                       el "toc" de madera. Va en medios (0,4-2 kHz), no en
                       agudos, porque un ruido filtrado por arriba silba.
       2. cuerpo       un seno o triangulo casi puro, con caida exponencial y
                       a menudo un pequeno glissando. Bajar la altura 5-10 %
                       mientras decae es lo que convierte un pitido en un
                       "pop" con goma dentro.
       3. peso         un seno grave de 70-150 Hz, muy corto. No se oye como
                       nota: se siente. Sin el, el sonido flota.

     Sobre eso, una cola de reverb oscura y muy corta (send por voz) para que
     nada suene seco, y una saturacion suave en el master que pega las capas.
     Los intervalos son quintas y cuartas justas: subir para abrir y
     confirmar, bajar para volver y cerrar. */

  var ac = null, bus = null, wet = null, noiseBuf = null, muted = false;
  try { muted = localStorage.getItem("mactv.mute") === "1"; } catch (e) {}

  /* Curva de saturacion suave (tanh). A los niveles de aqui casi no
     distorsiona; su trabajo es redondear los transitorios y hacer de techo. */
  function satCurve() {
    var n = 2048, c = new Float32Array(n), x, k = 1.8;
    for (var i = 0; i < n; i++) {
      x = i / (n - 1) * 2 - 1;
      /* dividido por k, no por tanh(k): asi la pendiente en el origen es 1 y
         las senales bajas pasan intactas en vez de subir 6 dB. El techo queda
         en tanh(k)/k, que es el limitador. */
      c[i] = Math.tanh(k * x) / k;
    }
    return c;
  }

  /* Impulso de sala: ruido que decae rapido y ya filtrado hacia abajo por un
     polo simple, para que la cola sea oscura y no un "sss". 180 ms bastan. */
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

      /* master: bus -> saturacion -> estante que quita filo -> salida.
         Nada de DynamicsCompressor: se come 12 dB fijos aunque la senal vaya
         muy por debajo del umbral, y aqui no hay nada que limitar. */
      var out = ac.createGain(); out.gain.value = .95;
      var tame = ac.createBiquadFilter();
      tame.type = "highshelf"; tame.frequency.value = 5000; tame.gain.value = -8;
      var glue = ac.createWaveShaper();
      glue.curve = satCurve();
      try { glue.oversample = "2x"; } catch (e) {}
      /* 1,6 y no 1: el registro de estas senales es bajo, y a igual dBFS lo
         grave se oye mas flojo (curvas isofonicas). Medido, el pico mas alto
         (confirm) queda en -17 dBFS y la saturacion solo le quita 1 dB. */
      bus = ac.createGain(); bus.gain.value = 1.6;
      bus.connect(glue); glue.connect(tame); tame.connect(out); out.connect(ac.destination);

      /* envio de reverb, en paralelo y despues del estante para que la cola
         entre ya oscura */
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

  /* Capa 1 y 3 comparten forma: ataque instantaneo, caida exponencial.
     o: at, gain, dur, freq, q  ->  el "toc" */
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

  /* Capa 2: la nota. o: at, gain, dur, attack, wave, bright, h2, to, bend, send
     - to    factor de altura final (1.5 sube una quinta, .667 la baja)
     - bend  en cuanto tiempo llega ahi; por defecto la mitad del decaimiento
     - h2    octava por encima, a ese nivel relativo (da madera, no cristal) */
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

  /* Alturas afinadas entre si. Registro bajo a proposito: la version anterior
     vivia en el octavo y noveno octava y por eso sonaba a juguete. */
  var N = { E2: 82.41, G2: 98, A2: 110, B2: 123.47, C3: 130.81, E3: 164.81,
            F3: 174.61, G3: 196, A3: 220, C4: 261.63, D4: 293.66, E4: 329.63,
            G4: 392, A4: 440, B4: 493.88, C5: 523.25, E5: 659.25, G5: 783.99 };
  var flip = 0;

  var CUES = {
    /* navegar: un toc de madera. Dos alturas casi iguales para que repetir no
       suene a metronomo; la diferencia se nota sin llamar la atencion */
    nav: function (t) {
      var f = (flip ^= 1) ? N.B4 : 466.16;
      tock({ at: t, gain: .055, freq: 1450, q: .9, dur: .016 });
      tone(f, { at: t, dur: .055, gain: .05, wave: "triangle", bright: 2.1,
                to: .94, bend: .05 });
      tone(N.A2, { at: t, dur: .05, gain: .04, bright: 1.5 });
    },
    /* tope: la misma madera pero apagada y grave. Dice "no hay nada mas alla" */
    edge: function (t) {
      tock({ at: t, gain: .04, freq: 430, q: .7, dur: .03 });
      tone(N.F3, { at: t, dur: .12, gain: .08, wave: "triangle", bright: 1.6,
                   to: .87, bend: .1 });
      tone(N.E2, { at: t, dur: .1, gain: .05, bright: 1.5 });
    },
    /* abrir algo: un pop que sube de la cuarta a la tonica y se queda arriba */
    select: function (t) {
      tock({ at: t, gain: .05, freq: 1700, q: 1, dur: .013 });
      tone(N.G4, { at: t, dur: .085, gain: .085, bright: 2.4, to: 1.335, bend: .045 });
      tone(N.C5, { at: t + .045, dur: .3, gain: .075, bright: 2.8, attack: .01,
                   h2: .1, send: .28 });
      tone(N.C3, { at: t, dur: .13, gain: .05, bright: 1.6 });
    },
    /* accion importante confirmada: quinta ascendente y cola con aire */
    confirm: function (t) {
      tock({ at: t, gain: .045, freq: 1600, q: 1, dur: .012 });
      tone(N.C5, { at: t, dur: .15, gain: .095, bright: 2.6, h2: .08, send: .18 });
      tone(N.G5, { at: t + .085, dur: .52, gain: .08, bright: 3, attack: .009,
                   h2: .12, send: .45 });
      tone(N.C3, { at: t, dur: .17, gain: .055, bright: 1.6 });
    },
    /* volver: el mismo pop, al reves. Baja una quinta mientras decae */
    back: function (t) {
      tock({ at: t, gain: .04, freq: 1050, q: .9, dur: .015 });
      tone(N.A4, { at: t, dur: .13, gain: .09, bright: 2.2, to: .667, bend: .075,
                   send: .15 });
      tone(N.A2, { at: t, dur: .1, gain: .045, bright: 1.5 });
    },
    /* abrir un panel: crece y sube una quinta, sin transitorio, al tiempo de
       la animacion */
    sheet: function (t) {
      tone(N.C4, { at: t, dur: .34, gain: .07, bright: 2.4, attack: .05,
                   to: 1.5, bend: .17, h2: .09, send: .35 });
      tone(N.C3, { at: t, dur: .2, gain: .035, bright: 1.5, attack: .035 });
    },
    /* cerrar un panel: lo mismo hacia abajo y mas corto */
    close: function (t) {
      tone(N.G4, { at: t, dur: .26, gain: .06, bright: 2.2, attack: .022,
                   to: .667, bend: .15, send: .28 });
      tone(N.G2, { at: t, dur: .16, gain: .03, bright: 1.5, attack: .022 });
    },
    /* boton del mando: tecla. Mas transitorio que nota */
    press: function (t) {
      tock({ at: t, gain: .075, freq: 2000, q: .8, dur: .012 });
      tone(N.E5, { at: t, dur: .05, gain: .06, wave: "triangle", bright: 1.9,
                   to: .88, bend: .038 });
      tone(N.B2, { at: t, dur: .045, gain: .04, bright: 1.5 });
    },
    /* repeticion al mantener: la misma tecla, a la mitad */
    tick: function (t) {
      tock({ at: t, gain: .042, freq: 1950, q: .8, dur: .01 });
      tone(N.E5, { at: t, dur: .034, gain: .03, wave: "triangle", bright: 1.9,
                   to: .9, bend: .028 });
    },
    /* algo ha fallado: dos notas graves y sordas, la segunda un semitono abajo */
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

  /* --------------------------------------------------------------- vibracion
     navigator.vibrate existe en Android/Chrome. iOS Safari no lo implementa:
     el unico resorte que queda alli es alternar un control <input switch>,
     que desde Safari 17.4 emite un tic del sistema. Es un tic fijo, no se
     puede graduar, y si el navegador no colabora no pasa nada. */

  var canVibrate = !!(global.navigator && navigator.vibrate);
  var iosSwitch = null, iosOK = false, hasSwitch = false, looksIOS = false;
  try {
    /* Dos senales, y basta con una. La propiedad IDL "switch" solo aparece
       donde WebKit la refleja, y no esta garantizado que la refleje aunque
       soporte el atributo; por eso no puede ser la unica prueba. La segunda
       es la plataforma: iPad se identifica como Mac, de ahi maxTouchPoints. */
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

  /* Patrones graduados: un roce para navegar, un golpe para confirmar. */
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
    /* Tiene que ser click() sobre la etiqueta: el tic lo produce el control al
       ejecutar su comportamiento de activacion. Esta via quedo cerrada en
       iOS 26.5; se conserva porque en 17.4-26.4 sigue siendo la mas comoda.
       Si ya hay superficies superpuestas (surfaceMode) el tic lo dan ellas y
       aqui no se toca nada, o sonarian dos veces. */
    try { s.click(); } catch (e) {}
  }

  /* --------------------------------------------- superficie tactil con switch
     Desde iOS 26.5 el tic solo sale si el dedo toca de verdad un <input
     switch>: Apple cerro la activacion por codigo. La unica salida es poner
     un switch real e invisible encima del boton y escuchar el gesto ahi.

     No se puede meter dentro de un <button> (un control dentro de otro es
     HTML invalido y iOS no lo activa), asi que va como hermano absoluto en el
     offsetParent, calcado a la geometria del boton. Al quedarse en el mismo
     contexto de apilamiento, cualquier panel que se abra encima lo tapa como
     taparia al boton. */

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

  /* Lo que llaman las paginas: una senal, los dos canales. */
  function feedback(kind) { cue(kind); haptic(kind); }

  /* iOS y Chrome solo dejan arrancar el audio dentro de un gesto. */
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
