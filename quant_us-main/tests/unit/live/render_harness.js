// 页面渲染桩：跑真实渲染路径，**并等异步 boot 跑完**。
// argv[0]=node, [1]=本文件, [2]=页面脚本, [3]=接口载荷
//
// 两种页面形态都要覆盖：
//   · scope 页（总览/持仓/机会/LLM作用）：同步 `window.__render(payload)` 返回 HTML
//   · boot 页（实验/定时任务/决策详情）：页面自己调用异步 `bootXxx()`，往 #content 里写
// 第一版只调 `__render`，于是 boot 类页面渲染出 0 字符 —— **看起来"通过"其实什么都没验**。
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const els = {};
const el = () => ({innerHTML: '', textContent: '', style: {}, dataset: {}, open: false,
                   querySelectorAll: () => [], querySelector: () => null,
                   addEventListener() {}});
global.document = {
  getElementById: id => (els[id] = els[id] || el()),
  querySelectorAll: () => [],
};
global.window = global;
global.location = {pathname: process.argv[4] || '/', search: ''};
global.fetch = async () => ({ok: true, status: 200, json: async () => payload});

const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src);

(async () => {
  let html = '';
  try { html = global.window.__render(payload) || ''; } catch (e) { html = ''; }
  if (!html.trim()) {
    // boot 类页面：脚本末尾已经调了 bootXxx()，等它的 promise 链落地
    for (let i = 0; i < 40; i++) {
      await new Promise(r => setTimeout(r, 25));
      const c = els['content'];
      if (c && c.innerHTML && c.innerHTML.trim() && !/加载中/.test(c.innerHTML)) {
        html = c.innerHTML; break;
      }
      if (c && c.innerHTML) html = c.innerHTML;
    }
  }
  process.stdout.write(html);
  process.exit(0);
})();
