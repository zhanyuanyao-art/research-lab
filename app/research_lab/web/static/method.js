// Literature map: cytoscape rendering, fixed columns below 8 placed works, cose above.
(function () {
  var el = document.getElementById('cy');
  if (!el || typeof cytoscape === 'undefined') return;
  var slug = el.dataset.slug;
  var css = getComputedStyle(document.documentElement);
  function v(name, fallback) { return (css.getPropertyValue(name) || fallback).trim(); }
  var C = { fg: v('--fg', '#1f1d1a'), muted: v('--muted', '#6b665e'), rule: v('--rule', '#e2dcd0'), accent: v('--accent', '#2f5d50'),
            soft: v('--accent-soft', '#e4ede9'), card: v('--card', '#fff'), warn: v('--warn', '#b5791c'), crit: v('--critical', '#a8412f') };

  function columnPositions(els) {
    var pos = {}, col = 0, W = 200, H = 64;
    els.nodes.filter(function (n) { return n.data.kind === 'camp'; }).forEach(function (camp) {
      var row = 0;
      els.nodes.forEach(function (n) {
        if (n.data.parent === camp.data.id) { pos[n.data.id] = { x: col * W, y: 60 + row * H }; row++; }
      });
      if (!row) pos[camp.data.id] = { x: col * W, y: 60 };
      col++;
    });
    var others = els.nodes.filter(function (n) { return n.data.kind !== 'camp' && !n.data.parent; });
    others.forEach(function (n, i) { pos[n.data.id] = { x: col * W + (i % 2) * W, y: 60 + Math.floor(i / 2) * H }; });
    return pos;
  }

  fetch(el.dataset.src, { headers: { 'Accept': 'application/json' } }).then(function (r) { return r.json(); }).then(function (els) {
    var sparse = (els.placed_count || 0) < 8;
    var pos = sparse ? columnPositions(els) : null;
    var nodes = els.nodes.map(function (n) { var o = { data: n.data, classes: n.classes }; if (pos && pos[n.data.id]) o.position = pos[n.data.id]; return o; });
    if (!(els.placed_count || 0)) {
      var hint = document.createElement('div');
      hint.className = 'map-empty';
      hint.innerHTML = '<strong>还没有文献归入阵营</strong><span>在右侧把文献归入阵营，或在文献页的“我的判断”里填写阵营。放够 8 篇后会自动排成关系图。</span>';
      el.parentNode.insertBefore(hint, el);
    }
    var cy = cytoscape({
      container: el,
      elements: { nodes: nodes, edges: els.edges },
      layout: sparse ? { name: 'preset', padding: 30 } : { name: 'cose', animate: false, padding: 30, nodeRepulsion: 9000 },
      wheelSensitivity: 0.2, maxZoom: 1.6, minZoom: 0.3,
      style: [
        { selector: 'node', style: { 'label': 'data(label)', 'font-size': 12, 'font-family': '-apple-system, PingFang SC, sans-serif', 'color': C.fg, 'text-wrap': 'wrap', 'text-max-width': 160, 'padding': 8, 'shape': 'round-rectangle', 'width': 'label', 'height': 'label',
            'text-valign': 'center', 'text-halign': 'center', 'background-color': C.card, 'border-width': 1, 'border-color': C.muted,
            'shape': 'round-rectangle', 'width': 'label', 'height': 'label', 'padding': 6 } },
        { selector: 'node.camp', style: { 'text-valign': 'top', 'text-halign': 'center', 'font-weight': 600, 'background-color': C.soft,
            'border-color': C.rule, 'padding': 18, 'font-size': 13, 'text-margin-y': -6, 'color': C.fg } },
        { selector: 'node.paper.unverified', style: { 'border-style': 'dotted' } },
        { selector: 'node.claim', style: { 'shape': 'ellipse', 'border-color': C.accent } },
        { selector: 'node.claim.own', style: { 'border-width': 2 } },
        { selector: 'node.case', style: { 'shape': 'diamond', 'padding': 10 } },
        { selector: 'node.puzzle', style: { 'shape': 'star', 'border-color': C.warn, 'padding': 10 } },
        { selector: 'node.suggested', style: { 'border-style': 'dashed' } },
        { selector: 'edge', style: { 'width': 1.4, 'line-color': C.muted, 'target-arrow-color': C.muted, 'target-arrow-shape': 'triangle',
            'curve-style': 'bezier', 'label': 'data(label)', 'font-size': 8, 'color': C.muted, 'text-rotation': 'autorotate' } },
        { selector: 'edge.rel-contests', style: { 'line-color': C.crit, 'target-arrow-color': C.crit, 'source-arrow-color': C.crit,
            'source-arrow-shape': 'triangle', 'width': 2 } },
        { selector: 'edge.suggested', style: { 'line-style': 'dashed' } },
        { selector: ':selected', style: { 'border-width': 3, 'border-color': C.accent } }
      ]
    });
    cy.on('tap', 'node', function (evt) {
      var id = evt.target.id();
      if (window.htmx) htmx.ajax('GET', '/projects/' + slug + '/map/node?id=' + encodeURIComponent(id), '#node-panel');
    });
  });
})();
