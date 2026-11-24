/**
 * 验证 Vue 交互：点击按钮后生成的原生 POST form 是否含正确 action / 字段名。
 */
import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const bundle = readFileSync(path.join(root, "static/vendor/naive-vue.js"), "utf8");
const app = readFileSync(path.join(root, "static/vendor/app.js"), "utf8");
const pages = JSON.parse(execFileSync("python", [path.join(here, "dump-pages.py")], {
  encoding: "utf8", cwd: root, maxBuffer: 20 * 1024 * 1024,
}));

const vc = new VirtualConsole();
const errors = [];
vc.on("jsdomError", (e) => errors.push(e.message));
vc.on("warn", (...a) => errors.push(a.join(" ")));
const dom = new JSDOM(pages["/"], {
  runScripts: "outside-only", pretendToBeVisual: true,
  url: "http://127.0.0.1/", virtualConsole: vc,
});
const { window } = dom;
// 拦截 form.submit，记录提交内容而不真正导航
const submits = [];
window.HTMLFormElement.prototype.submit = function () {
  const fields = {};
  Array.from(this.elements).forEach((el) => {
    if (el.name) { fields[el.name] = el.value; }
  });
  submits.push({ action: this.action, method: this.method, fields });
};
window.eval(bundle);
window.eval(app);
window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
await new Promise((r) => setTimeout(r, 250));

const doc = window.document;
const buttons = Array.from(doc.querySelectorAll("#dc-vue-root button"));
console.log("buttons:", buttons.map((b) => b.textContent.trim()).filter(Boolean));
// 意图字段名：找到 textarea 填入
const intentTa = doc.querySelector("#dc-vue-root textarea");
if (intentTa) {
  intentTa.value = "测试需求";
  intentTa.dispatchEvent(new window.Event("input", { bubbles: true }));
}
function clickByText(txt) {
  const b = buttons.find((x) => x.textContent.trim().indexOf(txt) >= 0);
  if (b) { b.dispatchEvent(new window.MouseEvent("click", { bubbles: true })); return true; }
  return false;
}

let pass = 0, total = 0;
function check(label, cond) {
  total++; if (cond) { pass++; console.log("  OK", label); } else { console.log("  FAIL", label); }
}

clickByText("生成规则 JSON");
check("plan action", submits.length >= 1 && submits[0].action.endsWith("/plan"));
check("plan intent", submits.length >= 1 && "intent" in submits[0].fields);
check("plan method post", submits.length >= 1 && submits[0].method === "post");

clickByText("生成并立即执行");
check("run action", submits.length >= 2 && submits[1].action.endsWith("/run"));

clickByText("校验并执行规则");
check("execute action + rule_json", submits.length >= 3 &&
  submits[2].action.endsWith("/execute") && "rule_json" in submits[2].fields);

clickByText("保存到规则库");
check("save action + fields", submits.length >= 4 &&
  submits[3].action.endsWith("/rules/save") &&
  "rule_name" in submits[3].fields && "rule_json" in submits[3].fields);

console.log(`\n${pass}/${total} checks passed; jsdom errors=${errors.length}`);
if (errors.length) { errors.slice(0, 3).forEach((e) => console.log("  ERR", e.slice(0, 200))); }
process.exit(pass === total && errors.length === 0 ? 0 : 1);
