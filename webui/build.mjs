/**
 * DataClean 前端离线构建脚本
 *
 * 作用：把 Vue 3（含运行时模板编译器）与 Naive UI 打包为单个 IIFE，
 * 产物写入 ../static/vendor/naive-vue.js，浏览器直接 <script src> 加载。
 *
 * 关键约束：
 * - 运行时完全离线：所有依赖打进 bundle，不引用任何 CDN；
 * - Naive UI 采用 CSS-in-JS，样式由 JS 运行时注入 <style>，无需单独 CSS 文件；
 * - 只在开发/维护阶段需要 Node + npm，最终用户运行 Web 服务无需 Node。
 */
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const here = path.dirname(fileURLToPath(import.meta.url));
const outfile = path.resolve(here, "../static/vendor/naive-vue.js");
fs.mkdirSync(path.dirname(outfile), { recursive: true });

const result = await build({
  entryPoints: [path.resolve(here, "entry.js")],
  bundle: true,
  write: false,
  format: "iife",
  globalName: "DataCleanUI",
  // 统一到带运行时编译器的 Vue 构建：页面可用 template 字符串编写，
  // Naive UI 内部对 vue 的引用也会走同一份实例，避免双实例问题。
  alias: { vue: "vue/dist/vue.esm-bundler.js" },
  target: ["es2020"],
  minify: true,
  legalComments: "none",
  define: {
    "process.env.NODE_ENV": '"production"',
    __VUE_OPTIONS_API__: "true",
    __VUE_PROD_DEVTOOLS__: "false",
    __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: "false",
  },
  logLevel: "info",
});

const bytes = result.outputFiles[0].contents;
fs.writeFileSync(outfile, bytes);
console.log(`built ${outfile} (${(bytes.length / 1024).toFixed(1)} KiB)`);
