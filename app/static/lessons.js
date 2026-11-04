/* My lessons (index.html). Loaded after the main script.

   A signed-in child's own lessons, private (no likes, followers or comments; app/lessons.py): a profile grid
   (avatar, stats, topic circles, All / Kept / To redo tabs, square tiles) and a full-screen cinema replay.
   A replay re-runs the live lesson from the saved, verified spec (LessonAPI.start with its lesson_id), so the
   pause-and-predict checkpoints come back and nothing expires. Swipe up/down (or the arrow buttons) moves to the
   next/previous lesson in the grid's order; nothing ever moves on by itself.
   The board keeps its own drawing code: on the dark chalkboard it is shown through an invert + hue-rotate filter. */
(function(){
  var $ = function(id){ return document.getElementById(id); };
  function esc(t){ return String(t == null ? '' : t).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function api(method, url, body){
    return fetch(url, { method:method, headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined, credentials:'same-origin' })
      .then(function(r){ return r.json().catch(function(){ return {}; }).then(function(j){ if(!r.ok){ var e = new Error(j.detail || ('Request failed (' + r.status + ')')); e.status = r.status; throw e; } return j; }); });
  }
  var ICON = {
    close:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    lock:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    replay:'<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
    twin:'<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="7" width="10" height="10" rx="2"/><rect x="11" y="4" width="10" height="10" rx="2"/></svg>',
    star:'<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/></svg>',
    up:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg>',
    down:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>'
  };
  var CHAPTERS = ['Read', 'Draw', 'Think', 'Answer', 'Check'];

  var font = document.createElement('link');
  font.rel = 'stylesheet'; font.href = 'https://fonts.googleapis.com/css2?family=Nunito:wght@500;700;800&display=swap';
  document.head.appendChild(font);
  var css = document.createElement('style');
  css.textContent = [
    // cinema palette
    '.myl, body.cinema #lesson{ --c-bg:#0b0d12; --c-board:#11241b; --c-chalk:#e2e8f0; --c-bright:#f8fafc; --c-mute:#94a3b8; --c-amber:#fbbf24; --c-rose:#fb7185; --c-glass:rgba(255,255,255,.12); }',
    '.myl{ position:fixed; inset:0; z-index:45; background:var(--c-bg); color:var(--c-chalk); font-family:Nunito,system-ui,sans-serif; overflow-y:auto; -webkit-overflow-scrolling:touch; }',
    '.myl[hidden]{ display:none; }',
    '.myl-in{ max-width:720px; margin:0 auto; padding:12px 16px 40px; }',
    '.myl button{ font-family:inherit; }',
    '.myl-bar{ display:flex; align-items:center; justify-content:space-between; gap:8px; min-height:48px; }',
    '.myl-bar h2{ margin:0; font-size:20px; font-weight:800; color:var(--c-bright); }',
    '.myl-x, .cine-x, .cine-nav button{ width:44px; height:44px; border-radius:22px; border:none; background:var(--c-glass); color:var(--c-bright); display:inline-flex; align-items:center; justify-content:center; cursor:pointer; flex:none; }',
    '.myl-head{ display:flex; align-items:center; gap:18px; margin:10px 0 8px; }',
    '.myl-av{ width:76px; height:76px; border-radius:50%; flex:none; display:flex; align-items:center; justify-content:center; font-size:42px; background:var(--c-board); box-shadow:0 0 0 3px var(--c-bg), 0 0 0 5px var(--c-amber); }',
    '.myl-stats{ flex:1; display:flex; justify-content:space-around; text-align:center; }',
    '.myl-stats b{ display:block; font-size:22px; font-weight:800; color:var(--c-bright); }',
    '.myl-stats span{ font-size:13px; color:var(--c-mute); }',
    '.myl-name{ font-size:17px; font-weight:800; color:var(--c-bright); }',
    '.myl-lock{ display:flex; align-items:center; gap:6px; font-size:13px; color:var(--c-mute); margin:4px 0 0; }',
    '.myl-topics{ display:flex; gap:14px; overflow-x:auto; padding:14px 2px 6px; scrollbar-width:none; }',
    '.myl-topic{ flex:none; width:66px; border:none; background:none; color:var(--c-chalk); cursor:pointer; padding:0; text-align:center; font-size:12px; line-height:1.15; }',
    '.myl-topic i{ display:flex; width:58px; height:58px; margin:0 auto 5px; border-radius:50%; background:var(--c-board); box-shadow:0 0 0 2px var(--c-bg), 0 0 0 4px #475569; align-items:center; justify-content:center; }',
    '.myl-topic.mastered i{ box-shadow:0 0 0 2px var(--c-bg), 0 0 0 4px var(--c-amber); }',
    '.myl-topic[aria-pressed="true"] i{ background:#1e3a2b; outline:2px dashed var(--c-bright); outline-offset:5px; }',
    '.myl-tabs{ display:flex; border-bottom:1px solid #1f2937; margin:8px 0 2px; }',
    '.myl-tabs button{ flex:1; min-height:44px; border:none; background:none; color:var(--c-mute); font-size:15px; font-weight:800; cursor:pointer; border-bottom:3px solid transparent; }',
    '.myl-tabs button[aria-selected="true"]{ color:var(--c-bright); border-bottom-color:var(--c-amber); }',
    '.myl-grid{ display:grid; grid-template-columns:repeat(3, 1fr); gap:3px; margin-top:3px; }',
    '.myl-tile{ position:relative; aspect-ratio:1/1; border:none; padding:0; background:var(--c-board); color:var(--c-chalk); cursor:pointer; overflow:hidden; text-align:left; }',
    '.myl-tile svg{ position:absolute; left:10%; top:14%; width:80%; height:44%; }',
    '.myl-tile .lbl{ position:absolute; left:8px; right:8px; bottom:20px; font-size:13px; font-weight:800; line-height:1.15; color:var(--c-bright); display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }',
    '.myl-tile .mark{ position:absolute; top:6px; right:7px; font-size:12px; font-weight:800; }',
    '.myl-tile .mark.kept{ color:var(--c-amber); font-size:16px; }',
    '.myl-tile .mark.redo{ color:var(--c-rose); }',
    '.myl-tile .dur{ position:absolute; left:8px; bottom:5px; font-size:11px; color:var(--c-mute); }',
    '.myl-tile .prog{ position:absolute; left:0; right:0; bottom:0; height:4px; background:rgba(255,255,255,.18); }',
    '.myl-tile .prog i{ display:block; height:100%; background:var(--c-amber); }',
    '.myl-tile.pinned{ outline:2px solid var(--c-amber); outline-offset:-2px; }',
    '.myl-tile .go{ position:absolute; top:6px; left:7px; font-size:11px; font-weight:800; color:#1c1917; background:var(--c-amber); border-radius:8px; padding:1px 6px; }',
    '.myl-empty{ color:var(--c-mute); text-align:center; padding:36px 10px; font-size:16px; }',
    // cinema replay: the existing lesson screen, full screen and dark
    'body.cinema{ overflow:hidden; }',
    // (colours come from the Cinema skin's tokens, which ui/app.css also applies to body.cinema #lesson)
    'body.cinema #lesson.on{ position:fixed; inset:0; z-index:46; background:var(--c-bg); padding:62px 72px 112px 8px; margin:0; }',
    'body.cinema #lesson .lesson-grid{ flex-direction:column; padding:0; gap:8px; }',
    'body.cinema #lesson .board-slot{ flex:none; padding:0; }',
    'body.cinema #lesson .talk{ width:auto; padding:0; }',
    'body.cinema #lesson .mic-row{ gap:8px; } body.cinema #lesson .mic{ min-width:0; flex:1; max-width:260px; } body.cinema #lesson .mic .lbl small{ display:none; } body.cinema #lesson .side-btn{ width:48px; height:48px; }',
    'body.cinema #lesson .ask h3::before{ content:"Paused"; display:block; font-size:12px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; margin-bottom:2px; }',
    'body.cinema #lesson #againBtn, body.cinema #lesson #renderBtn, body.cinema #lesson #endLessons{ display:none !important; }',
    '.cine-top, .cine-rail, .cine-foot{ display:none; }',
    'body.cinema .cine-top{ display:flex; position:fixed; z-index:47; left:0; right:0; top:0; padding:10px 12px; align-items:center; gap:10px; background:linear-gradient(#0b0d12 60%, rgba(11,13,18,0)); color:#f1f5f9; font-family:Nunito,system-ui,sans-serif; }',
    '.cine-top .t{ flex:1; font-size:16px; font-weight:800; }',
    '.cine-top .n{ font-size:14px; color:#94a3b8; }',
    '.cine-nav{ display:flex; gap:6px; }',
    '.cine-nav button:disabled{ opacity:.35; cursor:default; }',
    'body.cinema .cine-rail{ display:flex; position:fixed; z-index:47; right:12px; top:var(--rail-top, 340px); flex-direction:column; gap:14px; align-items:center; font-family:Nunito,system-ui,sans-serif; }',
    '.cine-rail button{ width:52px; height:52px; border-radius:26px; border:none; background:rgba(255,255,255,.12); color:#f1f5f9; display:flex; align-items:center; justify-content:center; cursor:pointer; font-family:inherit; font-weight:800; font-size:15px; }',
    '.cine-rail button[aria-pressed="true"]{ background:rgba(251,191,36,.2); color:#fbbf24; }',
    '.cine-rail button:disabled{ opacity:.4; cursor:default; }',
    '.cine-rail small{ display:block; font-size:11px; color:#94a3b8; text-align:center; margin-top:-10px; }',
    'body.cinema .cine-foot{ display:block; position:fixed; z-index:47; left:0; right:0; bottom:0; padding:22px 16px 10px; background:linear-gradient(rgba(11,13,18,0), #0b0d12 30%); color:#f1f5f9; font-family:Nunito,system-ui,sans-serif; pointer-events:none; }',
    '.cine-foot .t{ font-size:17px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }',
    '.cine-foot .m{ font-size:13px; color:#cbd5e1; margin-top:2px; }',
    '.cine-prog{ position:relative; height:4px; background:rgba(255,255,255,.2); border-radius:2px; margin-top:10px; }',
    '.cine-prog > i{ position:absolute; left:0; top:0; bottom:0; background:#fbbf24; border-radius:2px; transition:width .4s ease; }',
    '.cine-prog > b{ position:absolute; top:-2px; width:2px; height:8px; background:#0b0d12; }',
    '.cine-chap{ display:flex; justify-content:space-between; font-size:12px; color:#64748b; margin-top:6px; }',
    '.cine-chap span.on{ color:#fbbf24; font-weight:800; }',
    '.cine-foot .myl-lock{ margin-top:8px; }',
    '@media (min-width:900px){ body.cinema #lesson.on{ padding-left:calc(50vw - 380px); padding-right:calc(50vw - 380px); } body.cinema .cine-rail{ right:calc(50vw - 450px); } body.cinema #lesson .board-slot > .board-wrap{ width:100%; } }',
    '@media (max-height:700px){ .cine-rail small{ display:none; } body.cinema .cine-rail{ gap:8px; } }',
    '@media (prefers-reduced-motion: reduce){ .cine-prog > i{ transition:none; } }'
  ].join('\n');
  document.head.appendChild(css);

  // ---------------------------------------------------------------- DOM
  var grid = document.createElement('div');
  grid.className = 'myl'; grid.id = 'myl'; grid.hidden = true;
  grid.setAttribute('role', 'dialog'); grid.setAttribute('aria-label', 'My lessons');
  grid.innerHTML = '<div class="myl-in"><div class="myl-bar"><h2>My lessons</h2><button type="button" class="myl-x" id="mylClose" aria-label="Close My lessons">' + ICON.close + '</button></div>' +
    '<div id="mylBody"></div></div>';
  document.body.appendChild(grid);
  var lessonEl = $('lesson');
  var top = document.createElement('div'); top.className = 'cine-top';
  top.innerHTML = '<button type="button" class="cine-x" id="cineClose" aria-label="Back to My lessons">' + ICON.close + '</button>' +
    '<span class="t">My lessons</span><span class="n" id="cineN"></span>' +
    '<span class="cine-nav"><button type="button" id="cinePrev" aria-label="Previous lesson">' + ICON.up + '</button><button type="button" id="cineNext" aria-label="Next lesson">' + ICON.down + '</button></span>';
  var rail = document.createElement('div'); rail.className = 'cine-rail';
  rail.innerHTML = '<button type="button" id="cineReplay" aria-label="Replay from the start">' + ICON.replay + '</button><small>Replay</small>' +
    '<button type="button" id="cineTwin" aria-label="Try a twin problem">' + ICON.twin + '</button><small id="cineTwinLbl">Twin</small>' +
    '<button type="button" id="cineKeep" aria-label="Keep this lesson" aria-pressed="false">' + ICON.star + '</button><small>Keep</small>' +
    '<button type="button" id="cineSpeed" aria-label="Teacher speed">1×</button><small>Speed</small>';
  var foot = document.createElement('div'); foot.className = 'cine-foot';
  foot.innerHTML = '<div class="t" id="cineTitle"></div><div class="m" id="cineMeta"></div>' +
    '<div class="cine-prog" id="cineProg" role="progressbar" aria-label="Lesson progress" aria-valuemin="0" aria-valuemax="100"><i></i></div>' +
    '<div class="cine-chap" id="cineChap">' + CHAPTERS.map(function(c){ return '<span>' + c + '</span>'; }).join('') + '</div>' +
    '<div class="myl-lock">' + ICON.lock + 'Only you and your parent can see your lessons</div>';
  document.body.appendChild(top); document.body.appendChild(rail); document.body.appendChild(foot);

  // ---------------------------------------------------------------- grid
  // current: the saved lesson being replayed; keep: what the Keep button acts on (that lesson, or a twin once saved)
  var state = { which:'all', family:null, data:null, list:[], at:-1, current:null, playing:null, keep:null };
  function fmtDur(r){
    if(r.seconds){ var s = Math.round(r.seconds); return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2); }
    return r.steps ? r.steps + ' steps' : '';
  }
  function bars(thumb, w, h){  // the mini bar sketch: chalk rectangles from the first stage's lengths
    var t = (thumb && thumb.length ? thumb : [1, .6]).slice(0, 4), gap = h / (t.length * 1.6), bh = gap;
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true" preserveAspectRatio="xMinYMid meet">' + t.map(function(v, i){
      var y = gap * .3 + i * gap * 1.6;
      return '<rect x="1.5" y="' + y.toFixed(1) + '" width="' + Math.max(8, v * (w - 3)).toFixed(1) + '" height="' + bh.toFixed(1) + '" fill="' + (i ? 'none' : 'rgba(253,224,71,.12)') + '" stroke="#f8fafc" stroke-width="1.6" rx="1"/>';
    }).join('') + '</svg>';
  }
  function tile(r, i){
    var mark = r.kept ? '<span class="mark kept" aria-label="Kept">★</span>' : r.redo && !r.unfinished ? '<span class="mark redo">redo</span>' : '';
    var pinned = i === 0 && r.unfinished && r.progress > 0;
    return '<button type="button" class="myl-tile' + (pinned ? ' pinned' : '') + '" data-i="' + i + '" aria-label="' + esc(r.title + '. ' + r.status_text + (r.kept ? '. Kept' : '')) + '">' +
      bars(r.thumb, 100, 50) + (pinned ? '<span class="go">Keep going</span>' : '') + mark +
      '<span class="lbl">' + esc(r.label) + '</span><span class="dur">' + esc(fmtDur(r)) + '</span>' +
      (r.unfinished && r.progress > 0 ? '<span class="prog"><i style="width:' + Math.round(r.progress * 100) + '%"></i></span>' : '') + '</button>';
  }
  function render(){
    var j = state.data, body = $('mylBody');
    if(!j){ return; }
    var c = j.child, st = j.stats;
    var h = '<div class="myl-head"><div class="myl-av" aria-hidden="true">' + esc(c.avatar) + '</div><div class="myl-stats">' +
      '<div><b>' + st.lessons + '</b><span>lessons</span></div><div><b>' + st.stars + '</b><span>stars</span></div><div><b>' + st.streak + '</b><span>day streak</span></div></div></div>' +
      '<div class="myl-name">' + esc(c.nickname) + ' · ' + esc(c.level) + '</div>' +
      '<div class="myl-lock">' + ICON.lock + 'Private · only you and your parent see this</div>';
    if(j.families.length){
      h += '<div class="myl-topics" role="group" aria-label="Topics">' + j.families.map(function(f){
        return '<button type="button" class="myl-topic' + (f.mastered ? ' mastered' : '') + '" data-family="' + esc(f.family) + '" aria-pressed="' + (state.family === f.family) + '" title="' + esc(f.label + ' · ' + f.level) + '">' +
          '<i>' + bars(f.mastered ? [1, .55] : [.8, .5], 34, 22) + '</i>' + esc(f.label) + '</button>';
      }).join('') + '</div>';
    }
    h += '<div class="myl-tabs" role="tablist">' + [['all', 'All'], ['kept', 'Kept ★'], ['redo', 'To redo']].map(function(t){
      return '<button type="button" role="tab" data-which="' + t[0] + '" aria-selected="' + (state.which === t[0]) + '">' + t[1] + '</button>';
    }).join('') + '</div>';
    h += state.list.length ? '<div class="myl-grid">' + state.list.map(tile).join('') + '</div>'
      : '<p class="myl-empty">' + (st.lessons ? 'Nothing here yet.' : 'No lessons yet. Solve a problem and it shows up here, ready to replay.') + '</p>';
    body.innerHTML = h;
  }
  function load(){
    var q = '?which=' + state.which + '&tz=' + new Date().getTimezoneOffset() + (state.family ? '&family=' + encodeURIComponent(state.family) : '');
    return api('GET', '/api/lessons' + q).then(function(j){ state.data = j; state.list = j.lessons; render(); }).catch(function(e){
      state.data = null;
      $('mylBody').innerHTML = '<p class="myl-empty">' + (e.status === 401 ? '🔒 Lessons are saved only when you pick your name on a family device. Ask a parent to sign in, then tap your name.' : esc(e.message)) + '</p>';
    });
  }
  function openGrid(){ grid.hidden = false; document.body.style.overflow = 'hidden'; load(); $('mylClose').focus({ preventScroll:true }); }
  function closeGrid(){ grid.hidden = true; document.body.style.overflow = ''; }
  document.addEventListener('click', function(e){ if(e.target.closest && e.target.closest('.myl-open')) openGrid(); });
  $('mylClose').addEventListener('click', closeGrid);
  $('mylBody').addEventListener('click', function(e){
    var b = e.target.closest('button'); if(!b) return;
    if(b.dataset.which){ state.which = b.dataset.which; load(); }
    else if(b.dataset.family){ state.family = state.family === b.dataset.family ? null : b.dataset.family; load(); }
    else if(b.dataset.i != null){ play(parseInt(b.dataset.i, 10)); }
  });

  // ---------------------------------------------------------------- cinema replay
  function chapterMarks(spec){  // the first step of each chapter (Read, Draw, Think, Answer, Check), monotonic
    var tl = (spec && spec.timeline) || [], at = [0, -1, -1, -1, -1], cur = 0, byStep = [];
    tl.forEach(function(s, i){
      var d = s.do, c;
      if(d === 'write_question' || d === 'highlight') c = 0;
      else if(d === 'pause') c = 2;
      else if(d === 'write' && /^\s*check/i.test(s.text || '')) c = 4;
      else if(d === 'write' || d === 'answer' || d === 'label_unit' || d === 'fill_cell' || d === 'mark_point') c = 3;
      else c = 1;
      if(c > cur){ for(var k = cur + 1; k <= c; k++) if(at[k] < 0) at[k] = i; cur = c; }
      byStep.push(cur);
    });
    for(var k = 1; k < 5; k++) if(at[k] < 0) at[k] = tl.length;
    return { at:at, byStep:byStep, n:Math.max(1, tl.length) };
  }
  function placeRail(){
    var w = lessonEl.querySelector('.board-wrap'); if(!w || !document.body.classList.contains('cinema')) return;
    var wide = window.innerWidth >= 900, r = w.getBoundingClientRect();
    rail.style.setProperty('--rail-top', Math.round(wide ? r.top + 8 : Math.min(r.bottom + lessonEl.scrollTop + 12, window.innerHeight - 330)) + 'px');
  }
  window.addEventListener('resize', placeRail);
  var marks = null;
  function setProgress(index, chapter){
    var pct = Math.min(100, Math.round((index + 1) / marks.n * 100));
    $('cineProg').firstChild.style.width = pct + '%'; $('cineProg').setAttribute('aria-valuenow', pct);
    Array.prototype.forEach.call($('cineChap').children, function(s, k){ s.classList.toggle('on', k === chapter); });
  }
  function drawMarks(){
    var p = $('cineProg'); Array.prototype.slice.call(p.querySelectorAll('b')).forEach(function(b){ p.removeChild(b); });
    marks.at.slice(1).forEach(function(i){ if(i > 0 && i < marks.n){ var b = document.createElement('b'); b.style.left = (i / marks.n * 100) + '%'; p.appendChild(b); } });
  }
  function info(r, twin){
    $('cineTitle').textContent = twin ? '✏️ One like this' : r.title;
    $('cineMeta').textContent = twin ? 'New numbers, same method · checked by code' : r.family_label + ' · ' + r.level + ' · ' + r.status_text.toLowerCase();
    $('cineN').textContent = twin || state.at < 0 ? '' : (state.at + 1) + ' of ' + state.list.length;
    $('cinePrev').disabled = twin || state.at <= 0; $('cineNext').disabled = twin || state.at >= state.list.length - 1;
    state.keep = { id:r ? r.id : null, kept:!!(r && r.kept) };
    $('cineKeep').setAttribute('aria-pressed', state.keep.kept ? 'true' : 'false');
    $('cineKeep').disabled = !state.keep.id;
  }
  function play(i){
    var r = state.list[i]; if(!r) return;
    state.at = i;
    api('GET', '/api/lessons/' + r.id).then(function(j){
      state.current = j; state.playing = j.spec;
      document.body.classList.add('cinema');
      marks = chapterMarks(j.spec); drawMarks(); setProgress(-1, 0); info(j, false); showSpeed();
      LessonAPI.start(j.spec, { solved:j.solved, lesson_id:j.id });
      $('lesson').scrollTop = 0; placeRail();
    }).catch(function(e){ alert(e.message); });
  }
  function closeCinema(){
    if(!document.body.classList.contains('cinema')) return;
    document.body.classList.remove('cinema'); LessonAPI.stop(); state.playing = null;
    if(!grid.hidden) load();
  }
  $('cineClose').addEventListener('click', closeCinema);
  function move(d){ if(!document.body.classList.contains('cinema') || !state.playing) return; var i = state.at + d; if(i >= 0 && i < state.list.length) play(i); }
  $('cinePrev').addEventListener('click', function(){ move(-1); });
  $('cineNext').addEventListener('click', function(){ move(1); });
  document.addEventListener('keydown', function(e){
    if(!document.body.classList.contains('cinema') || /INPUT|TEXTAREA|SELECT/.test((e.target || {}).tagName || '')) return;
    if(e.key === 'Escape') closeCinema(); else if(e.key === 'ArrowDown') move(1); else if(e.key === 'ArrowUp') move(-1);
  });
  var touch = null;  // swipe up = next, down = previous, on the board only (the rest of the screen scrolls)
  lessonEl.addEventListener('touchstart', function(e){ if(e.target.closest('.board-wrap') && e.touches.length === 1) touch = { x:e.touches[0].clientX, y:e.touches[0].clientY }; }, { passive:true });
  lessonEl.addEventListener('touchend', function(e){
    if(!touch || !document.body.classList.contains('cinema')) return;
    var t = e.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y; touch = null;
    if(Math.abs(dy) > 60 && Math.abs(dy) > 1.5 * Math.abs(dx)) move(dy < 0 ? 1 : -1);
  }, { passive:true });

  $('cineReplay').addEventListener('click', function(){
    var j = state.current; if(!j) return;
    state.playing = j.spec; marks = chapterMarks(j.spec); drawMarks(); setProgress(-1, 0); info(j, false);
    LessonAPI.start(j.spec, { solved:j.solved, lesson_id:j.id });
  });
  $('cineTwin').addEventListener('click', function(){  // the same twins flow as "Try one like this" (agent/twins.py)
    var j = state.current, spec = LessonAPI.spec(); if(!j || !spec) return;
    if(spec.version !== 2){ $('cineTwinLbl').textContent = 'v2 only'; return; }
    var btn = this; btn.disabled = true; $('cineTwinLbl').textContent = 'Making…';
    api('POST', '/api/twin', { spec:spec, n:0 }).then(function(t){
      if(!t.available){ $('cineTwinLbl').textContent = 'Not yet'; return; }
      $('cineTwinLbl').textContent = 'Twin';
      state.playing = t.spec;
      marks = chapterMarks(t.spec); drawMarks(); setProgress(-1, 0); info(null, true);
      LessonAPI.start(t.spec, { solved:t.solved, check:t.check, twin:{ after_miss:false } });
    }).catch(function(){ $('cineTwinLbl').textContent = 'Not yet'; }).finally(function(){ btn.disabled = false; });
  });
  $('cineKeep').addEventListener('click', function(){
    var k = state.keep; if(!k || !k.id) return;
    var kept = !k.kept;
    $('cineKeep').setAttribute('aria-pressed', kept ? 'true' : 'false');
    api('POST', '/api/lessons/' + k.id + '/keep', { kept:kept }).then(function(r){
      k.kept = r.kept;
      if(state.current && state.current.id === k.id) state.current.kept = r.kept;
      var row = state.list[state.at]; if(row && row.id === k.id) row.kept = r.kept;
    }).catch(function(){ $('cineKeep').setAttribute('aria-pressed', kept ? 'false' : 'true'); });
  });
  // Speed: the lesson's own 1–2× control (#speedBox), cycled from the rail.
  function speedButtons(){ return Array.prototype.slice.call(document.querySelectorAll('#speedBox button')); }
  function showSpeed(){
    var on = speedButtons().filter(function(b){ return b.getAttribute('aria-pressed') === 'true'; })[0];
    $('cineSpeed').textContent = on ? (on.dataset.rate === 'auto' ? 'Auto' : on.dataset.rate + '×') : '1×';
  }
  $('cineSpeed').addEventListener('click', function(){
    var bs = speedButtons(), i = bs.findIndex(function(b){ return b.getAttribute('aria-pressed') === 'true'; });
    if(bs.length){ bs[(i + 1) % bs.length].click(); showSpeed(); }
  });
  var sb = $('speedBox'); if(sb) sb.addEventListener('click', function(){ setTimeout(showSpeed, 0); });

  // Chapter progress from the lesson's own events; a twin started from the end screen shows as one too.
  document.addEventListener('bmt:lesson-start', function(e){
    if(!document.body.classList.contains('cinema')) return;
    var s = e.detail && e.detail.spec;
    if(s && s !== state.playing){ state.playing = s; marks = chapterMarks(s); drawMarks(); setProgress(-1, 0); info(null, true); }
  });
  document.addEventListener('bmt:lesson-event', function(e){
    if(!document.body.classList.contains('cinema') || !marks) return;
    var ev = e.detail || {};
    if(ev.type === 'lesson') setTimeout(placeRail, 0);  // the stars row has just been drawn above the board
    if(ev.type === 'lesson' && ev.lesson_id && state.keep && !state.keep.id){ state.keep.id = ev.lesson_id; $('cineKeep').disabled = false; }
    else if(ev.type === 'step') setProgress(ev.index, marks.byStep[ev.index] || 0);
    else if(ev.type === 'ask'){
      setProgress(ev.index - 1, ev.key === 'challenge' ? 2 : Math.max(2, marks.byStep[ev.index] || 0));
      setTimeout(function(){ var c = lessonEl.querySelector('.controls'); if(c) c.scrollIntoView({ block:'nearest' }); }, 50);  // the answer box, above the footer
    }
    else if(ev.type === 'recap' || ev.type === 'end') setProgress(marks.n - 1, 4);
  });
})();
