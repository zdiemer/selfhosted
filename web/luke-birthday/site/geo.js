/* ~*~ LUKE'S 30th BIRTHDAY HOMEPAGE ~*~ scripts.
   No frameworks. No build step. Netscape would be proud. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var LS = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };

  /* ---------------- hit counter ---------------- */
  // Static host, no backend: the counter is honest about the one visitor it can
  // actually see (you), sitting on top of a suitably 1998 starting number.
  var hits = parseInt(LS.get('luke30.hits', '0'), 10) + 1;
  LS.set('luke30.hits', hits);
  var total = 1996 + hits * 7;
  var padded = ('0000000' + total).slice(-7);
  $('counter').textContent = padded;
  $('marqhits').textContent = total;

  /* ---------------- seconds alive ---------------- */
  var BORN = new Date(1996, 7, 16, 7, 30, 0); // Aug 16 1996
  function tickAlive() {
    var secs = Math.floor((Date.now() - BORN.getTime()) / 1000);
    $('alive').textContent = secs.toLocaleString() + ' sec';
  }
  tickAlive();
  setInterval(tickAlive, 1000);

  /* ---------------- engaged for ---------------- */
  var ENGAGED = new Date(2026, 5, 20, 0, 0, 0); // June 20 2026
  function tickEngaged() {
    var ms = Date.now() - ENGAGED.getTime();
    var days = Math.floor(ms / 86400000);
    var hrs = Math.floor(ms / 3600000) % 24;
    var mins = Math.floor(ms / 60000) % 60;
    var secs = Math.floor(ms / 1000) % 60;
    $('engagedfor').textContent = days + ' DAYS, ' + hrs + 'h ' + mins + 'm ' + secs + 's';
    $('engageddays').textContent = days + ' days ago';
  }
  tickEngaged();
  setInterval(tickEngaged, 1000);

  /* ---------------- fake status bar ---------------- */
  var sb = $('statusbar');
  var statuses = [
    'Document: Done. (0.03 secs)',
    'Connect: Contacting host luke.diemer.codes…',
    'Transferring data from birthday.cgi…',
    'Applet BirthdayCake.class started',
    'Read 30 of 30 items',
    'Warning: this page contains 14 animated GIFs'
  ];
  var si = 0;
  setInterval(function () { si = (si + 1) % statuses.length; sb.textContent = statuses[si]; }, 4200);
  document.addEventListener('mouseover', function (e) {
    if (e.target.tagName === 'A' && e.target.href) { sb.textContent = 'Shortcut to: ' + e.target.href; }
  });

  /* ---------------- cursor sparkle trail ---------------- */
  var glyphs = ['✦', '✧', '✨', '★', '❄', '🚀', '♥'];
  var box = $('sparkles');
  var last = 0;
  document.addEventListener('mousemove', function (e) {
    var now = Date.now();
    if (now - last < 45) return;
    last = now;
    var s = document.createElement('div');
    s.className = 'sparkle';
    s.textContent = glyphs[Math.floor(Math.random() * glyphs.length)];
    s.style.left = e.clientX + 'px';
    s.style.top = e.clientY + 'px';
    s.style.color = 'hsl(' + Math.floor(Math.random() * 360) + ',100%,70%)';
    box.appendChild(s);
    setTimeout(function () { s.remove(); }, 900);
  });

  /* ---------------- falling snow (it's Colorado) ---------------- */
  var snow = $('snow');
  // One in six is a pink heart, since the engagement outranks the snow.
  var flakes = ['❄', '❅', '❆', '•', '❄', '♥'];
  function drop() {
    var f = document.createElement('div');
    f.className = 'flake';
    f.textContent = flakes[Math.floor(Math.random() * flakes.length)];
    var size = 8 + Math.random() * 16;
    var dur = 6000 + Math.random() * 7000;
    f.style.left = Math.random() * 100 + 'vw';
    f.style.fontSize = size + 'px';
    f.style.color = f.textContent === '♥' ? '#ff6699'
                  : (Math.random() < 0.15 ? '#ffe000' : '#dff2ff');
    snow.appendChild(f);
    f.animate([
      { transform: 'translateY(0) rotate(0deg)' },
      { transform: 'translateY(' + (window.innerHeight + 60) + 'px) rotate(360deg)' }
    ], { duration: dur, easing: 'linear' }).onfinish = function () { f.remove(); };
  }
  setInterval(drop, 420);
  for (var i = 0; i < 10; i++) setTimeout(drop, i * 200);

  /* ---------------- the MIDI (a WAV, but spiritually a MIDI) ---------------- */
  var audio = new Audio('hbd.wav');
  audio.loop = true;
  var btn = $('midi');
  btn.addEventListener('click', function () {
    if (audio.paused) {
      audio.play().then(function () { btn.textContent = '■ STOP SONG'; }).catch(function () {
        btn.textContent = 'blocked by browser :(';
      });
    } else {
      audio.pause();
      btn.textContent = '▶ PLAY SONG';
    }
  });

  /* ---------------- launch sequence ---------------- */
  var loglines = [
    'loading LUKE30.SYS',
    'checking jacket inventory ....... OK (too many)',
    'mounting /mnt/colorado ......... OK',
    'calibrating elevation ......... 5280 ft',
    'engaging birthday thrusters ... NOMINAL'
  ];
  var li = 0;
  setInterval(function () {
    li = (li + 1) % loglines.length;
    $('launchlog').textContent = '> ' + loglines[li];
  }, 2000);

  var t = 10;
  setInterval(function () {
    t = t > 0 ? t - 1 : 10;
    $('tminus').textContent = t === 0 ? 'LIFTOFF 🚀' : t;
  }, 1000);

  /* ---------------- Risk of Rain 2 sim ---------------- */
  var DIFFS = ['DRIZZLE', 'RAINSTORM', 'MONSOON', 'TYPHOON', 'HAHAHAHA', 'I SEE YOU', 'RISK OF THIRTY'];
  var QUOTES = [
    '"You have made a grave mistake… turning thirty."',
    '"You cannot kill me in any way that matters. I am 30."',
    '"Their courage was their undoing." — also he took the Brittle Crown',
    '"A horrible person … who is somehow a great brother."',
    '"He’s got 30 syringes and a plan."',
    '"Teleporter charged. Cake is now the boss."'
  ];
  var pct = 0, secs = 0, di = 0, deaths = 0, blamed = 0;
  setInterval(function () {
    pct = pct >= 100 ? 0 : pct + 1.7;
    $('tpbar').style.width = Math.min(100, pct) + '%';
    secs += 1;
    var m = Math.floor(secs / 60), s = secs % 60;
    $('runtime').textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    if (secs % 18 === 0) {
      di = Math.min(DIFFS.length - 1, di + 1);
      $('difficulty').textContent = DIFFS[di];
      $('difficulty').style.color = di > 3 ? '#ff5533' : '#33ff66';
    }
    if (secs % 25 === 0) {
      deaths += 1; blamed += 1;
      $('deaths').textContent = deaths;
      $('blamed').textContent = blamed;
      $('providence').textContent = QUOTES[Math.floor(Math.random() * QUOTES.length)];
    }
  }, 1000);

  /* ---------------- guestbook ---------------- */
  // localStorage-backed: entries live in the signer's own browser. It is a
  // birthday gag, not a database. Seeded so the book is never empty.
  var SEED = [
    { who: 'Zach', where: 'the closet where this server lives', when: 'Aug 16, 2026', body: 'happy 30th, brother. built you a website instead of a card. i think we both know this took longer.\n\nsee you at the teleporter.' },
    { who: 'The Mountains', where: 'Colorado', when: 'Aug 16, 2026', body: 'we are still here. you know where to find us. bring the good poles this time.' },
    { who: 'Providence', where: 'Petrichor V', when: 'Aug 16, 2026', body: 'happy birthday. the loop must continue.' },
    { who: 'I-70 Westbound', where: 'mile marker 241', when: 'Aug 16, 2026', body: 'moving freely at 7:15am. just kidding. happy birthday though.' },
    { who: 'A Rocket', where: 'low earth orbit', when: 'Aug 16, 2026', body: 'thanks for the ride up. 10/10 would be assembled by luke again.' },
    { who: 'The Engagement Ring', where: "Lynsey's hand", when: 'Aug 16, 2026', body: 'doing great up here, thanks for asking. happy birthday luke. congratulations to you both. ♥' }
  ];

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function mine() {
    try { return JSON.parse(LS.get('luke30.guestbook', '[]')); } catch (e) { return []; }
  }

  function render() {
    var all = SEED.concat(mine().map(function (e) { e.mine = true; return e; }));
    $('gbentries').innerHTML = all.map(function (e) {
      return '<div class="gbentry' + (e.mine ? ' mine' : '') + '">' +
        '<span class="when">' + esc(e.when) + '</span>' +
        '<span class="who">' + esc(e.who) + '</span>' +
        '<span class="tiny"> from ' + esc(e.where || 'parts unknown') + '</span>' +
        '<div class="body">' + esc(e.body) + '</div></div>';
    }).join('') +
    '<div class="tiny">' + all.length + ' entries. Entries you sign are saved in your own browser ' +
    '(this page has no server) — so screenshot the good ones and text them to him.</div>';
  }

  $('gbform').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var body = $('gbmsg').value.trim();
    if (!body) { alert('Say something nice first!'); return; }
    var list = mine();
    list.push({
      who: $('gbname').value.trim() || 'Anonymous Coward',
      where: $('gbwhere').value.trim() || 'the internet',
      when: new Date().toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }),
      body: body
    });
    LS.set('luke30.guestbook', JSON.stringify(list));
    $('gbmsg').value = '';
    render();
    alert('THANK YOU FOR SIGNING!!! ★');
    $('gbentries').scrollIntoView({ behavior: 'smooth', block: 'end' });
  });

  $('gbclear').addEventListener('click', function () {
    if (confirm('Delete the entries you signed from this browser?')) {
      LS.set('luke30.guestbook', '[]');
      render();
    }
  });

  render();

  /* ---------------- the obligatory greeting ---------------- */
  if (!LS.get('luke30.greeted')) {
    LS.set('luke30.greeted', '1');
    setTimeout(function () { alert('★ HAPPY 30th BIRTHDAY LUKE!!! ★\n\nYou are visitor number ' + total + '.\n\nPlease sign the guestbook.'); }, 700);
  }
})();
