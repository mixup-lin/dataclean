/**
 * jsdom 挂载冒烟测试：用真实 SSR HTML + 本地 bundle 验证 Vue/Naive UI 能成功挂载。
 * 只用于开发验证，不作为项目运行时依赖。
 */
import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const bundle = readFileSync(path.join(root, "static/vendor/naive-vue.js"), "utf8");
const app = readFileSync(path.join(root, "static/vendor/app.js"), "utf8");

// 从 webapp 生成真实 SSR 页面（通过子进程调用，避免 ESM/CJS 混用）
import { execFileSync } from "node:child_process";
const pagesJson = execFileSync("python", [path.join(here, "dump-pages.py")], {
  encoding: "utf8", cwd: root, maxBuffer: 20 * 1024 * 1024,
});
const pages = JSON.parse(pagesJson);

let failures = 0;
for (const [name, html] of Object.entries(pages)) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push("jsdomError: " + e.message));
  vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));
  vc.on("warn", (...a) => { /* 捕获 mount fallback 警告 */ errors.push("console.warn: " + a.join(" ")); });

  const dom = new JSDOM(html, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "http://127.0.0.1/",
    virtualConsole: vc,
  });
  const { window } = dom;
  try {
    window.eval(bundle);
    window.eval(app);
    // 触发 DOMContentLoaded（jsdom outside-only 下 defer 脚本不自动跑）
    window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 300));

    const ready = window.document.body.classList.contains("dc-vue-ready");
    const vueRoot = window.document.getElementById("dc-vue-root");
    const childCount = vueRoot ? vueRoot.children.length : 0;
    const hasNaive = vueRoot && /n-input|n-card|dc-v-hero/.test(vueRoot.innerHTML);
    const ok = ready && childCount > 0 && hasNaive && errors.length === 0;
    console.log(`[${ok ? "OK" : "FAIL"}] ${name}: ready=${ready} children=${childCount} naive=${!!hasNaive} errors=${errors.length}`);
    if (!ok) {
      failures++;
      errors.slice(0, 4).forEach((e) => console.log("   ", e.slice(0, 300)));
    }
  } catch (e) {
    failures++;
    console.log(`[FAIL] ${name}: threw ${e.message}`);
  } finally {
    window.close();
  }
}
console.log(`\n${Object.keys(pages).length - failures}/${Object.keys(pages).length} mounted`);
process.exit(failures ? 1 : 0);
