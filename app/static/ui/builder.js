/* "Build it yourself": the board task builder (agent/board_task.py). It takes the board's place while the child
   builds, then hands back to the real board, which draws the teacher's version (the right one, or the reveal).

   Ops, as the server's "ask" event sends them in "board":
     units  set each bar's (or part's) units: + / – steppers, or drag along the bar (it snaps to whole units)
     mark   put the "?" on the part the question asks for: tap a part, or drag the "?" chip onto it
     move   drag a piece from the bar that gave it to the bar that got it (or tap the piece, then tap the bar)
     grow   tap "+ years" on each bar that gets older (template D)
   Check sends {"type":"board","op","value"}; code on the server decides. Pointer Events throughout, so mouse,
   touch and pen share one path; touch-action:none only on what can be dragged. All colours are skin tokens and
   the builder sits outside the board's SVG, so Cinema's board filter never touches it. */
(function(){
  var root = null, task = null, value = null, submit = null, sel = null, done = false;
  function $(id){ return document.getElementById(id); }
  function h(tag, cls, text){ var e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e; }
  function esc(t){ return String(t == null ? '' : t); }

  function open(t, send){
    root = $('builder'); task = t; submit = send; sel = null; done = false;
    if(t.op === 'units'){ value = {}; t.rows.forEach(function(r){ value[r.id] = 1; }); }
    else if(t.op === 'grow') value = { bars:[] };
    else value = null;
    root.hidden = false; root.className = 'builder op-' + t.op;
    document.getElementById('boardSlot').classList.add('building');
    render();
  }
  function close(){
    if(!root) return;
    root.hidden = true; root.innerHTML = ''; task = null; value = null; sel = null;
    document.getElementById('boardSlot').classList.remove('building');
  }
  function active(){ return !!task; }

  // A verdict: show what the server checked (a spoken move, or the right answer when it was revealed).
  function show(v, result){
    if(!task) return;
    if(v){
      if(task.op === 'units'){ value = {}; task.rows.forEach(function(r){ value[r.id] = v[r.id] || 1; }); }
      else if(task.op === 'mark'){
        var row = task.rows.filter(function(r){ return r.id === v.bar; })[0], sg = v.segments;
        value = sg && row && row.segments.length > 1 && sg[0] === 0 && sg[1] === row.segments.length - 1 ? { bar:v.bar, whole:true }
              : { bar:v.bar, segment:v.segment != null ? v.segment : (sg ? sg[0] : null) };
      }
      else if(task.op === 'grow') value = { bars:(v.bars || []).slice() };
      else value = { from:v.from, to:v.to };
    }
    done = result === 'correct' || result === 'revealed';
    root.classList.toggle('ok', result === 'correct');
    root.classList.toggle('shown', result === 'revealed');
    root.classList.toggle('miss', result === 'try_again');
    render();
  }

  // ---------------------------------------------------------------- drawing
  function render(){
    root.innerHTML = '';
    var head = h('div', 'b-head');
    var tip = { units:'Tap + and –, or drag along a bar.', mark:'Tap the part where the ? goes.',
                move:'Drag the piece to the bar that got it.', grow:'Tap + years on each bar that gets older.' }[task.op];
    if(task.op === 'mark'){  // one row: the "?" chip and its tip, beside the Question button
      var chip = h('span', 'b-chip' + (value && value.bar ? ' placed' : ''), '?');
      chip.setAttribute('aria-hidden', 'true');
      if(!done) dragChip(chip);
      head.appendChild(chip);
      tip = value && value.bar ? 'Moved it? Tap another brace.' : 'Drag me onto a brace, or tap one.';
    }
    head.appendChild(h('span', 'b-tip', task.op === 'units' && task.parts ? 'The bar for ' + esc(task.bar) + ': ' + tip : tip));
    var q = h('button', 'b-q', '📷 Question'); q.type = 'button'; q.setAttribute('data-sheet', 'sheet-question');
    head.appendChild(q);
    root.appendChild(head);
    var rows = h('div', 'b-rows');
    if(task.op === 'units') unitRows(rows); else segRows(rows);
    root.appendChild(rows);
    var foot = h('div', 'b-foot');
    if(task.op === 'move') foot.appendChild(h('span', 'b-tip', moveText()));
    var go = h('button', 'btn b-check', done ? '✓' : 'Check'); go.type = 'button'; go.id = 'builderCheck';
    go.disabled = done || !ready();
    go.addEventListener('click', check);
    foot.appendChild(go);
    root.appendChild(foot);
  }
  function ready(){
    if(task.op === 'mark') return !!(value && value.bar);
    if(task.op === 'move') return !!(value && value.from && value.to);
    if(task.op === 'grow') return value.bars.length > 0;
    return true;
  }
  function label(r){ return r.label + (r.stage ? ' (' + r.stage.toLowerCase() + ')' : ''); }

  function unitRows(rows){
    var most = Math.max(5, Math.max.apply(null, task.rows.map(function(r){ return value[r.id]; })) + 1);
    most = Math.min(most, task.max || 12);
    task.rows.forEach(function(r){
      var row = h('div', 'b-row'), n = value[r.id];
      row.appendChild(h('span', 'b-label', r.label));
      var track = h('div', 'b-track'); track.dataset.row = r.id;
      track.setAttribute('role', 'slider'); track.setAttribute('aria-label', r.label + ': units');
      track.setAttribute('aria-valuemin', 1); track.setAttribute('aria-valuemax', task.max || 12); track.setAttribute('aria-valuenow', n);
      track.tabIndex = 0;
      var bar = h('div', 'b-bar' + (r.style === 'marked' ? ' marked' : ''));
      bar.style.width = (n / most * 100) + '%';
      for(var i = 0; i < n; i++) bar.appendChild(h('i'));
      track.appendChild(bar);
      var edge = h('span', 'b-edge'); edge.style.left = (n / most * 100) + '%'; track.appendChild(edge);
      if(!done){ dragUnits(track, r.id, most); keys(track, r.id); }
      row.appendChild(track);
      var step = h('div', 'b-step');
      var minus = h('button', 'b-pm', '–'); minus.type = 'button'; minus.setAttribute('aria-label', r.label + ': one unit less');
      var plus = h('button', 'b-pm', '+'); plus.type = 'button'; plus.setAttribute('aria-label', r.label + ': one unit more');
      minus.disabled = done || n <= 1; plus.disabled = done || n >= (task.max || 12);
      minus.addEventListener('click', function(){ setUnits(r.id, value[r.id] - 1); });
      plus.addEventListener('click', function(){ setUnits(r.id, value[r.id] + 1); });
      step.appendChild(minus); step.appendChild(h('b', 'b-n', n)); step.appendChild(plus);
      row.appendChild(step);
      rows.appendChild(row);
    });
  }
  function setUnits(id, n){
    n = Math.max(1, Math.min(task.max || 12, n));
    if(value[id] === n) return;
    value[id] = n; root.classList.remove('miss'); render();
    var t = root.querySelector('.b-track[data-row="' + id + '"]'); if(t && document.activeElement === document.body) t.focus({ preventScroll:true });
  }
  function keys(track, id){
    track.addEventListener('keydown', function(e){
      if(e.key === 'ArrowUp' || e.key === 'ArrowRight'){ e.preventDefault(); setUnits(id, value[id] + 1); refocus(id); }
      else if(e.key === 'ArrowDown' || e.key === 'ArrowLeft'){ e.preventDefault(); setUnits(id, value[id] - 1); refocus(id); }
      else if(e.key === 'Enter'){ e.preventDefault(); check(); }
    });
  }
  function refocus(id){ var t = root.querySelector('.b-track[data-row="' + id + '"]'); if(t) t.focus({ preventScroll:true }); }
  // Drag along the bar: its right edge follows the finger and snaps to whole units.
  function dragUnits(track, id, most){
    track.addEventListener('pointerdown', function(e){
      e.preventDefault();
      track.setPointerCapture(e.pointerId);
      var box = track.getBoundingClientRect(), live = most;
      var at = function(ev){ return Math.round((ev.clientX - box.left) / box.width * live); };
      var move = function(ev){
        var n = Math.max(1, Math.min(task.max || 12, at(ev)));
        if(n === value[id]) return;
        value[id] = n; root.classList.remove('miss');
        var bar = track.querySelector('.b-bar'), edge = track.querySelector('.b-edge');
        bar.innerHTML = ''; for(var i = 0; i < n; i++) bar.appendChild(h('i'));
        bar.style.width = Math.min(100, n / live * 100) + '%'; edge.style.left = Math.min(100, n / live * 100) + '%';
        track.parentNode.querySelector('.b-n').textContent = n;
      };
      move(e);
      var up = function(){ track.removeEventListener('pointermove', move); track.removeEventListener('pointerup', up); track.removeEventListener('pointercancel', up); render(); };
      track.addEventListener('pointermove', move); track.addEventListener('pointerup', up); track.addEventListener('pointercancel', up);
    });
  }

  // Bars with their real parts (sized by the solved values), for mark / move / grow.
  function segRows(rows){
    var widest = Math.max.apply(null, task.rows.map(function(r){ return r.segments.reduce(function(t, s){ return t + s.w; }, 0); }));
    var lastStage = null;
    task.rows.forEach(function(r){
      if(r.stage && r.stage !== lastStage){ rows.appendChild(h('div', 'b-stage', r.stage)); lastStage = r.stage; }
      var row = h('div', 'b-row'); row.dataset.bar = r.id;
      row.appendChild(h('span', 'b-label', r.label));
      var track = h('div', 'b-track'); track.dataset.bar = r.id;
      var bar = h('div', 'b-segs');
      var total = r.segments.reduce(function(t, s){ return t + s.w; }, 0);
      bar.style.width = (total / widest * 100) + '%';
      r.segments.forEach(function(s, i){
        var seg = h('button', 'b-seg ' + (s.style || 'normal'), s.label || '');
        seg.type = 'button'; seg.style.flexGrow = s.w; seg.dataset.bar = r.id; seg.dataset.seg = i;
        seg.setAttribute('aria-label', label(r) + (s.label ? ', ' + s.label : ', part ' + (i + 1)));
        if(s.units > 1 && (s.style || 'normal') === 'normal'){ seg.classList.add('b-units'); seg.style.setProperty('--n', s.units); }
        if(task.op === 'mark' && value && value.bar === r.id && (value.whole || value.segment === i)) seg.classList.add('picked');
        if(task.op === 'move' && value && value.from === r.id && sel === i) seg.classList.add('lifted');
        if(!done){
          if(task.op === 'mark') seg.addEventListener('click', function(){ value = { bar:r.id, segment:i }; root.classList.remove('miss'); render(); });
          if(task.op === 'move') dragPiece(seg, r.id, i);
        }
        bar.appendChild(seg);
      });
      if(task.op === 'grow' && value.bars.indexOf(r.id) >= 0) bar.appendChild(h('span', 'b-seg added grown', '+ years'));
      if(task.op === 'move' && value && value.to === r.id) bar.appendChild(h('span', 'b-seg added grown', '+'));
      if(task.op === 'mark'){ track.classList.add('b-marktrack'); track.appendChild(braces(r, total / widest * 100)); }
      track.appendChild(bar);
      if(task.op === 'mark' && r.segments.length > 1) track.appendChild(braces(r, total / widest * 100, true));
      row.appendChild(track);
      if(task.op === 'grow'){
        var on = value.bars.indexOf(r.id) >= 0;
        var b = h('button', 'b-pm b-grow', on ? '✓ years' : '+ years'); b.type = 'button';
        b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.disabled = done;
        b.addEventListener('click', function(){
          var k = value.bars.indexOf(r.id); if(k >= 0) value.bars.splice(k, 1); else value.bars.push(r.id);
          root.classList.remove('miss'); render();
        });
        row.appendChild(b);
      }
      if(task.op === 'move' && !done) row.addEventListener('click', function(e){
        if(e.target.closest('.b-seg')) return;  // pieces answer their own taps (dragPiece)
        if(value && value.from && value.from !== r.id){ value.to = r.id; root.classList.remove('miss'); render(); }
      });
      rows.appendChild(row);
    });
  }
  // Mark: a "}" over each part (and one under the whole bar when it has parts), as the teacher draws them. The "?"
  // sits on the brace you pick, so a part of 3 units is marked as a whole, not on its last unit.
  function braces(r, width, whole){
    var strip = h('div', 'b-braces' + (whole ? ' whole' : '')); strip.style.width = width + '%';
    (whole ? [null] : r.segments).forEach(function(s, i){
      var on = value && value.bar === r.id && (whole ? value.whole : (!value.whole && value.segment === i));
      var b = h('button', 'b-brace' + (on ? ' picked' : ''));
      if(on) b.appendChild(h('span', 'b-qmark', '?'));
      b.type = 'button'; b.dataset.bar = r.id;
      if(whole){ b.dataset.whole = '1'; b.style.flexGrow = 1; } else { b.dataset.seg = i; b.style.flexGrow = s.w; }
      b.setAttribute('aria-label', '? on ' + label(r) + (whole ? ', the whole bar' : (s.label ? ', ' + s.label : ', part ' + (i + 1))));
      if(!done) b.addEventListener('click', function(){ value = whole ? { bar:r.id, whole:true } : { bar:r.id, segment:i }; root.classList.remove('miss'); render(); });
      strip.appendChild(b);
    });
    return strip;
  }
  function moveText(){
    if(!value || !value.from) return 'Tap or drag a piece.';
    var name = function(id){ var r = task.rows.filter(function(x){ return x.id === id; })[0]; return r ? r.label : ''; };
    return value.to ? name(value.from) + ' → ' + name(value.to) : 'From ' + name(value.from) + ' to…?';
  }
  // A piece: tap it (then tap the bar that got it), or drag it there. One pointer path for mouse, touch and pen.
  function dragPiece(seg, bid, i){
    seg.style.touchAction = 'none';
    seg.addEventListener('pointerdown', function(e){
      e.preventDefault();
      var x0 = e.clientX, y0 = e.clientY, ghost = null;
      seg.setPointerCapture(e.pointerId);
      var move = function(ev){
        if(!ghost && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 8) return;
        if(!ghost){ ghost = h('span', 'b-ghost', seg.textContent || '◼'); document.body.appendChild(ghost); }
        ghost.style.left = ev.clientX + 'px'; ghost.style.top = ev.clientY + 'px';
      };
      var up = function(ev){
        seg.removeEventListener('pointermove', move); seg.removeEventListener('pointerup', up); seg.removeEventListener('pointercancel', up);
        if(ghost){
          ghost.remove();
          var under = document.elementFromPoint(ev.clientX, ev.clientY), row = under && under.closest ? under.closest('.b-row') : null;
          if(row && row.dataset.bar && row.dataset.bar !== bid){ value = { from:bid, to:row.dataset.bar }; sel = i; root.classList.remove('miss'); render(); }
          return;
        }
        if(value && value.from && value.from !== bid && !value.to) value.to = bid;  // a tap on the bar that got it
        else { value = { from:bid, to:null }; sel = i; }  // a tap: the piece is lifted, now tap a bar
        root.classList.remove('miss'); render();
      };
      seg.addEventListener('pointermove', move); seg.addEventListener('pointerup', up); seg.addEventListener('pointercancel', up);
    });
  }
  // The "?" chip: drag it onto a part (a tap on a part does the same).
  function dragChip(chip){
    chip.style.touchAction = 'none';
    chip.addEventListener('pointerdown', function(e){
      e.preventDefault();
      chip.setPointerCapture(e.pointerId);
      var ghost = h('span', 'b-ghost b-chip', '?'); document.body.appendChild(ghost);
      var move = function(ev){ ghost.style.left = ev.clientX + 'px'; ghost.style.top = ev.clientY + 'px'; };
      move(e);
      var up = function(ev){
        chip.removeEventListener('pointermove', move); chip.removeEventListener('pointerup', up); chip.removeEventListener('pointercancel', up);
        ghost.remove();
        var under = document.elementFromPoint(ev.clientX, ev.clientY), seg = under && under.closest ? under.closest('.b-seg, .b-brace') : null;
        if(seg && seg.dataset.whole){ value = { bar:seg.dataset.bar, whole:true }; root.classList.remove('miss'); render(); }
        else if(seg && seg.dataset.seg != null){ value = { bar:seg.dataset.bar, segment:+seg.dataset.seg }; root.classList.remove('miss'); render(); }
      };
      chip.addEventListener('pointermove', move); chip.addEventListener('pointerup', up); chip.addEventListener('pointercancel', up);
    });
  }

  function check(){
    if(!task || done || !ready()) return;
    var v = task.op === 'move' ? { from:value.from, to:value.to } : value;
    submit({ type:'board', op:task.op, value:v });
  }

  window.Builder = { open:open, close:close, show:show, active:active };
})();
