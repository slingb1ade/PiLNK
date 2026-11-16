/* ATC caption for 3D Approach (AJ, 11 Nov 2026): "we can add the STT in 3D fairly easy. The STT model doesn't have to
   be perfect to display the text so we can prove that it works".

   The node's speech-to-text writes every call it hears to /atc/transcript (the same feed as the dashboard's ATC panel;
   node-local, it never leaves the node). This file holds the rules for the one-line caption in the 3D view: which
   call to show, whether it belongs to the aircraft being followed, and when to take it down again.

   Three things it is built to get right:
   - Wrong aircraft. A call is the followed aircraft's own only when the callsigns are the same, whole: ANZ12 is not
     ANZ123. A call with no callsign is shown, but never as the aircraft's own.
   - Dead air. A call stays up HOLD_S after it reaches the screen, then the caption goes. The same call never comes
     back, and a feed that has stopped shows nothing at all.
   - Sync drift. Speech reaches the feed some seconds after it is spoken (11-30 s measured on a Pi 5, Sep 2026), so
     the hold counts from when the line ARRIVES, and the caption carries how long ago the words were said. A line
     that turns up more than MAX_LAG_S late is history, not a caption, and is never shown.

   Every string here came from a radio through a speech model: it is untrusted. This file only ever hands text on;
   the page writes it with textContent. Pure functions, no DOM; loads under node for the tests. */
(function (root) {
  'use strict';
  const HOLD_S = 15;        // how long a call stays up once it is on the screen
  const MAX_LAG_S = 60;     // a call arriving later than this after it was spoken is not shown
  const FUTURE_S = 5;       // clock slack: a call stamped this far ahead of our clock is still a call
  const MAX_CHARS = 200;    // two lines on a phone; longer text is cut
  const POLL_MS = 2000, PROBE_MS = 60000;
  const CS = /^[A-Z0-9-]{2,8}$/;

  const num = v => typeof v === 'number' && isFinite(v) && v > 0;
  const obj = v => !!v && typeof v === 'object' && !Array.isArray(v);

  /* 'ANZ123' from ' anz123 '; '' for anything that is not a callsign. Both sides of every comparison go through here. */
  function normCs(v) {
    if (typeof v !== 'string') return '';
    const s = v.trim().toUpperCase();
    return CS.test(s) ? s : '';
  }

  /* Has speech-to-text ever written on this node? The route answers updated:0 where it never has (every fleet node). */
  function has(d) { return obj(d) && num(d.updated); }

  /* The dashboard's rule (atcFeedDown): no answer, a stale file or a stopped transcriber all mean "not listening". */
  function down(d) { return !obj(d) || !!d.stale || d.running === false; }

  function lineOf(l, d) {
    if (!obj(l) || !num(l.t) || typeof l.text !== 'string') return null;
    let text = l.text.replace(/\s+/g, ' ').trim();
    if (!text) return null;
    if (text.length > MAX_CHARS) text = text.slice(0, MAX_CHARS - 1) + '…';
    // per-line frequency (P3 schema); an old-format line falls back to the document's
    const freq = num(l.freq_mhz) ? l.freq_mhz : (obj(d) && num(d.freq_mhz) ? d.freq_mhz : null);
    return { t: l.t, cs: normCs(l.callsign), text, freq };
  }

  /* The newest usable line in the feed, or null. onlyCs: keep only that aircraft's calls ('' keeps none). */
  function newest(d, onlyCs) {
    const all = (obj(d) && Array.isArray(d.lines)) ? d.lines : [];
    let best = null;
    for (const raw of all) {
      const l = lineOf(raw, d);
      if (!l) continue;
      if (onlyCs !== undefined && (!onlyCs || l.cs !== onlyCs)) continue;
      if (!best || l.t >= best.t) best = l;       // same second: the later entry
    }
    return best;
  }

  /* One poll. state: what the last poll returned (null the first time). now: the node's clock, seconds.
     -> { state, show } where show is null (nothing on screen) or { t, cs, text, mine, age, freq }. */
  function step(state, d, now, followCs, opts) {
    const prev = obj(state) ? state : { key: null, since: null };
    const follow = normCs(followCs), ownOnly = !!(opts && opts.ownOnly);
    if (down(d)) return { state: prev, show: null };
    const n = newest(d, ownOnly ? follow : undefined);
    if (!n) return { state: prev, show: null };
    const key = n.t + '|' + n.cs + '|' + n.text;
    let st = prev;
    if (prev.key !== key) {                        // a call we have not had before: does it get a turn on screen?
      const lag = now - n.t;
      st = { key, since: (lag <= MAX_LAG_S && lag >= -FUTURE_S) ? now : null };
    }
    if (st.since == null || now - st.since > HOLD_S) return { state: st, show: null };
    return { state: st, show: { t: n.t, cs: n.cs, text: n.text, mine: !!follow && n.cs === follow,
      age: Math.max(0, Math.round(now - n.t)), freq: n.freq } };
  }

  function ageText(s) { return (typeof s === 'number' && isFinite(s) && s >= 0) ? Math.round(s) + ' s ago' : ''; }
  function freqText(f) { return num(f) ? f.toFixed(3) : ''; }
  function clock(t) {                              // HH:MM:SS on the viewer's clock, as the dashboard panel shows it
    if (!num(t)) return '';
    const dt = new Date(t * 1000), p = n => ('0' + n).slice(-2);
    return p(dt.getHours()) + ':' + p(dt.getMinutes()) + ':' + p(dt.getSeconds());
  }

  const api = { HOLD_S, MAX_LAG_S, FUTURE_S, MAX_CHARS, POLL_MS, PROBE_MS, normCs, has, down, newest, step, ageText, freqText, clock };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.AtcCaption = api;
})(typeof window !== 'undefined' ? window : this);
