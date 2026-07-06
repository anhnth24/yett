/* yett DS — loader dự phòng.
   Nạp các component .jsx trực tiếp (fetch + Babel transform) và expose lên window.YettDS.
   Khi project được compile thành design system thật, consumer nên dùng _ds_bundle.js;
   loader này để guidelines/ui_kits chạy standalone. Yêu cầu: React + Babel standalone đã load trước. */
(function () {
  var script = document.currentScript;
  var root = script.src.replace(/ds-support\/loader\.js.*$/, '');
  var FILES = [
    'components/core/Button.jsx',
    'components/core/Card.jsx',
    'components/core/Badge.jsx',
    'components/core/Tag.jsx',
    'components/forms/Input.jsx',
    'components/forms/Select.jsx',
    'components/forms/Switch.jsx',
    'components/navigation/Tabs.jsx',
    'components/feedback/ApprovalBanner.jsx',
  ];
  window.__dsReady = (async function () {
    var ns = (window.YettDS = window.YettDS || {});
    var mods = {};
    for (var i = 0; i < FILES.length; i++) {
      var f = FILES[i];
      var res = await fetch(root + f);
      if (!res.ok) throw new Error('YettDS loader: fetch failed ' + res.status + ' — ' + f);
      var src = await res.text();
      var code = Babel.transform(src, { presets: [['env', { modules: 'commonjs' }], ['react', { runtime: 'classic' }]] }).code;
      var module = { exports: {} };
      var req = (function (from) {
        return function (p) {
          if (p === 'react') return window.React;
          var dir = from.split('/').slice(0, -1);
          var parts = p.split('/');
          for (var j = 0; j < parts.length; j++) {
            if (parts[j] === '.') continue;
            else if (parts[j] === '..') dir.pop();
            else dir.push(parts[j]);
          }
          return mods[dir.join('/')];
        };
      })(f);
      try {
        new Function('module', 'exports', 'require', code)(module, module.exports, req);
      } catch (e) {
        console.error('YettDS loader: eval failed in ' + f + '\n' + (e && (e.stack || e)));
        throw e;
      }
      mods[f] = module.exports;
      Object.assign(ns, module.exports);
    }
    return ns;
  })();
  window.__dsReady.catch(function (e) {
    console.error('YettDS loader failed:', e && (e.stack || e));
  });
})();
