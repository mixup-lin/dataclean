/*
 * DataClean Vue 渐进增强层
 * ------------------------------------------------------------------
 * 设计原则：
 * 1) 服务端 SSR 骨架始终完整输出（无 JS / 测试契约 100% 保留）；
 * 2) 本脚本在浏览器端读取 SSR DOM 的值与 HTML 片段作为初始数据，
 *    挂载 Vue 3 + Naive UI 组件树，渐进接管「视觉与交互」；
 * 3) 提交仍走原生 form POST（同字段名 / 同路由 / 同 enctype），
 *    后端契约完全不变；等待层与 302 跳转由浏览器原生处理；
 * 4) 任一步失败自动回退 SSR 骨架（fail-safe）。
 *
 * 全局依赖：window.DataCleanUI（naive-vue.js 构建产物）。
 */
(function () {
  "use strict";
  var U = window.DataCleanUI;
  if (!U || !U.createApp) { return; }

  var createApp = U.createApp, ref = U.ref, reactive = U.reactive;

  // ---------- 主题：品牌蓝 + 靛蓝，与既有视觉语言对齐 ----------
  var themeOverrides = {
    common: {
      primaryColor: "#2563EB", primaryColorHover: "#1D4ED8",
      primaryColorPressed: "#1E40AF", primaryColorSuppl: "#3B82F6",
      infoColor: "#2563EB", successColor: "#16A34A",
      warningColor: "#D97706", errorColor: "#DC2626",
      borderRadius: "8px", borderRadiusSmall: "5px",
      fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif',
      fontSize: "13px",
    },
    Card: { borderRadius: "12px" },
    Button: { fontWeight: "600" },
  };

  // ---------- DOM 工具：从 SSR 骨架读取初始数据 ----------
  function q(sel, root) { return (root || document).querySelector(sel); }
  function qa(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function val(name, root) {
    var el = q('[name="' + name + '"]', root || document);
    return el ? (el.value || "") : "";
  }
  function checked(name, root) {
    return qa('[name="' + name + '"]', root || document)
      .filter(function (e) { return e.checked; })
      .map(function (e) { return e.value; });
  }
  function inner(sel, root) {
    var el = q(sel, root || document);
    return el ? el.innerHTML : "";
  }
  function outer(sel, root) {
    var el = q(sel, root || document);
    return el ? el.outerHTML : "";
  }
  function multiHtml(sel, root) {
    return qa(sel, root || document).map(function (e) { return e.outerHTML; }).join("");
  }
  function decodeQuery(href, key) {
    try {
      var qs = (href || "").split("?")[1] || "";
      var parts = qs.split("&");
      for (var i = 0; i < parts.length; i++) {
        var kv = parts[i].split("=");
        if (decodeURIComponent(kv[0]) === key) { return decodeURIComponent(kv[1] || ""); }
      }
    } catch (e) { /* ignore */ }
    return "";
  }

  // ---------- 提交：复用原生 POST（保持路由、字段名、multipart） ----------
  function submitWith(action, fields) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = action;
    form.style.display = "none";
    Object.keys(fields || {}).forEach(function (key) {
      var v = fields[key];
      // 与原生 form 一致：空字段也要提交（如 rule_json 为空时后端需给出校验错误）
      if (v === undefined || v === null) { return; }
      var values = Array.isArray(v) ? v : [v];
      if (!values.length) { values = [""]; }
      values.forEach(function (item) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = key;
        input.value = item;
        form.appendChild(input);
      });
    });
    document.body.appendChild(form);
    form.submit();
  }

  // ---------- 通用组件 ----------
  var AlertBox = {
    props: { type: { type: String, default: "info" }, title: String, html: String, text: String },
    template:
      '<n-alert :type="type" :show-icon="true" :bordered="true" style="margin:10px 0">' +
      '<template #header><span v-if="title">{{ title }}</span></template>' +
      '<div v-if="html" v-html="html"></div><div v-else>{{ text }}</div></n-alert>',
  };
  var HtmlCard = {
    props: { title: String, html: String, hint: String },
    template:
      '<n-card size="small" :bordered="true" style="margin-top:14px">' +
      '<template #header><div class="dc-v-title">{{ title }}</div>' +
      '<div v-if="hint" class="dc-v-hint">{{ hint }}</div></template>' +
      '<div v-if="html" class="dc-v-fragment" v-html="html"></div>' +
      '<div v-else class="dc-v-hint">暂无内容</div></n-card>',
  };
  var shared = { AlertBox: AlertBox, HtmlCard: HtmlCard };

  // ---------- 工作台 ----------
  var Workbench = {
    components: shared,
    setup: function () {
      var planForm = q('form[action="/plan"]');
      var execForm = q('form[action="/execute"]');
      var state = reactive({
        intent: val("intent", planForm),
        source_path: val("source_path", planForm) || "lots.csv",
        source_format: val("source_format", planForm) || "csv",
        columns: val("columns", planForm),
        column_aliases: val("column_aliases", planForm),
        saved_rule_id: val("saved_rule_id", planForm),
        rule_json: val("rule_json", execForm),
        rule_name: val("rule_name", execForm),
        formats: checked("formats", planForm).length ? checked("formats", planForm) : ["csv"],
        report: true,
      });
      var fr = reactive({
        chips: qa(".chips .chip").map(function (e) {
          return { text: e.textContent.trim(), intent: decodeQuery(e.getAttribute("href"), "intent") };
        }),
        errors: inner(".card .alert.error"),
        meta: "", schema: "", preview: "", contract: "", rules: "", runs: "",
      });
      qa(".card").forEach(function (card) {
        var h2 = q("h2", card);
        var title = h2 ? h2.textContent.trim() : "";
        if (title.indexOf("规则 JSON") >= 0) {
          fr.meta = multiHtml(".alert, .schema-panel, .task-badge", card);
          var det = q("details.fold", card);
          fr.preview = det ? det.outerHTML : "";
        } else if (title.indexOf("规则库") >= 0) {
          fr.rules = card.innerHTML;
        } else if (title.indexOf("最近执行记录") >= 0) {
          fr.runs = card.innerHTML;
        }
      });
      fr.schema = outer(".schema-panel");
      fr.contract = multiHtml(".contract");

      var fmtOptions = ["csv", "json", "excel", "sqlite"].map(function (v) {
        return { label: v, value: v };
      });
      var exportOptions = ["csv", "xlsx", "json"];
      function base() {
        return {
          intent: state.intent, source_path: state.source_path,
          source_format: state.source_format, columns: state.columns,
          column_aliases: state.column_aliases, saved_rule_id: state.saved_rule_id,
          generation_path: "schema", formats: state.formats,
          report: state.report ? "1" : "",
        };
      }
      function plan() { submitWith("/plan", base()); }
      function run() { submitWith("/run", base()); }
      function execute() { submitWith("/execute", { rule_json: state.rule_json }); }
      function saveRule() {
        submitWith("/rules/save", {
          rule_json: state.rule_json, rule_name: state.rule_name, intent: state.intent,
        });
      }
      function applyChip(text, intent) { if (intent) { state.intent = intent; } }
      return {
        state: state, fr: fr, fmtOptions: fmtOptions, exportOptions: exportOptions,
        plan: plan, run: run, execute: execute, saveRule: saveRule, applyChip: applyChip,
      };
    },
    template: [
      '<div class="dc-v-workbench"><n-config-provider :theme-overrides="theme">',
      '<div class="dc-v-hero"><div class="dc-v-kicker">NATURAL LANGUAGE → RULES → EXECUTION</div>',
      '<h1>自然语言数据处理工作台</h1>',
      '<p>用一句话描述统计、清洗与整理需求，自动生成可查看、可编辑、可校验的规则，并产出结果、图表与报告。</p></div>',
      '<n-steps :current="1" size="small" style="margin:6px 0 16px">',
      '<n-step title="输入需求" description="自然语言 + 数据源"/>',
      '<n-step title="生成规则" description="LLM → Schema → 规则 JSON"/>',
      '<n-step title="校验执行" description="白名单 → 管道"/>',
      '<n-step title="导出查看" description="结果 + 报告"/></n-steps>',
      '<div class="dc-v-grid">',
      '<n-card size="small" title="需求输入" :bordered="true">',
      '<AlertBox v-if="fr.errors" type="error" title="校验未通过" :html="fr.errors"/>',
      '<n-form label-placement="top" size="small">',
      '<n-form-item label="自然语言需求描述">',
      '<n-input v-model:value="state.intent" type="textarea" :rows="4" placeholder="例如：按小区关联维表后统计成交套数并排名"/></n-form-item>',
      '<div class="dc-v-chips"><n-tag v-for="c in fr.chips" :key="c.text" size="small" type="info" :bordered="true" checkable @click="applyChip(c.text, c.intent)">{{ c.text }}</n-tag></div>',
      '<div class="dc-v-row">',
      '<n-form-item label="主表路径"><n-input v-model:value="state.source_path" placeholder="lots.csv"/></n-form-item>',
      '<n-form-item label="主表格式"><n-select v-model:value="state.source_format" :options="fmtOptions" size="small"/></n-form-item>',
      '</div>',
      '<n-form-item label="可用列名（逗号分隔，留空自动探测）"><n-input v-model:value="state.columns" placeholder="留空自动探测"/></n-form-item>',
      '<n-form-item label="列名映射（中文描述 = 真实列名，每行一条）"><n-input v-model:value="state.column_aliases" type="textarea" :rows="3" placeholder="地区 = city&#10;结束时间 = end_time"/></n-form-item>',
      '<n-form-item label="导出格式"><n-checkbox-group v-model:value="state.formats"><n-space>',
      '<n-checkbox v-for="f in exportOptions" :key="f" :value="f">导出 {{ f.toUpperCase() }}</n-checkbox>',
      '</n-space></n-checkbox-group></n-form-item>',
      '</n-form>',
      '<div class="dc-v-actions"><n-button type="primary" @click="plan">生成规则 JSON</n-button>',
      '<n-button @click="run">生成并立即执行</n-button>',
      '<n-button quaternary tag="a" href="/jobs">后台任务</n-button></div>',
      '</n-card>',
      '<n-card size="small" title="规则 JSON（可查看 / 可编辑）" :bordered="true">',
      '<div v-if="fr.meta" class="dc-v-fragment" v-html="fr.meta"></div>',
      '<div v-if="fr.schema" class="dc-v-fragment" v-html="fr.schema"></div>',
      '<n-form label-placement="top" size="small">',
      '<n-form-item label="规则编辑器"><n-input v-model:value="state.rule_json" type="textarea" :rows="14" class="dc-v-code" placeholder="点击「生成规则 JSON」后在此显示"/></n-form-item>',
      '<n-form-item label="保存为可复用规则"><n-input v-model:value="state.rule_name" placeholder="例如：福州2026年各小区条数统计"/></n-form-item>',
      '</n-form>',
      '<div class="dc-v-actions"><n-button type="primary" @click="execute">校验并执行规则</n-button>',
      '<n-button @click="saveRule">保存到规则库</n-button></div>',
      '<div v-if="fr.preview" class="dc-v-fragment" v-html="fr.preview"></div>',
      '</n-card></div>',
      '<HtmlCard title="规则库（保存 / 管理 / 直接执行）" :html="fr.rules"/>',
      '<HtmlCard title="最近执行记录" :html="fr.runs"/>',
      '<HtmlCard v-if="fr.contract" title="环节契约" :html="fr.contract"/>',
      '</n-config-provider></div>',
    ].join("\n"),
  };

  // ---------- 任务结果 ----------
  var Result = {
    components: shared,
    setup: function () {
      var hero = q(".hero") || document;
      var h1 = q("h1", hero), p = q(".hero p", hero);
      var panels = multiHtml(".panel-flow, .panel-body");
      if (!panels) { panels = inner(".grid"); }
      return {
        title: h1 ? h1.textContent.trim() : "执行结果",
        conclusion: p ? p.textContent.trim() : "",
        panels: panels,
        metrics: multiHtml(".metrics .kpi, .metric"),
        warnings: multiHtml(".alert.warn, .alert.error"),
        artifacts: multiHtml(".artifacts a, .artifacts button"),
        contract: multiHtml(".contract"),
      };
    },
    template: [
      '<div class="dc-v-result"><n-config-provider :theme-overrides="theme">',
      '<div class="dc-v-hero"><div class="dc-v-kicker">TASK RESULT</div>',
      '<h1>{{ title }}</h1><p>{{ conclusion }}</p></div>',
      '<AlertBox v-if="warnings" type="warning" :html="warnings"/>',
      '<div v-if="metrics" class="dc-v-metrics" v-html="metrics"></div>',
      '<HtmlCard v-if="panels" title="分析结果" :html="panels"/>',
      '<HtmlCard title="导出与下载"><div class="dc-v-artifacts" v-html="artifacts || \'<span class=dc-v-hint>暂无产物</span>\'"></div></HtmlCard>',
      '<HtmlCard v-if="contract" title="环节契约" :html="contract"/>',
      '</n-config-provider></div>',
    ].join("\n"),
  };

  // ---------- 文档抽取 ----------
  var Extract = {
    components: shared,
    setup: function () {
      var form = q('form[action="/extract"]');
      var ecoEl = q('[name="eco"]', form);
      var state = reactive({
        doc_path: val("doc_path", form), local_doc: val("local_doc", form),
        schema_text: val("schema_text", form), mode: val("mode", form) || "sync",
        eco: !!(ecoEl && ecoEl.checked),
        eco_window: val("eco_window", form) || "400",
        eco_max_snippets: val("eco_max_snippets", form) || "20",
        eco_max_chars: val("eco_max_chars", form) || "12000",
        workers: val("workers", form) || "4",
        export_formats: checked("export_formats", form).length ? checked("export_formats", form) : ["csv"],
      });
      var errors = inner(".card .alert.error");
      var resultHtml = "";
      qa(".card").forEach(function (card) {
        var h2 = q("h2", card);
        if (h2 && h2.textContent.trim().indexOf("抽取结果") >= 0) { resultHtml = card.innerHTML; }
      });
      function submit() {
        submitWith("/extract", {
          schema_text: state.schema_text, doc_path: state.doc_path,
          local_doc: state.local_doc, mode: state.mode,
          eco: state.eco ? "1" : "", eco_window: state.eco_window,
          eco_max_snippets: state.eco_max_snippets, eco_max_chars: state.eco_max_chars,
          workers: state.workers, export_formats: state.export_formats,
        });
      }
      return {
        state: state, errors: errors, resultHtml: resultHtml,
        contract: multiHtml(".contract"), submit: submit,
        exportOptions: ["csv", "xlsx", "json"],
      };
    },
    template: [
      '<div class="dc-v-extract"><n-config-provider :theme-overrides="theme">',
      '<div class="dc-v-hero"><div class="dc-v-kicker">DOCUMENT → STRUCTURED OUTPUT</div>',
      '<h1>文档结构化数据抽取</h1>',
      '<p>从 PDF / DOCX / TXT 与图片扫描件中提取结构化字段与表格，支持批量、同步 / 异步与节能模式。</p></div>',
      '<div class="dc-v-grid">',
      '<n-card size="small" title="文档输入" :bordered="true">',
      '<AlertBox v-if="errors" type="error" title="抽取失败" :html="errors"/>',
      '<n-form label-placement="top" size="small">',
      '<n-form-item label="文档路径"><n-input v-model:value="state.doc_path" placeholder="_doc_samples/contract.txt"/></n-form-item>',
      '<n-form-item label="字段定义 Schema"><n-input v-model:value="state.schema_text" type="textarea" :rows="7" class="dc-v-code"/></n-form-item>',
      '<n-form-item label="导出格式"><n-checkbox-group v-model:value="state.export_formats"><n-space>',
      '<n-checkbox v-for="f in exportOptions" :key="f" :value="f">{{ f.toUpperCase() }}</n-checkbox>',
      '</n-space></n-checkbox-group></n-form-item>',
      '<n-form-item label="处理方式"><n-radio-group v-model:value="state.mode" size="small">',
      '<n-radio-button value="sync">同步</n-radio-button><n-radio-button value="async">异步</n-radio-button>',
      '</n-radio-group></n-form-item>',
      '</n-form>',
      '<div class="dc-v-actions"><n-button type="primary" @click="submit">执行结构化抽取</n-button>',
      '<n-button quaternary tag="a" href="/jobs">查看后台任务</n-button></div>',
      '</n-card>',
      '<n-card size="small" title="抽取结果" :bordered="true">',
      '<div v-if="resultHtml" class="dc-v-fragment" v-html="resultHtml"></div>',
      '<div v-else class="dc-v-hint">尚无抽取结果</div></n-card></div>',
      '<HtmlCard v-if="contract" title="环节契约" :html="contract"/>',
      '</n-config-provider></div>',
    ].join("\n"),
  };

  // ---------- 任务列表 / 进度 ----------
  var Generic = {
    setup: function () {
      var hero = q(".hero") || document;
      var h1 = q("h1", hero), p = q(".hero p", hero);
      return {
        title: h1 ? h1.textContent.trim() : "任务",
        desc: p ? p.textContent.trim() : "",
        mainHtml: q("#main") ? q("#main").innerHTML : "",
      };
    },
    template: [
      '<div class="dc-v-generic"><n-config-provider :theme-overrides="theme">',
      '<div class="dc-v-hero"><div class="dc-v-kicker">ASYNC JOB</div>',
      '<h1>{{ title }}</h1><p>{{ desc }}</p></div>',
      '<n-card size="small" :bordered="true"><div class="dc-v-fragment" v-html="mainHtml"></div></n-card>',
      '</n-config-provider></div>',
    ].join("\n"),
  };

  var pageMap = {
    workbench: Workbench, result: Result, extract: Extract,
    jobs: Generic, job: Generic, report: Generic,
  };

  // ---------- 挂载（组件/指令全量本地注册，避免template解析差异） ----------
  function mount() {
    var page = document.body.getAttribute("data-dc-page") || "workbench";
    var Comp = pageMap[page] || Workbench;
    var root = document.createElement("div");
    root.id = "dc-vue-root";
    document.body.insertBefore(root, document.body.firstChild);
    try {
      var app = createApp(Comp);
      app.config.globalProperties.theme = themeOverrides;
      var comps = {
        NConfigProvider: U.NConfigProvider, NCard: U.NCard, NAlert: U.NAlert,
        NButton: U.NButton, NInput: U.NInput, NSelect: U.NSelect,
        NCheckbox: U.NCheckbox, NCheckboxGroup: U.NCheckboxGroup,
        NRadioGroup: U.NRadioGroup, NRadioButton: U.NRadioButton,
        NSteps: U.NSteps, NStep: U.NStep, NTag: U.NTag, NSpace: U.NSpace,
        NForm: U.NForm, NFormItem: U.NFormItem,
      };
      Object.keys(comps).forEach(function (k) { app.component(k, comps[k]); });
      app.mount(root);
      document.body.classList.add("dc-vue-ready");
    } catch (err) {
      if (root.parentNode) { root.parentNode.removeChild(root); }
      if (window.console) {
        console.warn("DataClean Vue mount failed, falling back to SSR:", err);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
