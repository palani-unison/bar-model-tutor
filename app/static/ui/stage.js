/* One focused stage at a time (index.html), skins, and bottom sheets. Loaded before the page's own scripts.

   Stages: landing (not signed in) -> start (snap first) -> capture (camera / photo / type / example) -> confirm
   (photo: question number, Start) -> lesson (the board fills the screen; the end is a sheet over it). "family"
   holds parent onboarding and the children panel. Each stage change is a history entry, so the browser's back
   button walks back through them; entering a lesson replaces the capture/confirm entry, so back from a lesson
   goes to start. Other scripts hook in with Stage.onLeave(stage, fn) and listen for "bmt:stage".
   Skins: four sets of colour tokens (ui/app.css) on <html data-skin>, remembered per device. */
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var STAGES = { landing:'stage-landing', start:'stage-start', capture:'stage-capture', confirm:'stage-confirm', family:'stage-family', lesson:'lesson' };
  var cur = null, sub = null, signedIn = false, leave = {}, depth = 0;
  function get(k){ try { return localStorage.getItem(k); } catch(e){ return null; } }
  function put(k, v){ try { localStorage.setItem(k, v); } catch(e){} }

  // ---------------------------------------------------------------- stages
  function home(){ return signedIn ? 'start' : 'landing'; }
  function render(){
    Object.keys(STAGES).forEach(function(s){ var el = $(STAGES[s]); if(el) el.hidden = s !== cur; });
    document.body.dataset.stage = cur;
    if(sub) document.body.dataset.sub = sub; else delete document.body.dataset.sub;
    closeSheets();
    window.scrollTo(0, 0);
  }
  // go(name, {sub, replace, pop}): sub is the capture source (camera / photo / type / example)
  function go(name, opts){
    opts = opts || {};
    if(!STAGES[name]) name = home();
    var nsub = opts.sub || null;
    if(name === cur && nsub === sub){ if(!opts.pop) history.replaceState({ bmt:name, sub:nsub, depth:depth }, ''); return; }
    var prev = cur;
    if(prev && prev !== name && leave[prev]) leave[prev].forEach(function(f){ try { f(name); } catch(e){} });
    cur = name; sub = nsub;
    if(!opts.pop){
      var replace = opts.replace || !prev || (name === 'lesson' && (prev === 'capture' || prev === 'confirm'));
      if(replace) history.replaceState({ bmt:name, sub:nsub, depth:depth }, '');
      else { depth += 1; history.pushState({ bmt:name, sub:nsub, depth:depth }, ''); }
    } else depth = (history.state && history.state.depth) || 0;
    render();
    document.dispatchEvent(new CustomEvent('bmt:stage', { detail:{ stage:name, sub:nsub, from:prev } }));
  }
  window.addEventListener('popstate', function(e){
    if(document.querySelector('.board-wrap.zoomed')){ unzoom(); }
    var st = e.state && e.state.bmt;
    if(st === 'lesson' && cur !== 'lesson') st = home();  // a finished lesson can't be stepped back into
    if(cur === 'lesson' && (st === 'capture' || st === 'confirm')){ go(home(), { replace:true }); return; }  // back from a lesson: start again
    if(st && st !== 'landing' && st !== 'lesson' && !signedIn && st !== 'capture') st = 'landing';
    go(st || home(), { pop:true, sub:e.state && e.state.sub });
  });
  function back(){
    if(depth > 0 && history.state && history.state.bmt) history.back();
    else go(home(), { replace:true });
  }

  // The account script tells us who is here. Signing in moves landing -> start; signing out goes back to landing.
  function account(me){
    var was = signedIn;
    signedIn = !!(me && (me.parent || me.child));
    if(!cur){ go(home(), { replace:true }); return; }  // the first answer from /api/me opens the first stage
    if(signedIn && cur === 'landing') go('start', { replace:true });
    else if(!signedIn && was && (cur === 'start' || cur === 'family' || cur === 'confirm')) go('landing', { replace:true });
  }

  // ---------------------------------------------------------------- sheets
  function closeSheets(){
    Array.prototype.forEach.call(document.querySelectorAll('.sheet'), function(s){ s.hidden = true; });
    var b = $('sheetBack'); if(b) b.hidden = true;
  }
  function sheet(id, open){
    var s = $(id); if(!s) return;
    if(open === undefined) open = s.hidden;
    closeSheets();
    if(!open) return;
    s.hidden = false; $('sheetBack').hidden = false;
    var f = s.querySelector('[data-autofocus]') || s.querySelector('button'); if(f) f.focus({ preventScroll:true });
    document.dispatchEvent(new CustomEvent('bmt:sheet', { detail:{ id:id } }));
  }
  document.addEventListener('keydown', function(e){
    if(e.key !== 'Escape') return;
    if(document.querySelector('.board-wrap.zoomed')) return unzoom();
    if(document.querySelector('.sheet:not([hidden])')) closeSheets();
  });

  // ---------------------------------------------------------------- the board, full screen on a tap
  function unzoom(){
    var w = document.querySelector('.board-wrap.zoomed'); if(w) w.classList.remove('zoomed');
    var b = $('zoomBack'); if(b) b.hidden = true;
  }
  function toggleZoom(w){
    if(w.classList.contains('zoomed')) return unzoom();
    if(document.body.classList.contains('cinema')) return;  // the replay has its own swipe gestures on the board
    w.classList.add('zoomed'); $('zoomBack').hidden = false;
  }

  // ---------------------------------------------------------------- voice first: phones and tablets talk, the keyboard is optional
  var coarse = window.matchMedia ? matchMedia('(pointer: coarse)') : null;
  function voiceFirst(){
    var on = !!(coarse && coarse.matches) || window.innerWidth < 700;
    document.body.classList.toggle('voice-first', on);
    return on;
  }
  window.addEventListener('resize', voiceFirst);

  // ---------------------------------------------------------------- skins
  var SKINS = [
    { id:'notebook', name:'Notebook', theme:'#e9ebe3', sw:['#e9ebe3', '#fbfaf5', '#1f2a44'] },
    { id:'cinema', name:'Cinema night', theme:'#0b0d12', sw:['#0b0d12', '#11241b', '#fbbf24'] },
    { id:'play', name:'Bright play', theme:'#dbeafe', sw:['#dbeafe', '#fde047', '#86efac'] },
    { id:'ocean', name:'Ocean calm', theme:'#ecfeff', sw:['#ecfeff', '#ffffff', '#0f766e'] }
  ];
  var FONTS = { cinema:'Nunito:wght@500;700;800', ocean:'Nunito:wght@500;700;800', play:'Baloo+2:wght@500;700;800' };
  function font(id){  // Google Fonts only for the skins that need them, once each
    var fam = FONTS[id]; if(!fam || document.querySelector('link[data-font="' + fam + '"]')) return;
    var l = document.createElement('link'); l.rel = 'stylesheet'; l.dataset.font = fam;
    l.href = 'https://fonts.googleapis.com/css2?family=' + fam + '&display=swap'; document.head.appendChild(l);
  }
  function skin(){ return document.documentElement.dataset.skin || 'notebook'; }
  function setSkin(id, save){
    var s = SKINS.filter(function(x){ return x.id === id; })[0] || SKINS[0];
    document.documentElement.dataset.skin = s.id; font(s.id);
    var m = document.querySelector('meta[name="theme-color"]'); if(m) m.setAttribute('content', s.theme);
    if(save !== false) put('bmt.skin', s.id);
    Array.prototype.forEach.call(document.querySelectorAll('[data-skin-pick]'), function(b){ b.setAttribute('aria-pressed', b.dataset.skinPick === s.id ? 'true' : 'false'); });
  }
  function picker(){
    return '<div class="skins" role="group" aria-label="Look">' + SKINS.map(function(s){
      return '<button type="button" data-skin-pick="' + s.id + '" aria-pressed="' + (s.id === skin()) + '"><span class="sw" style="background:' + s.sw[0] + '">' +
        '<i style="background:' + s.sw[1] + '"></i><i style="background:' + s.sw[2] + '"></i></span>' + s.name + '</button>';
    }).join('') + '</div>';
  }
  document.addEventListener('click', function(e){
    var t = e.target.closest ? e.target.closest('[data-skin-pick]') : null;
    if(t){ setSkin(t.dataset.skinPick); return; }
    t = e.target.closest ? e.target.closest('[data-sheet]') : null;
    if(t){ sheet(t.dataset.sheet); return; }
    t = e.target.closest ? e.target.closest('[data-close-sheet], #sheetBack') : null;
    if(t){ closeSheets(); return; }
    t = e.target.closest ? e.target.closest('.board-wrap, #zoomBack') : null;
    if(t && cur === 'lesson'){ toggleZoom(t.id === 'zoomBack' ? document.querySelector('.board-wrap.zoomed') || t : t); }
  });

  // ---------------------------------------------------------------- boot
  function boot(){
    setSkin(get('bmt.skin') || 'notebook', false);
    voiceFirst();
    Array.prototype.forEach.call(document.querySelectorAll('[data-skins]'), function(el){ el.innerHTML = picker(); });
    $('backBtn').addEventListener('click', back);
    setTimeout(function(){ if(!cur) go(home(), { replace:true }); }, 4000);  // /api/me never answered
  }

  window.Stage = {
    go:go, back:back, home:home, current:function(){ return cur; }, sub:function(){ return sub; },
    account:account, signedIn:function(){ return signedIn; },
    onLeave:function(stage, fn){ (leave[stage] = leave[stage] || []).push(fn); },
    sheet:sheet, closeSheets:closeSheets, unzoom:unzoom,
    voiceFirst:voiceFirst, skins:SKINS, skin:skin, setSkin:setSkin, skinPicker:picker
  };
  boot();
})();
