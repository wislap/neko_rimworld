import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const repo = fileURLToPath(new URL("../../../../", import.meta.url));
const plugin = fileURLToPath(new URL("../", import.meta.url));
const front = path.join(repo, "frontend/plugin-manager");
const { Window } = await import(pathToFileURL(path.join(front, "node_modules/happy-dom/lib/index.js")).href);
const { transform } = await import(pathToFileURL(path.join(front, "node_modules/sucrase/dist/index.js")).href);
const { bundleHostedTsxSource } = await import(pathToFileURL(path.join(front, "src/components/plugin/hosted/hostedTsxModule.mjs")).href);
const runtime = await fs.readFile(path.join(front, "src/components/plugin/hosted/ui-kit/runtime.js"), "utf8");
const source = await fs.readFile(path.join(plugin, "ui/panel.tsx"), "utf8");
const messages = Object.fromEntries(await Promise.all(["zh-CN", "en"].map(async locale => [locale, JSON.parse(await fs.readFile(path.join(plugin, "locales", locale + ".json"), "utf8"))])));
const dependencies = [];
async function collect(dir) {
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const absolute = path.join(dir, entry.name);
    if (entry.isDirectory()) await collect(absolute);
    else if (/\.tsx?$/.test(entry.name) && entry.name !== "panel.tsx") dependencies.push({ path: path.relative(plugin, absolute).replaceAll("\\", "/"), source: await fs.readFile(absolute, "utf8") });
  }
}
await collect(path.join(plugin, "ui"));
const compiled = transform(bundleHostedTsxSource(source, dependencies, "ui/panel.tsx"), { transforms:["typescript","jsx"], jsxPragma:"h", jsxFragmentPragma:"Fragment", production:true }).code.replace(/export\s+default\s+function\s+([\w$]*)\s*\(/, "const __Panel = function $1(");
const guideEntry = "ui/guide/guide-page.tsx";
const guideSource = await fs.readFile(path.join(plugin,guideEntry),"utf8");
const compiledGuide = transform(bundleHostedTsxSource(guideSource, dependencies.filter(item=>item.path!==guideEntry), guideEntry), { transforms:["typescript","jsx"], jsxPragma:"h", jsxFragmentPragma:"Fragment", production:true }).code.replace(/export\s+default\s+function\s+([\w$]*)\s*\(/, "const __Panel = function $1(");
const flush = async () => { for (let i=0;i<8;i++) await Promise.resolve(); await new Promise(resolve => setTimeout(resolve, 1)); };
function setup(locale = "zh-CN", initialState = {}, surface = "panel") {
  const window = new Window({ url:"http://localhost/ui" });
  window.__NEKO_PAYLOAD={ locale, host:{origin:"http://localhost"}, i18n:{messages,default_locale:"en"} };
  const NativeText = window.Text;
  window.eval(runtime);
  window.Text = NativeText; // happy-dom constructs text through window.Text; browsers use their native DOM implementation.
  window.eval((surface === "guide" ? compiledGuide : compiled)+"\nwindow.TestPanel=__Panel;");
  const ui=window.NekoUiKit;
  const root=window.document.createElement("main");window.document.body.append(root);
  const state={ action_nonce:"test-nonce", status:"ready", port:48919, configuration:{token_configured:true,agent_configured:true,agent_api_key_configured:true,agent_base_url:"https://example.com/v1",agent_model:"test"}, agent:{active_count:0,recent_count:0,active_runs:[],recent_runs:[],latest_run:null} };
  Object.assign(state, initialState);
  const calls=[];
  let refreshes=0;
  let invoke=async (id,args) => ({ result:id === "workspace_view" ? {workspaces:[],total:0,next_offset:null} : id === "begin_settings_save" ? {operation_id:args.operation_id,kind:args.kind,state:"saved",persisted:true,applied:true} : {status:"ok"} });
  const props={ state, plugin:{id:"neko_rimworld",name:"RimWorld"}, actions:["workspace_view","rimworld_report","get_agent_run_details","rimworld_consult","probe_game_overview","save_agent_settings","save_bridge_settings","begin_settings_save","get_settings_save"].map(id=>({id,entry_id:id})), warnings:[], locale, api:{call:async(id,args)=>{calls.push({id,args});return invoke(id,args)}, refresh:async()=>{refreshes++;render()}} };
  function render(){ui.render(ui.h(window.TestPanel,props),root)}
  function button(label, selector="button") { const found=[...root.querySelectorAll(selector)].find(x=>x.textContent===label);assert.ok(found,`Missing ${label}: ${root.textContent}`);return found; }
  async function click(label,selector="button"){button(label,selector).click();await flush()}
  async function tab(label){
    if(label === "调试" || label === "主页" || label === "会话与事件") {
      const advanced=root.querySelector('#rimworld-main-nav-tab-diagnostics');
      assert.ok(advanced,"调试 must always be present in the top navigation");advanced.click();
      await flush();await click(label === "调试" ? "咨询测试" : "执行记录",'[role="tab"]');return;
    }
    await click(label,'[role="tab"]')
  }
  async function type(input,value){assert.ok(input);input.value=value;input.dispatchEvent(new window.Event("input",{bubbles:true}));await flush()}
  render();
  return {window,root,props,state,calls,render,button,click,tab,type,refreshes:()=>refreshes,setInvoke(fn){invoke=fn},async close(){ui.render(null,root);await window.happyDOM.abort();window.close()}};
}

function summary(id,question){return {run_id:id,question,state:"completed",result_status:"ok",result_summary:question+" summary",event_count:61,revision:61,terminal:true,stage:"completed",latest_event:{event_seq:61,event:"run_completed",cycle:1,elapsed_ms:1000},latest_event_summary:"Run completed.",model_calls:1,tool_calls:1,duration_ms:1000}}
function detail(id,question,offset=0){return {...summary(id,question),events:[{event_seq:offset+1,event:"model_response_received",elapsed_ms:100,data:{content:question}}],events_page:{offset,limit:50,total:61,has_more:offset===0},result:{status:"ok",answer:question+" full",summary:question+" summary",observations:[]}}}

test("the guide translates every chapter and offers public downloads without running actions",async()=>{
  for(const locale of ["zh-CN","en","zh-TW","fr"]){
    const h=setup(locale,{},"guide");try{
      await flush();
      assert.equal(h.root.querySelector('#rimworld-main-nav'),null);
      const chapters=[...h.root.querySelectorAll('section[id^="rimworld-guide-"]')];
      assert.equal(chapters.length,8);
      assert.deepEqual(h.root.textContent.match(/\bguide\.(?:title|intro|(?:prepare|mod|plugin|pair|model|chat|answers|help|link|build)\.)/g),null,locale);
      const chinese=locale.startsWith("zh");
      assert.match(h.root.textContent,chinese?/从下载安装到第一次咨询/:/From download to your first colony question/);
      assert.match(h.root.textContent,chinese?/用户插件/:/User Plugin/);
      assert.match(h.root.textContent,/127\.0\.0\.1/);
      assert.match(h.root.textContent,/48919/);
      const links=[...h.root.querySelectorAll('.neko-download')];
      assert.equal(links.length,7);
      const external=[];h.window.parent.postMessage=message=>{if(message.type==='neko-hosted-surface-open-external')external.push(message.payload.url)};
      for(const link of links)link.click();
      assert.equal(external.length,7,"sandboxed downloads must go through the host external-link handler");
      assert.ok(external.includes("https://github.com/wislap/NekoRimWorld/releases"));
      assert.ok(external.includes("https://github.com/wislap/n.e.k.o_plugin_neko_rimworld/releases"));
      assert.ok(external.every(url=>url.startsWith("https://")));
      for(const detail of h.root.querySelectorAll('details'))detail.querySelector('summary').click();
      let jumped=false;chapters[4].scrollIntoView=()=>{jumped=true};
      h.root.querySelectorAll('nav button')[4].click();await flush();
      assert.ok(jumped);assert.equal(h.window.document.activeElement,chapters[4]);
      assert.equal(h.calls.length,0,"reading and jumping through the guide must not call the backend");
    }finally{await h.close()}
  }
});

test("the guide is separate from the panel pages and never disturbs panel drafts",async()=>{
  const h=setup();const guide=setup("zh-CN",{},"guide");try{
    await flush();
    assert.equal(h.root.querySelectorAll('#rimworld-main-nav-tab-guide').length,0);
    assert.equal(h.root.querySelectorAll('[role="tablist"] [role="tab"]').length,5);
    await h.tab("设置");
    await h.type(h.root.querySelector('input[placeholder="https://example.com/v1"]'),"https://draft.example/v1");
    await h.type(h.root.querySelectorAll('input[type="password"]')[1],"draft-key");
    assert.equal(guide.root.querySelectorAll('section[id^="rimworld-guide-"]').length,8);
    h.render();await flush();
    assert.equal(h.root.querySelector('input[placeholder="https://example.com/v1"]').value,"https://draft.example/v1");
    assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"draft-key");
    assert.equal(guide.calls.length,0);
    assert.ok(h.calls.every(call=>call.id==='workspace_view'));
  }finally{await guide.close();await h.close()}
});

test("primary pages preserve drafts across navigation and server refresh",async()=>{
  const h=setup();try{
    await flush();assert.equal(h.root.querySelectorAll('input[type="password"]').length,0);
    await h.tab("设置");
    await h.type(h.root.querySelector('input[placeholder="https://example.com/v1"]'),"https://draft.example/v1");
    await h.type(h.root.querySelectorAll('input[type="password"]')[1],"temporary-test-secret");
    await h.tab("调试");await h.type(h.root.querySelector("textarea"),"unfinished question");
    await h.tab("主页");h.state.configuration.agent_base_url="https://server-change.example/v1";h.render();await flush();
    await h.tab("设置 · 未保存");
    assert.equal(h.root.querySelector('input[placeholder="https://example.com/v1"]').value,"https://draft.example/v1");
    assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"temporary-test-secret");
    await h.tab("调试");assert.equal(h.root.querySelector("textarea").value,"unfinished question");
    assert.ok(h.calls.every(x=>x.id === "workspace_view"));
  }finally{await h.close()}
});

test("running consultation survives navigation and prevents duplicate submit",async()=>{
  const h=setup();try{
    let finish;
    h.setInvoke(async(id,args)=>{
      if(id==="rimworld_consult")return await new Promise(resolve=>{finish=resolve});
      if(id==="get_agent_run_details")return {result:detail(args.run_id,"completed question",args.event_offset)};
      return {result:{status:"ok"}};
    });
    await h.tab("调试");const run=h.button("运行咨询");run.click();run.click();await flush();
    assert.equal(h.calls.filter(x=>x.id==="rimworld_consult").length,1);
    await h.tab("主页");assert.match(h.root.textContent,/正在分析/);
    finish({result:{status:"degraded",summary:"A limited result",audit:{run_id:"run-new"}}});await flush();
    await h.tab("调试");assert.match(h.root.textContent,/A limited result/);
    await h.click("打开本次会话详情");await flush();assert.match(h.root.textContent,/completed question full/);
    assert.equal(h.root.querySelector('#rimworld-main-nav-tab-diagnostics').getAttribute("aria-selected"),"true");
    assert.equal(h.calls.filter(x=>x.id==="get_agent_run_details").length,1);
  }finally{await h.close()}
});

test("degraded runs explain the provider failure and next action without opening raw events",async()=>{
  const h=setup();try{
    const failed={...summary("failed-run","Colony status"),result_status:"degraded",model_calls:4,model_failures:3,tool_calls:2,failure_summary:{
      error_code:"model_request_deadline",phase:"first_response",attempts:4,failed_attempts:3,retries:2,provider_failures:2,
      provider_error:{type:"upstream_error",message:"Our servers are currently overloaded. Please try again later."}
    }};
    h.state.agent.recent_runs=[failed];h.state.agent.latest_run=failed;h.render();await flush();
    await h.tab("会话与事件");
    assert.match(h.root.textContent,/模型服务过载/);
    assert.match(h.root.textContent,/已自动重试 2 次/);
    assert.match(h.root.textContent,/稍后重试或更换模型服务/);
    h.setInvoke(async()=>({result:{...detail("failed-run","Colony status"),...failed,result:{status:"degraded",answer:"fallback",summary:"fallback",observations:[{}]}}}));
    await h.click("查看本次详情");await flush();
    assert.match(h.root.textContent,/已取得 1 份游戏观察/);
    assert.match(h.root.textContent,/Our servers are currently overloaded/);
  }finally{await h.close()}
});

test("active runs use lightweight progress and fetch full detail once on completion",async()=>{
  const h=setup();try{
    const active={...summary("active-run","Live progress"),state:"model_requesting",result_status:null,result_summary:null,terminal:false,revision:4,event_count:4,stage:"model_requesting",latest_event:{event_seq:4,event:"model_retry_scheduled",cycle:1,elapsed_ms:2500},latest_event_summary:"Retrying the model in 0.5 seconds.",duration_ms:2500};
    h.state.agent.active_count=1;h.state.agent.active_runs=[active];h.state.agent.recent_runs=[];h.state.agent.latest_run=active;h.render();await flush();
    await h.tab("会话与事件");
    assert.match(h.root.textContent,/模型暂时没有响应，正在重试/);
    await h.tab("会话与事件");await flush();
    assert.match(h.root.textContent,/模型暂时没有响应，正在重试/);
    assert.match(h.root.textContent,/进度版本.*4/);
    assert.equal(h.calls.filter(x=>x.id==="get_agent_run_details").length,0);

    const completed={...summary("active-run","Live progress"),revision:9,event_count:9};
    h.setInvoke(async(id,args)=>id==="get_agent_run_details"?{result:{...detail(args.run_id,"Live progress",args.event_offset),revision:9,terminal:true}}:{result:{status:"ok"}});
    h.state.agent.active_count=0;h.state.agent.active_runs=[];h.state.agent.recent_runs=[completed];h.state.agent.latest_run=completed;h.render();await flush();
    assert.match(h.root.textContent,/Live progress full/);
    assert.equal(h.calls.filter(x=>x.id==="get_agent_run_details").length,1);
    h.render();await flush();
    assert.equal(h.calls.filter(x=>x.id==="get_agent_run_details").length,1);
  }finally{await h.close()}
});

test("detail refresh failure preserves the last successful detail",async()=>{
  const h=setup();try{
    const run=summary("stable-run","Stable detail");h.state.agent.recent_runs=[run];h.state.agent.latest_run=run;
    h.setInvoke(async()=>({result:detail("stable-run","Stable detail")}));h.render();await flush();await h.tab("会话与事件");await flush();
    assert.match(h.root.textContent,/Stable detail full/);
    h.setInvoke(async()=>{throw new Error("Hosted surface request timed out.")});
    await h.click("刷新详情");
    assert.match(h.root.textContent,/Stable detail full/);
    assert.match(h.root.textContent,/详情刷新暂时失败/);
    assert.match(h.root.textContent,/已保留上次成功读取的内容/);
  }finally{await h.close()}
});

test("selected historical run, event pages and expiry never switch to latest",async()=>{
  const h=setup();try{
    const first=summary("r1","Older question");const second=summary("r2","Newer question");
    h.state.agent.recent_runs=[second,first];h.state.agent.latest_run=second;h.render();await flush();
    let expired=false;
    h.setInvoke(async(id,args)=>{
      if(expired)throw new Error("This run is no longer retained");
      return {result:detail(args.run_id,args.run_id==="r1"?"Older question":"Newer question",args.event_offset)};
    });
    await h.tab("会话与事件");
    const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("Older question"));assert.ok(row);row.click();await flush();
    assert.match(h.root.textContent,/Older question full/);
    h.state.agent.latest_run=summary("r3","Newest");h.render();await flush();assert.match(h.root.textContent,/Older question full/);
    await h.tab("事件");await h.click("下一页");assert.equal(h.calls.at(-1).args.event_offset,50);
    expired=true;h.state.agent.recent_runs=[h.state.agent.latest_run];h.render();await flush();
    await h.click("刷新详情");assert.match(h.root.textContent,/no longer retained/);
    assert.doesNotMatch(h.root.textContent,/Newest full/);
  }finally{await h.close()}
});

test("budget edits are included in save and secret inputs clear only after success",async()=>{
  const h=setup();try{
    await h.tab("设置");const numbers=h.root.querySelectorAll('input[type="number"]');await h.type(numbers[4],"40");
    await h.type(h.root.querySelectorAll('input[type="password"]')[1],"temporary-key");
    await h.click("保存模型设置");
    const saved=h.calls.find(x=>x.id==="begin_settings_save");assert.equal(saved.args.budgets.request_timeout_seconds,40);
    assert.equal(saved.args.ui_nonce,"test-nonce");assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"");
    assert.match(h.root.textContent,/已保存/);
  }finally{await h.close()}
});


test("lost bridge save reply checks the same operation without another write",async()=>{
  const h=setup();try{
    h.setInvoke(async(id,args)=>{
      if(id==="begin_settings_save"){h.state.status="error";throw new Error("Hosted request timed out")}
      if(id==="get_settings_save")return {result:{operation_id:args.operation_id,kind:"bridge",state:"unconfirmed"}};
      return {result:{status:"ok"}};
    });
    await h.tab("设置");
    const port=h.root.querySelector('input[type="number"]');await h.type(port,"48920");
    await h.click("保存并应用连接设置");
    assert.equal(h.refreshes(),1);assert.equal(port.value,"48920");
    assert.match(h.root.textContent,/尚未确认保存结果/);
    assert.match(h.root.textContent,/游戏连接需要检查/);
    assert.equal(h.calls.filter(x=>x.id==="begin_settings_save").length,1);
    assert.equal(h.calls.find(x=>x.id==="get_settings_save").args.operation_id,h.calls.find(x=>x.id==="begin_settings_save").args.operation_id);
  }finally{await h.close()}
});

test("late run detail replies cannot replace a newer selection",async()=>{
  const h=setup();try{
    const first=summary("r1","First");const second=summary("r2","Second");
    h.state.agent.recent_runs=[first,second];h.state.agent.latest_run=first;h.render();await flush();
    const pending={};
    h.setInvoke((id,args)=>new Promise(resolve=>{pending[args.run_id]=resolve}));
    await h.tab("会话与事件");
    const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("Second"));row.click();await flush();
    pending.r2({result:detail("r2","Second")});await flush();
    pending.r1({result:detail("r1","First")});await flush();
    assert.match(h.root.textContent,/Second full/);assert.doesNotMatch(h.root.textContent,/First full/);
  }finally{await h.close()}
});

test("top navigation supports keyboard selection",async()=>{
  const h=setup();try{
    const tab=h.root.querySelector("#rimworld-main-nav-tab-work");tab.focus();
    tab.dispatchEvent(new h.window.KeyboardEvent("keydown",{key:"End",bubbles:true}));await flush();
    assert.equal(h.root.querySelector("#rimworld-main-nav-tab-settings").getAttribute("aria-selected"),"true");
    assert.equal(h.window.document.activeElement,h.root.querySelector("#rimworld-main-nav-tab-settings"));
    assert.ok(h.root.querySelector('input[type="password"]'));
  }finally{await h.close()}
});

test("debug remains a top-level destination from advice, history and settings",async()=>{
  const h=setup();try{
    await flush();
    for(const label of ["最近建议","历史记录","设置"]){
      await h.click(label,'[role="tab"]');
      const tab=h.root.querySelector('#rimworld-main-nav-tab-diagnostics');
      assert.equal(tab.textContent,"调试");
      assert.equal(tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]').length,5);
    }
    await h.type(h.root.querySelector('input[placeholder="https://example.com/v1"]'),"https://unsaved.example/v1");
    await h.click("调试",'[role="tab"]');
    assert.equal(h.root.querySelector('#rimworld-main-nav-tab-diagnostics').getAttribute("aria-selected"),"true");
    assert.ok(h.button("执行记录",'[role="tab"]'));
    assert.ok(h.button("原始读取记录",'[role="tab"]'));
    await h.click("咨询测试",'[role="tab"]');
    assert.ok(h.root.querySelector('textarea'));
    await h.click("设置 · 未保存",'[role="tab"]');
    assert.equal(h.root.querySelector('input[placeholder="https://example.com/v1"]').value,"https://unsaved.example/v1");
    assert.ok(h.calls.every(call=>call.id === "workspace_view"));
  }finally{await h.close()}
});

test("run budgets reject fractional counts and save independent input and request limits",async()=>{
  const h=setup();try{
    await h.tab("设置");
    const inputFor=label=>[...h.root.querySelectorAll('label')].find(node=>node.textContent.startsWith(label))?.querySelector('input');
    await h.type(inputFor("单次运行模型请求上限"),"2.5");
    assert.ok(h.button("保存模型设置").disabled);
    assert.match(h.root.textContent,/请求次数必须为整数/);
    assert.ok(h.calls.every(call=>call.id!=="begin_settings_save"));
    await h.type(inputFor("单次运行模型请求上限"),"8");
    await h.type(inputFor("单次运行累计输入上限"),"12345");
    await h.click("保存模型设置");
    const saved=h.calls.find(call=>call.id==="begin_settings_save");
    assert.equal(saved.args.budgets.max_model_calls,8);
    assert.equal(saved.args.budgets.max_input_tokens,12345);
    assert.equal(saved.args.budgets.run_budget_seconds,900);
  }finally{await h.close()}
});

test("output budget is editable and length errors display actual limits",async()=>{
  const h=setup();try{
    await h.tab("设置");
    const input=h.root.querySelectorAll('input[type="number"]')[7];
    assert.equal(input.value,"32768");await h.type(input,"65536");
    await h.click("保存模型设置");
    assert.equal(h.calls.at(-1).args.budgets.max_output_tokens,65536);
    const run={...summary("length-run","Budget failure"),result_status:"degraded",failure_summary:{error_code:"model_output_limit",finish_reason:"length",max_output_tokens:32768,usage:{completion_tokens:32768,reasoning_tokens:32000}}};
    h.state.agent.latest_run=run;h.state.agent.recent_runs=[run];h.render();await flush();
    h.setInvoke(async()=>({result:{...detail("length-run","Budget failure"),...run,result:{status:"degraded"}}}));
    await h.tab("主页");await h.click("查看本次详情");await flush();
    assert.match(h.root.textContent,/输出预算不足/);assert.match(h.root.textContent,/32768 \/ 32000/);
    assert.match(h.root.textContent,/length/);
  }finally{await h.close()}
});


test("saved reports show full body, preserve draft status and explicitly attach followups",async()=>{
  const h=setup();try{
    const id="a".repeat(32), full="完整正文。".repeat(500)+"报告末尾保留";
    h.setInvoke(async(entry,args)=>{
      if(entry==="rimworld_report" && args.action==="list")return {result:{reports:[{report_id:id,state:"draft",question:"历史调查",created_at:"2026-09-05"}],total:1,next_offset:null}};
      if(entry==="rimworld_report")return {result:{report:{report_id:id,state:"draft",question:"历史调查",full_text:full,observation_count:2}}};
      return {result:{status:"ok",summary:"followup done"}};
    });
    await h.tab("会话与事件");await h.tab("完整报告");
    const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("历史调查"));assert.ok(row);row.click();await flush();
    assert.ok(h.root.textContent.includes(id));assert.match(h.root.textContent,/报告末尾保留/);assert.match(h.root.textContent,/不能当作正式交付/);
    await h.click("复制完整正文");assert.equal(await h.window.navigator.clipboard.readText(),full);
    await h.click("基于这份报告追问");await h.type(h.root.querySelector("textarea"),"解释原报告的细节");await h.click("运行咨询");
    assert.deepEqual(JSON.parse(JSON.stringify(h.calls.find(x=>x.id==="rimworld_consult").args)),{question:`解释原报告的细节\n请分析保存的历史报告 ${id}。`});
    await h.click("取消报告引用");await h.click("运行咨询");
    assert.deepEqual(JSON.parse(JSON.stringify(h.calls.filter(x=>x.id==="rimworld_consult").at(-1).args)),{question:"解释原报告的细节"});
  }finally{await h.close()}
});

test("late report reply cannot replace a newly selected report",async()=>{
  const h=setup();try{
    const first="a".repeat(32),second="b".repeat(32),pending={};
    h.setInvoke(async(entry,args)=>{
      if(args.action==="list")return {result:{reports:[{report_id:first,question:"报告一"},{report_id:second,question:"报告二"}],total:2}};
      return await new Promise(resolve=>{pending[args.report_id]=resolve});
    });
    await h.tab("会话与事件");await h.tab("完整报告");
    [...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("报告一")).click();await flush();
    [...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("报告二")).click();await flush();
    pending[second]({result:{report:{report_id:second,full_text:"第二份正文",state:"complete"}}});await flush();
    pending[first]({result:{report:{report_id:first,full_text:"第一份正文",state:"complete"}}});await flush();
    assert.match(h.root.textContent,/第二份正文/);assert.doesNotMatch(h.root.textContent,/第一份正文/);
  }finally{await h.close()}
});

function workFixture(id, owner, question) {
  const turn = `${id}-turn`
  const workspace = {workspace_id:id,owner,colony_id:`${id}-colony`,work_state:"waiting",last_question:question,turn_count:1}
  return {workspace, history:{workspace_id:id,workspace,turns:[{turn_id:turn,sequence:1,question,status:"ok"}],total:1,next_offset:null},
    exchange:{workspace_id:id,turn_id:turn,question,status:"ok",history_saved:true,audit_available:true,
      result:{status:"ok",answer:question+" 完整结果末尾",agent_text:"旧内核摘要"},handoff:{summary:"最终宿主摘要",prepared_at:"2026-10-03"},
      evidence:[{observation_id:"source",sha256:"original-hash"}]}}
}

test("persistent work remains visible offline with no volatile runs or consultation calls",async()=>{
 const h=setup();try{
  const a=workFixture("work-a","小天","患者历史调查")
  h.setInvoke(async(id,args)=>{
   assert.equal(id,"workspace_view")
   if(args.action==="list")return {result:{workspaces:[a.workspace],total:1,next_offset:null}}
   if(args.action==="work")return {result:a.history}
   if(args.action==="turn")return {result:a.exchange}
   if(args.action==="observation")return {result:{workspace_id:"work-a",turn_id:"work-a-turn",observation_id:"source",sha256:"original-hash",observation:{response:{payload:{data:{flag:false,value:null}}}}}}
  })
  await h.tab("历史记录");await flush()
  assert.match(h.root.textContent,/游戏离线/)
  assert.match(h.root.textContent,/患者历史调查 完整结果末尾/)
  assert.match(h.root.textContent,/最终宿主摘要/)
  assert.doesNotMatch(h.root.textContent,/旧内核摘要/)
  await h.click("查看这次分析的高级诊断")
  await h.click("1. 游戏读取记录 1")
  assert.match(h.root.textContent,/original-hash/)
  assert.match(h.root.textContent,/false/)
  assert.ok(h.calls.every(c=>c.id==="workspace_view"))
 }finally{await h.close()}
})

test("late work response cannot replace another role's selected work",async()=>{
 const h=setup();try{
  const a=workFixture("a","角色甲","甲的调查"), b=workFixture("b","角色乙","乙的调查")
  let finishA
  h.setInvoke(async(id,args)=>{
   if(args.action==="list")return {result:{workspaces:[a.workspace,b.workspace],total:2,next_offset:null}}
   if(args.action==="work" && args.workspace_id==="a")return await new Promise(resolve=>{finishA=resolve})
   if(args.action==="work")return {result:b.history}
   return {result:b.exchange}
  })
  await h.tab("历史记录");await flush()
  const row=[...h.root.querySelectorAll("tr")].find(x=>x.textContent.includes("角色乙"));row.click();await flush()
  assert.match(h.root.textContent,/乙的调查 完整结果末尾/)
  finishA({result:a.history});await flush()
  assert.doesNotMatch(h.root.textContent,/甲的调查 完整结果末尾/)
  assert.match(h.root.textContent,/乙的调查 完整结果末尾/)
 }finally{await h.close()}
})

test("saved work audit opens after volatile run history has been cleared",async()=>{
 const h=setup();try{
  const a=workFixture("a","小天","持久调查")
  h.setInvoke(async(id,args)=>{
   if(args.action==="list")return {result:{workspaces:[a.workspace],total:1,next_offset:null}}
   if(args.action==="work")return {result:a.history}
   if(args.action==="turn")return {result:a.exchange}
   if(args.action==="audit")return {result:{...detail("saved-run","保存的执行审计"),workspace_id:"a",turn_id:"a-turn"}}
   throw new Error("Unexpected call")
  })
  await h.tab("历史记录");await flush()
  await h.click("查看这次分析的高级诊断")
  await h.click("查看保存的执行诊断")
  assert.match(h.root.textContent,/保存的执行审计 full/)
  assert.match(h.root.textContent,/工作中保存的执行审计/)
  assert.equal(h.calls.filter(c=>c.id==="get_agent_run_details").length,0)
 }finally{await h.close()}
})

test("work refresh failure preserves the selected historical answer and save uncertainty",async()=>{
 const h=setup();try{
  const a=workFixture("a","小天","完整但未保存")
  a.exchange.history_saved=false
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[a.workspace],total:1,next_offset:null}:args.action==="work"?a.history:a.exchange}))
  await h.tab("历史记录");await flush()
  assert.match(h.root.textContent,/保存尚未确认/)
  h.setInvoke(async()=>{throw new Error("Offline read failure")})
  await h.click("刷新记录")
  assert.match(h.root.textContent,/完整但未保存 完整结果末尾/)
  assert.match(h.root.textContent,/历史记录暂时无法读取/)
  assert.ok(h.calls.every(c=>c.id==="workspace_view"))
 }finally{await h.close()}
})

test("advice defaults to the current colony and keeps full text and raw data out of the first view",async()=>{
 const h=setup();try{
  const old=workFixture("older","小天","另一殖民地的问题"),current=workFixture("current","小天","当前殖民地的问题")
  h.state.session={colony_id:"current…ony",load_epoch:"load…1"}
  current.workspace.current_colony=true
  current.exchange.evidence[0].request={operation:"inspect_resources",arguments:{}}
  current.exchange.evidence[0].received_at="2026-10-17T08:00:00Z"
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[old.workspace,current.workspace],total:2,next_offset:null}:args.action==="work"?current.history:args.action==="turn"?current.exchange:{...current.exchange,observation_id:"source",sha256:"original-hash",observation:{flag:false,value:null}}}))
  h.render();await flush()
  assert.match(h.root.textContent,/当前殖民地的问题/)
  assert.doesNotMatch(h.root.textContent,/另一殖民地的问题|source|original-hash|准备返回宿主|原始证据/)
  assert.equal(h.root.querySelectorAll('table').length,0)
  assert.equal(h.root.querySelector('details').open,false)
  assert.match(h.root.textContent,/最终宿主摘要/)
  assert.equal(h.calls.filter(c=>c.args.action==="observation").length,0)
  h.root.querySelector('summary').click();await flush()
  assert.equal(h.root.querySelector('details').open,true)
  await h.click("查看这次分析的高级诊断")
  assert.match(h.root.textContent,/物资来源与数量/)
  await h.click("1. 物资来源与数量")
  assert.match(h.root.textContent,/original-hash/)
  assert.match(h.root.textContent,/false/)
  assert.ok(h.calls.every(c=>c.id==="workspace_view"))
 }finally{await h.close()}
})

test("a new loaded colony cannot display the previous colony as its current advice",async()=>{
 const h=setup();try{
  const a=workFixture("a","角色甲","旧殖民地建议")
  h.state.session={colony_id:"a-colony",load_epoch:"first"}
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[a.workspace],total:1,next_offset:null}:args.action==="work"?a.history:a.exchange}))
  h.render();await flush();assert.match(h.root.textContent,/旧殖民地建议/)
  h.state.session={colony_id:"new-colony",load_epoch:"second"};h.render();await flush()
  assert.doesNotMatch(h.root.textContent,/旧殖民地建议/)
  assert.match(h.root.textContent,/当前殖民地还没有讨论记录/)
 }finally{await h.close()}
})

test("operational handoffs are not shown as advice and the available full answer stays readable",async()=>{
 const h=setup();try{
  const a=workFixture("a","角色甲","未完整完成的分析")
  a.exchange.handoff={summary:"Internal operational notice",delivery:{submission_mode:"operational_fallback"}}
  a.exchange.result={status:"degraded",answer:"保留的作者正文",summary:"Internal fallback",delivery:{submission_mode:"operational_fallback"}}
  h.state.session={colony_id:"a-colony",load_epoch:"first"}
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[a.workspace],total:1,next_offset:null}:args.action==="work"?a.history:a.exchange}))
  h.render();await flush()
  assert.doesNotMatch(h.root.textContent,/Internal operational|Internal fallback/)
  assert.match(h.root.textContent,/保留的作者正文/)
  assert.equal(h.root.querySelector('details').open,true)
  assert.match(h.root.textContent,/分析未完整完成/)
 }finally{await h.close()}
})

test("a failed historical answer read keeps its bound question list available for recovery",async()=>{
 const h=setup();try{
  const a=workFixture("a","角色甲","可恢复的历史问题")
  h.setInvoke(async(id,args)=>{
   if(args.action==="list")return {result:{workspaces:[a.workspace],total:1,next_offset:null}}
   if(args.action==="work")return {result:a.history}
   throw new Error("Temporary history transport failure")
  })
  await h.tab("历史记录");await flush()
  assert.match(h.root.textContent,/讨论过的问题/)
  assert.match(h.root.textContent,/可恢复的历史问题/)
  assert.match(h.root.textContent,/历史记录暂时无法读取/)
  assert.doesNotMatch(h.root.textContent,/最终宿主摘要/)
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[a.workspace],total:1,next_offset:null}:args.action==="work"?a.history:a.exchange}))
  await h.click("刷新记录");await flush()
  assert.match(h.root.textContent,/最终宿主摘要/)
 }finally{await h.close()}
})

test("first use explains pairing, model setup and where to ask without calling either service",async()=>{
 const h=setup();try{
  h.state.configuration={token_configured:false,agent_configured:false};h.state.status="stopped";h.render();await flush()
  assert.match(h.root.textContent,/开始使用 RimWorld 建议/)
  assert.match(h.root.textContent,/配对码/);assert.match(h.root.textContent,/独立模型服务/)
  assert.match(h.root.textContent,/不会调用模型/)
  assert.ok(h.calls.every(call=>call.id==="workspace_view"))
  await h.click("打开设置");assert.ok(h.root.querySelector('input[placeholder="https://example.com/v1"]'))
  assert.equal(h.root.querySelector("details").open,false)
 }finally{await h.close()}
})

test("pending save disables duplicate writes and a manual receipt check clears a confirmed secret",async()=>{
 const h=setup();try{
  let saved=false
  h.setInvoke(async(id,args)=>({result:id==="begin_settings_save" || id==="get_settings_save" ? {operation_id:args.operation_id,kind:"agent",state:saved?"saved":"saving",persisted:saved?true:null,applied:saved} : {workspaces:[],total:0,next_offset:null}}))
  await h.tab("设置");await h.type(h.root.querySelectorAll('input[type="password"]')[1],"private-ui-key")
  h.button("保存模型设置").click();await flush()
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"private-ui-key")
  assert.match(h.root.textContent,/正在保存并确认/)
  assert.equal(h.button("正在保存，请稍候…").disabled,true)
  saved=true;await h.click("核对这次保存状态")
  assert.match(h.root.textContent,/设置已保存并应用/)
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"")
  assert.equal(h.calls.filter(call=>call.id==="begin_settings_save").length,1)
  assert.doesNotMatch(JSON.stringify(Array.from({length:h.window.localStorage.length},(_,i)=>h.window.localStorage.getItem(h.window.localStorage.key(i)))),/private-ui-key/)
 }finally{await h.close()}
})

test("a late successful receipt cannot erase a newer unsaved draft",async()=>{
 const h=setup();try{
  let pending
  h.setInvoke(async(id,args)=>{
   if(id==="begin_settings_save"){pending={operation_id:args.operation_id,kind:"agent"};return {result:{...pending,state:"unconfirmed"}}}
   if(id==="get_settings_save")return {result:{...pending,state:"saved",persisted:true,applied:true}}
   return {result:{workspaces:[],total:0,next_offset:null}}
  })
  await h.tab("设置");await h.type(h.root.querySelectorAll('input[type="password"]')[1],"first-key")
  await h.click("保存模型设置")
  await h.type(h.root.querySelector('input[placeholder="https://example.com/v1"]'),"https://new-draft.example/v1")
  await h.type(h.root.querySelectorAll('input[type="password"]')[1],"newer-key")
  h.state.settings_saves=[{...pending,state:"saved",persisted:true,applied:true}];h.render();await flush()
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"newer-key")
  assert.equal(h.root.querySelector('input[placeholder="https://example.com/v1"]').value,"https://new-draft.example/v1")
  assert.match(h.root.textContent,/上次提交的设置已保存/)
  assert.ok(h.button("设置 · 未保存",'[role="tab"]'))
 }finally{await h.close()}
})

test("remounting within the same iframe recovers a pending receipt without repeating the save",async()=>{
 const h=setup();try{
  let pending,finished=false
  h.setInvoke(async(id,args)=>{
   if(id==="begin_settings_save"){pending={operation_id:args.operation_id,kind:"agent"};return {result:{...pending,state:"saving"}}}
   if(id==="get_settings_save")return {result:{...pending,state:finished?"saved":"saving",persisted:finished?true:null,applied:finished}}
   return {result:{workspaces:[],total:0,next_offset:null}}
  })
  await h.tab("设置");await h.type(h.root.querySelectorAll('input[type="password"]')[1],"do-not-store-this-key")
  await h.click("保存模型设置")
  h.window.NekoUiKit.render(null,h.root)
  finished=true;h.render();await flush()
  assert.match(h.root.textContent,/设置已保存并应用/)
  assert.equal(h.calls.filter(call=>call.id==="begin_settings_save").length,1)
  assert.equal(h.calls.filter(call=>call.id==="get_settings_save").at(-1).args.operation_id,pending.operation_id)
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"")
 }finally{await h.close()}
})

test("a completely new iframe restores the latest save for each kind from the backend",async()=>{
 let receipt
 const first=setup();try{
  first.setInvoke(async(id,args)=>{
   if(id==="begin_settings_save") {receipt={operation_id:args.operation_id,kind:args.kind,state:"saving"};return {result:receipt}}
   return {result:{workspaces:[],total:0,next_offset:null}}
  })
  await first.tab("设置");await first.type(first.root.querySelectorAll('input[type="password"]')[1],"not-a-persistent-secret")
  await first.click("保存模型设置")
  assert.equal(first.calls.filter(c=>c.id==="begin_settings_save").length,1)
 }finally{await first.close()}
 const saved={...receipt,state:"saved",persisted:true,applied:true}
 const bridge={operation_id:"b".repeat(32),kind:"bridge",state:"failed",error_code:"bridge_start_error",persisted:false,applied:false}
 const reopened=setup("zh-CN",{settings_saves:[{...saved,operation_id:"a".repeat(32),state:"failed"},bridge,saved]});try{
  await reopened.tab("设置")
  assert.match(reopened.root.textContent,/设置已保存并应用/)
  assert.match(reopened.root.textContent,/未能使用这个端口建立连接/)
  assert.equal(reopened.root.querySelectorAll('input[type="password"]')[1].value,"")
  assert.ok(reopened.calls.every(c=>c.id!=="begin_settings_save"))
  assert.equal(reopened.window.localStorage.length,0)
  assert.equal(reopened.window.sessionStorage.length,0)
 }finally{await reopened.close()}
})

test("a new iframe follows an unfinished save and a failed save never becomes success",async()=>{
 const pending={operation_id:"c".repeat(32),kind:"agent",state:"saving",persisted:null,applied:null}
 const h=setup("zh-CN",{settings_saves:[pending]});try{
  h.setInvoke(async(id,args)=>({result:id==="get_settings_save"?{...pending,state:"failed",persisted:false,applied:false,error_code:"invalid_agent_settings"}:{workspaces:[],total:0,next_offset:null}}))
  await h.tab("设置");assert.match(h.root.textContent,/正在保存并确认/)
  await h.click("核对这次保存状态")
  assert.match(h.root.textContent,/这次设置没有应用/)
  assert.match(h.root.textContent,/模型设置未通过检查/)
  assert.doesNotMatch(h.root.textContent,/设置已保存并应用/)
  assert.equal(h.calls.find(c=>c.id==="get_settings_save").args.operation_id,pending.operation_id)
  assert.ok(h.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await h.close()}
})

test("receipts arriving after a fresh iframe mounts do not erase a new draft",async()=>{
 const h=setup();try{
  await h.tab("设置")
  await h.type(h.root.querySelector('input[placeholder="https://example.com/v1"]'),"https://new-draft.example/v1")
  await h.type(h.root.querySelectorAll('input[type="password"]')[1],"new-draft-key")
  h.state.settings_saves=[{operation_id:"d".repeat(32),kind:"agent",state:"saved",persisted:true,applied:true}]
  h.render();await flush()
  assert.match(h.root.textContent,/上次提交的设置已保存/)
  assert.equal(h.root.querySelector('input[placeholder="https://example.com/v1"]').value,"https://new-draft.example/v1")
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"new-draft-key")
  assert.ok(h.button("设置 · 未保存",'[role="tab"]'))
  assert.ok(h.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await h.close()}
})

test("a backend restart invalidates old success without losing current settings or replaying a write",async()=>{
 const saved={operation_id:"e".repeat(32),kind:"agent",state:"saved",persisted:true,applied:true}
 const h=setup("zh-CN",{settings_saves:[saved]});try{
  await h.tab("设置");assert.match(h.root.textContent,/设置已保存并应用/)
  await h.type(h.root.querySelectorAll('input[type="password"]')[1],"keep-new-draft")
  h.setInvoke(async(id)=>{if(id==="get_settings_save")throw new Error("settings_receipt_unavailable");return {result:{workspaces:[],total:0,next_offset:null}}})
  h.state.action_nonce="restarted-backend";h.state.settings_saves=[];h.state.configuration.agent_model="persisted-model"
  h.render();await flush()
  assert.match(h.root.textContent,/尚未确认保存结果/)
  assert.match(h.root.textContent,/插件重启后，上次保存记录可能无法恢复/)
  assert.doesNotMatch(h.root.textContent,/设置已保存并应用|上次提交的设置已保存/)
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"keep-new-draft")
  await h.click("核对这次保存状态")
  assert.ok(h.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await h.close()}
 const reopened=setup("zh-CN",{action_nonce:"restarted-backend",settings_saves:[],configuration:{agent_model:"persisted-model",agent_base_url:"https://example.com/v1"}});try{
  await reopened.tab("设置")
  assert.ok([...reopened.root.querySelectorAll("input")].some(input=>input.value==="persisted-model"))
  assert.doesNotMatch(reopened.root.textContent,/设置已保存并应用|这次设置没有应用/)
  assert.ok(reopened.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await reopened.close()}
})

test("a receipt reply from the previous backend cannot restore stale success after restart",async()=>{
 const pending={operation_id:"f".repeat(32),kind:"agent",state:"saving",persisted:null,applied:null}
 const h=setup("zh-CN",{settings_saves:[pending]});try{
  let finishOld
  h.setInvoke(async(id)=>id==="get_settings_save"?await new Promise(resolve=>{finishOld=resolve}):{result:{workspaces:[],total:0,next_offset:null}})
  await h.tab("设置");h.button("核对这次保存状态").click();await flush()
  assert.equal(typeof finishOld,"function")
  h.setInvoke(async(id)=>{if(id==="get_settings_save")throw new Error("settings_receipt_unavailable");return {result:{workspaces:[],total:0,next_offset:null}}})
  h.state.action_nonce="next-process";h.state.settings_saves=[];h.render();await flush()
  assert.match(h.root.textContent,/尚未确认保存结果/)
  finishOld({result:{...pending,state:"saved",persisted:true,applied:true}});await flush()
  assert.match(h.root.textContent,/尚未确认保存结果/)
  assert.doesNotMatch(h.root.textContent,/设置已保存并应用/)
  assert.ok(h.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await h.close()}
})

test("an initial save reply from the previous backend cannot replace the restart warning",async()=>{
 const h=setup();try{
  let finishSave,submitted
  h.setInvoke(async(id,args)=>{
   if(id==="begin_settings_save") {submitted=args;return await new Promise(resolve=>{finishSave=resolve})}
   if(id==="get_settings_save")throw new Error("settings_receipt_unavailable")
   return {result:{workspaces:[],total:0,next_offset:null}}
  })
  await h.tab("设置");await h.type(h.root.querySelectorAll('input[type="password"]')[1],"keep-this-key")
  h.button("保存模型设置").click();await flush()
  h.state.action_nonce="another-process";h.state.settings_saves=[];h.render();await flush()
  assert.match(h.root.textContent,/尚未确认保存结果/)
  finishSave({result:{operation_id:submitted.operation_id,kind:"agent",state:"saved",persisted:true,applied:true}});await flush()
  assert.match(h.root.textContent,/尚未确认保存结果/)
  assert.doesNotMatch(h.root.textContent,/设置已保存并应用/)
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"keep-this-key")
  assert.equal(h.calls.filter(c=>c.id==="begin_settings_save").length,1)
 }finally{await h.close()}
})

test("a saved label without persistence and activation confirmation cannot clear a key",async()=>{
 const h=setup();try{
  h.setInvoke(async(id,args)=>({result:{operation_id:args.operation_id,kind:"agent",state:"saved",persisted:null,applied:false}}))
  await h.tab("设置");await h.type(h.root.querySelectorAll('input[type="password"]')[1],"keep-this-draft")
  await h.click("保存模型设置")
  assert.match(h.root.textContent,/尚未确认保存结果/)
  assert.equal(h.root.querySelectorAll('input[type="password"]')[1].value,"keep-this-draft")
  assert.doesNotMatch(h.root.textContent,/设置已保存并应用/)
  assert.equal(h.calls.filter(call=>call.id==="begin_settings_save").length,1)
 }finally{await h.close()}
})

test("normal advice explains an incomplete answer and lets the user copy it without another consultation",async()=>{
 const h=setup();try{
  const a=workFixture("a","YUI","当前问题")
  a.exchange.result={status:"degraded",answer:"仍可阅读的完整保留文本",agent_text:"部分结论",audit:{error_code:"agent_run_budget"},report:{export:{status:"unavailable",reason:"export_failed"}}}
  a.exchange.handoff=null;a.exchange.history_saved=false
  h.state.session={colony_id:"a-colony",load_epoch:"one"}
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[a.workspace],total:1,next_offset:null}:args.action==="work"?a.history:a.exchange}))
  h.render();await flush()
  assert.match(h.root.textContent,/未能在等待时间内完成/)
  assert.match(h.root.textContent,/保存尚未确认/)
  assert.match(h.root.textContent,/没有生成可下载文件/)
  await h.click("复制完整回答");assert.equal(await h.window.navigator.clipboard.readText(),"仍可阅读的完整保留文本")
  assert.ok(h.calls.every(call=>call.id==="workspace_view"))
 }finally{await h.close()}
})

test("history searches send a bounded saved-data filter and colony choices are readable",async()=>{
 const h=setup();try{
  const a=workFixture("a","YUI","历史问题")
  h.state.session={colony_id:"a-colony",load_epoch:"one"}
  h.setInvoke(async(id,args)=>({result:args.action==="list"?{workspaces:[a.workspace],total:1,next_offset:null}:args.action==="work"?a.history:a.exchange}))
  await h.tab("历史记录")
  await h.type(h.root.querySelector('input[placeholder="输入问题中的词语或角色名"]'),"old question")
  await new Promise(resolve=>setTimeout(resolve,300));await flush()
  assert.equal(h.calls.filter(call=>call.args.action==="list").at(-1).args.search,"old question")
  await h.click("只看当前殖民地")
  assert.equal(h.calls.filter(call=>call.args.action==="list").at(-1).args.colony_only,true)
  assert.ok(h.calls.every(call=>call.id==="workspace_view"))
  assert.ok([...h.root.querySelectorAll("table button")].length>0)
 }finally{await h.close()}
})

test("history question buttons select once without bubbling into a second row selection",async()=>{
 const h=setup();try{
  const a=workFixture("a","Alpha","First discussion"),b=workFixture("b","Beta","Second discussion")
  h.setInvoke(async(id,args)=>{
   if(args.action==="list")return {result:{workspaces:[a.workspace,b.workspace],total:2,next_offset:null}}
   const selected=args.workspace_id==="b"?b:a
   return {result:args.action==="work"?selected.history:selected.exchange}
  })
  await h.tab("历史记录");await flush()
  await h.click("Second discussion")
  assert.match(h.root.textContent,/Second discussion 完整结果末尾/)
  assert.doesNotMatch(h.root.textContent,/First discussion 完整结果末尾/)
  assert.equal(h.calls.filter(call=>call.args.action==="turn"&&call.args.workspace_id==="b").length,1)
 }finally{await h.close()}
})

test("English and fallback locales render translated settings, guidance and troubleshooting",async()=>{
 for(const locale of ["en","fr-FR"]){
  const h=setup(locale);try{
   await flush();assert.match(h.root.textContent,/Get started with RimWorld advice/)
   await h.click("Settings",'[role="tab"]');assert.match(h.root.textContent,/Save model settings/)
   assert.match(h.root.textContent,/Advanced: run and model limits/)
   assert.doesNotMatch(h.root.textContent,/[\u4e00-\u9fff]|settings\.[a-z]|debug\.[a-z]/)
   await h.click("Troubleshooting",'[role="tab"]');assert.match(h.root.textContent,/Original readings/)
   assert.doesNotMatch(h.root.textContent,/[\u4e00-\u9fff]|debug\.[a-z]/)
  }finally{await h.close()}
 }
})

test("autoplay requires an explicit panel action and shows real board and safe undo", async()=>{
 const h=setup("zh-CN", {autoplay:{enabled:false,board:[{entry_id:"task-1",kind:"task",revision:3,status:"open",body:"准备食物"}],takeovers:[{seq:5,payload:{object:"pawn",field:"priority"}}],actions:[{seq:9,mode:"commit",status:"committed",changeset_id:"change",tick:30,undo_available:true}]}});try{
  h.props.actions.push({id:"autoplay_mode",entry_id:"autoplay_mode"},{id:"autoplay_undo",entry_id:"autoplay_undo"})
  h.render();await flush();assert.doesNotMatch(h.root.textContent,/task-1.*revision 3/);await h.tab("自动游玩");assert.match(h.root.textContent,/task-1.*revision 3/);assert.match(h.root.textContent,/pawn \/ priority/)
  assert.equal(h.calls.filter(c=>c.id==="autoplay_mode").length,0)
  await h.click("开启自动游玩");assert.equal(JSON.stringify(h.calls.find(c=>c.id==="autoplay_mode").args),JSON.stringify({enabled:true,goal:"Prioritize survival and stability.",constraints:"No cannibalism or live organ harvesting.",goal_edited:false,constraints_edited:false,ui_nonce:"test-nonce"}))
  await h.click("撤销");assert.equal(JSON.stringify(h.calls.find(c=>c.id==="autoplay_undo").args),JSON.stringify({seq:9,ui_nonce:"test-nonce"}))
 }finally{await h.close()}
})

test("autoplay budget keeps draft and recovers a save receipt across navigation",async()=>{
 const h=setup("zh-CN", {autoplay:{enabled:false,settings:{max_requests:123}}});try{
  let release; const pending=new Promise(resolve=>{release=resolve});
  h.setInvoke(async(id,args)=>id==="begin_settings_save" && args.kind==="autoplay" ? await pending : id==="get_settings_save" ? {result:{operation_id:args.operation_id,kind:"autoplay",state:"saved",persisted:true,applied:true}} : {result:{workspaces:[],total:0,next_offset:null}})
  h.render();await flush();await h.tab("自动游玩");const input=[...h.root.querySelectorAll("input")].find(x=>x.value==="123");await h.type(input,"124")
  h.button("保存自动游玩预算").click();await flush();const call=h.calls.find(c=>c.id==="begin_settings_save");assert.equal(call.args.budgets.max_requests,124)
  await h.tab("设置");await h.tab("自动游玩");assert.ok([...h.root.querySelectorAll("input")].some(x=>x.value==="124"))
  release({result:{operation_id:call.args.operation_id,kind:"autoplay",state:"saved",persisted:true,applied:true}});await flush()
  assert.equal(h.calls.filter(c=>c.id==="begin_settings_save").length,1)
 }finally{await h.close()}
})

test("autoplay budget clears optional limits and rejects empty required counts", async()=>{
 const h=setup("en", {autoplay:{enabled:false,settings:{max_requests:123,max_cost:8,input_price:7}}});try{
  h.render();await flush();await h.tab("Autoplay");const inputs=[...h.root.querySelectorAll('input')];
  await h.type(inputs.find(x=>x.value==='8'),'');await h.click('Save autoplay budget')
  const save=h.calls.find(c=>c.id==='begin_settings_save');assert.equal(save.args.budgets.max_cost,null)
  await h.type([...h.root.querySelectorAll('input')].find(x=>x.value==='123'),'');await h.click('Save autoplay budget')
  assert.equal(h.calls.filter(c=>c.id==='begin_settings_save').length,1)
  assert.match(h.root.textContent,/positive whole numbers/)
 }finally{await h.close()}
})

test("activation sends edited goal and untouched default constraints as choices", async()=>{
 const h=setup("en", {autoplay:{enabled:false}});try{
  h.props.actions.push({id:"autoplay_mode",entry_id:"autoplay_mode"});h.render();await flush();await h.tab("Autoplay")
  const goal=[...h.root.querySelectorAll('input')].find(x=>x.value==='Prioritize survival and stability.')
  await h.type(goal,'Build a peaceful colony.');await h.click('Enable autoplay')
  const sent=h.calls.find(c=>c.id==='autoplay_mode').args
  assert.equal(sent.goal,'Build a peaceful colony.');assert.equal(sent.goal_edited,true)
  assert.equal(sent.constraints,'No cannibalism or live organ harvesting.');assert.equal(sent.constraints_edited,false)
 }finally{await h.close()}
})

test("reenabling restores this colony's latest goal across reload without overwriting draft", async()=>{
 const activation={seq:7,source:"user",load_epoch:"old",payload:{goal:"Protect animals",constraints:"No live organ harvesting",goal_edited:true,constraints_edited:true}}
 const h=setup("en", {session:{colony_id:"A",load_epoch:"new"},autoplay:{enabled:false,activation}});try{
  h.props.actions.push({id:"autoplay_mode",entry_id:"autoplay_mode"});h.render();await flush();await h.tab("Autoplay")
  const find=v=>[...h.root.querySelectorAll('input')].find(x=>x.value===v)
  assert.ok(find('Protect animals'));assert.ok(find('No live organ harvesting'))
  await h.type(find('Protect animals'),'Protect all animals')
  h.state.autoplay.activation={...activation,seq:8};h.render();await flush();assert.ok(find('Protect all animals'))
  await h.click('Enable autoplay');const sent=h.calls.find(c=>c.id==='autoplay_mode').args
  assert.equal(sent.goal,'Protect all animals');assert.equal(sent.goal_edited,true)
  h.state.session={colony_id:"B",load_epoch:"other"};h.state.autoplay.activation=null;h.render();await flush()
  assert.ok(find('Prioritize survival and stability.'));assert.equal(find('Protect all animals'),undefined)
 }finally{await h.close()}
})

test("model settings ignore autoplay receipts and check the matching agent receipt", async()=>{
 const agent={operation_id:"a".repeat(32),kind:"agent",state:"saving",persisted:null,applied:null}
 const autoplay={operation_id:"b".repeat(32),kind:"autoplay",state:"failed",persisted:false,applied:false}
 const h=setup("zh-CN",{settings_saves:[agent,autoplay]});try{
  h.setInvoke(async(id,args)=>({result:id==="get_settings_save" ? {...agent,state:"saved",persisted:true,applied:true} : {workspaces:[],total:0,next_offset:null}}))
  await h.tab("设置");assert.match(h.root.textContent,/正在保存并确认/)
  await h.click("核对这次保存状态")
  assert.equal(h.calls.filter(c=>c.id==="get_settings_save").at(-1).args.operation_id,agent.operation_id)
  assert.match(h.root.textContent,/设置已保存并应用/)
  assert.ok(h.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await h.close()}
})

test("autoplay is a separate page and advice stays the default", async()=>{
 const h=setup("en",{autoplay:{enabled:false,board:[{entry_id:"only-board",kind:"task",revision:1,status:"open",body:"plan"}]}});try{
  await flush();assert.equal(h.root.querySelector('#rimworld-main-nav-tab-work').getAttribute('aria-selected'),'true')
  assert.doesNotMatch(h.root.textContent,/only-board|Enable autoplay/)
  await h.tab('Autoplay');assert.equal(h.root.querySelector('#rimworld-main-nav-tab-autoplay').getAttribute('aria-selected'),'true')
  assert.match(h.root.textContent,/only-board/);assert.match(h.root.textContent,/Enable autoplay/)
  assert.ok(h.calls.every(c=>c.id!=="autoplay_mode"))
  await h.tab('Latest advice');assert.doesNotMatch(h.root.textContent,/only-board|Enable autoplay/)
 }finally{await h.close()}
})

test("idle autoplay polls for background changes and stops after disable", async()=>{
 const h=setup("en",{autoplay:{enabled:false},agent:{active_count:0,recent_count:0,active_runs:[],recent_runs:[]}})
 const originalSet=h.window.setTimeout, originalClear=h.window.clearTimeout
 const timers=new Map();let next=1,refreshes=0
 try{
  h.window.setTimeout=(fn,delay)=>{const id=next++;timers.set(id,{fn,delay});return id}
  h.window.clearTimeout=id=>timers.delete(id)
  h.props.api.refresh=async()=>{refreshes++;h.state.autoplay.fault={kind:"endpoint",code:"model_transport",pause:"requested"};h.render()}
  h.state.autoplay.enabled=true;h.render();await flush()
  assert.match(h.root.textContent,/No consultation in progress/);assert.doesNotMatch(h.root.textContent,/Analyzing/)
  const timer=[...timers.entries()].find(([,v])=>v.delay===4000 || v.delay===8000)
  assert.ok(timer,"idle autoplay schedules a refresh")
  timers.delete(timer[0]);await timer[1].fn();await flush();assert.equal(refreshes,1)
  await h.tab("Autoplay");assert.match(h.root.textContent,/model_transport/)
  assert.ok([...timers.values()].some(v=>v.delay===4000 || v.delay===8000))
  h.state.autoplay.enabled=false;h.render();await flush()
  assert.ok(![...timers.values()].some(v=>v.delay===4000 || v.delay===8000))
  assert.equal(refreshes,1)
 }finally{h.window.setTimeout=originalSet;h.window.clearTimeout=originalClear;await h.close()}
})

test("autoplay receipt cannot survive a backend restart while an old check is pending", async()=>{
 const pending={operation_id:"a".repeat(32),kind:"autoplay",state:"saving",persisted:null,applied:null}
 const h=setup("en",{settings_saves:[pending],autoplay:{enabled:false}});try{
  let finishOld
  h.setInvoke(async(id)=>id==="get_settings_save"?await new Promise(resolve=>{finishOld=resolve}):{result:{workspaces:[],total:0,next_offset:null}})
  await h.tab("Autoplay");await h.click("Check save status");assert.equal(typeof finishOld,"function")
  h.state.settings_saves=[{...pending,state:"saved",persisted:true,applied:true}];h.render();await flush()
  assert.match(h.root.textContent,/Save status: saved/)
  h.setInvoke(async(id)=>{if(id==="get_settings_save")throw new Error("settings_receipt_unavailable");return {result:{workspaces:[],total:0,next_offset:null}}})
  h.state.action_nonce="new-process";h.state.settings_saves=[];h.render();await flush()
  assert.match(h.root.textContent,/Save status: unconfirmed/)
  finishOld({result:{...pending,state:"saved",persisted:true,applied:true}});await flush()
  assert.match(h.root.textContent,/Save status: unconfirmed/);assert.doesNotMatch(h.root.textContent,/Save status: saved/)
  assert.ok(h.calls.filter(c=>c.id==="get_settings_save").length>=2)
  assert.ok(h.calls.every(c=>c.id!=="begin_settings_save"))
 }finally{await h.close()}
})


test("autoplay presents recorded attention and pause without new calls", async()=>{
 for (const locale of ["zh-CN", "en"]) {
  const h=setup(locale,{autoplay:{enabled:true,attention:{available:true,level:"urgent",sampled_tick:100,next_daily_tick:60000,next_escalated_tick:2600,
    reasons:[{event_seq:8,source_id:"alert:1",signal:{label:"Colonist in danger",priority:"Critical"}}],
    leases:[{event_seq:9,actor:"planner",level:"focus",reference:"task",reason:"Check recovery",end_tick:5000}]},
   pause_watchdog:{paused:true,owner:"planner",sampled_tick:101,source_status:"observed",wake_count:2,notification:"submitted"}}});
  try { await flush();await h.tab(locale === "en" ? "Autoplay" : "自动游玩");const text=h.root.textContent;
   assert.match(text,locale === "en" ? /Attention level: Urgent/ : /关注档位: 紧急/);
   assert.match(text,/Colonist in danger.*Critical/);assert.match(text,/60000/);assert.match(text,/2600/);assert.match(text,/5000/);
   assert.match(text,locale === "en" ? /Pause owner: Planner/ : /暂停归属: 规划者/);
   assert.match(text,locale === "en" ? /Pause evidence: Recorded for this connection/ : /暂停依据: 已记录当前连接状态/);
   assert.match(text,locale === "en" ? /Submitted to host/ : /已提交给宿主/);
   assert.ok(h.calls.every(c=>!["autoplay_mode","rimworld_consult","probe_game_overview"].includes(c.id)));
  } finally {await h.close()}
 }
});

test("autoplay distinguishes unavailable, unknown and notification outcomes", async()=>{
 const h=setup("en",{autoplay:{enabled:true,attention:{available:false,level:null,reasons:[],leases:[],next_daily_tick:60000,next_escalated_tick:null,sampled_tick:null},
   pause_watchdog:{paused:null,owner:null,sampled_tick:null,source_status:"unavailable",wake_count:null,notification:null}}});
 try { await flush();await h.tab("Autoplay");assert.match(h.root.textContent,/Native attention signals unavailable/);assert.match(h.root.textContent,/Attention level: unknown/);
  assert.match(h.root.textContent,/Pause owner: unknown/);assert.match(h.root.textContent,/Notification: unknown/);
  h.state.autoplay.pause_watchdog={paused:true,owner:"unknown",sampled_tick:17,source_status:"observed",wake_count:3,notification:"rejected"};h.render();await flush();assert.match(h.root.textContent,/Submission rejected/);
  h.state.autoplay.pause_watchdog.notification="unknown";h.render();await flush();assert.match(h.root.textContent,/Submission outcome unknown/);
  h.state.autoplay.enabled=false;h.render();await flush();assert.doesNotMatch(h.root.textContent,/Attention and review|Pause and watchdog/);
 } finally {await h.close()}
});


test("autoplay default state is unknown without a connected session", async()=>{
 for (const locale of ["en", "zh-CN"]) {
  const h=setup(locale,{autoplay:{enabled:true,attention:{available:null,level:null,reasons:[],next_daily_tick:null,next_escalated_tick:null,leases:[],sampled_tick:null,load_epoch:null},
   pause_watchdog:{paused:null,owner:null,sampled_tick:null,source_status:"unavailable",wake_count:null,notification:null},
   board:[{entry_id:"task",kind:"task",revision:1,status:"open",body:"Plan"}],actions:[{seq:3,mode:"commit",status:"committed",changeset_id:"id",tick:10}]}});
  try { await flush();await h.tab(locale === "en" ? "Autoplay" : "自动游玩");const text=h.root.textContent;
   assert.match(text,locale === "en" ? /Native attention availability is unknown/ : /原生注意力信号是否可用尚不清楚/);
   assert.match(text,locale === "en" ? /Next daily review: unknown/ : /下次日复核: 未知/);
   assert.doesNotMatch(text,locale === "en" ? /daily review remains scheduled/ : /日复核仍按计划进行/);
   assert.match(text,/task task · revision 1 · open · Plan/);assert.match(text,/commit · committed · id · tick 10/);
  } finally {await h.close()}
 }
});


test("autoplay shows an active focus request when native attention is unavailable", async()=>{
 for (const locale of ["en", "zh-CN"]) {
  const h=setup(locale,{autoplay:{enabled:true,attention:{available:false,source_status:"unavailable",level:"focus",reasons:[{event_seq:7,source_id:"lease",signal:{reason:"Review treatment"}}],next_daily_tick:60000,next_escalated_tick:15000,leases:[{event_seq:7,actor:"planner",level:"focus",reference:"task",reason:"Review treatment",end_tick:16000}],sampled_tick:1000,load_epoch:"epoch"},pause_watchdog:{paused:null,owner:null,sampled_tick:null,source_status:"unavailable",wake_count:null,notification:null}}});
  try {await flush();await h.tab(locale === "en" ? "Autoplay" : "自动游玩");const text=h.root.textContent;
   assert.match(text,locale === "en" ? /Attention level: Focus/ : /关注档位: 关注/);
   assert.match(text,locale === "en" ? /Native attention signals unavailable/ : /原生注意力信号不可用/);
   assert.match(text,locale === "en" ? /Native observation: Unavailable/ : /原生观察质量: 不可用/);
   assert.match(text,locale === "en" ? /Calculated from recorded facts and active requests/ : /根据已记录事实与有效声明计算/);
   assert.match(text,/Review treatment/);assert.match(text,/16000/);
  } finally {await h.close()}
 }
});

function colonyWorkFixture() {
 const render_spec={format:"construction_render.v1",map_id:"map:1",bounds:{min_x:10,min_z:20,width:3,height:3},sample:{game_tick:100},source_sha256:"observation-hash",plan_sha256:"plan-hash",coverage:{cells_complete:false,objects_complete:true},
  cells:[{x:10,z:20,known:true,walkable:true},{x:12,z:22,known:false}],objects:[{id:"bed:1",stage:"built",cells:[{x:10,z:20}]}],elements:[{id:"wall:1",layer:"building",cells:[{x:11,z:21}],interaction_cells:[{x:10,z:21},{x:12,z:21}]}],areas:[],issues:[{severity:"unknown",code:"observation_incomplete",reason:"Northern cells need observation",cells:[{x:12,z:22}],element_ids:["wall:1"]}]};
 return {format:"colony_work.v1",load_epoch:"load:1",suggestions:[{event_seq:1,load_epoch:"load:old",suggestion_id:"possible-kitchen",plan:{title:"Possible kitchen"},reason:"Discuss this candidate",attachments:[],render_spec:{...render_spec,plan_id:'<script>alert("candidate")</script>'}}],
  construction:{plans:[{plan_id:"house:1",task_id:"shelter",revision:2,plan:{title:"Shelter"},placement_complete:true,construction_complete:false,purpose_status:"not_evaluated",phase:"in_progress",counts:{required:2,fulfilled:1,stopped:1,unknown:0},render_spec,
   elements:[{id:"wall:1",intent:"continue",stage:"frame",fresh:true,object_id:"frame:1"},{id:"bed:1",intent:"taken_over",stage:"user_replaced",fresh:false,intent_reason:"Player changed the bed"},{id:"roof:1",intent:"continue",stage:"designated",fresh:true}],attachments:[{evidence_id:"image:source-reference",reference_id:"source-reference",sha256:"image-byte-hash",mime_type:"image/png",width:64,height:64,metadata:{map_id:"map:1",load_epoch:"load:1",observation_id:"scene:1",revision:2}}],history:[{kind:"construction.adopted",seq:2,revision:1,load_epoch:"load:1"},{kind:"construction.revised",seq:3,revision:2,load_epoch:"load:1"}]}]},
  resources:{decisions:[{allocation_id:"wood-choice",revision:1,reason:"Build shelter before kitchen",snapshot:{quantities:{WoodLog:80},map_id:"map:1",scope:{selected_sources:["spawned_ground","trader_stock"]},game_tick:100,load_epoch:"load:1",complete:true},assessment:{
   independent:[{demand_id:"house",revision:2,window:"today",required:{WoodLog:60},shortfalls:{},unknown:[],satisfied:true},{demand_id:"kitchen",revision:1,window:"today",required:{WoodLog:50},shortfalls:{},unknown:[],satisfied:true}],
   joint:[{window:"today",required:{WoodLog:110},shortfalls:{WoodLog:30},unknown:[],satisfied:false,status:"shortfall"}],
   selected:{window:"today",current_stock:{satisfied:true,shortfalls:{}},demands:[{demand_id:"house",share:{WoodLog:60},remaining:{WoodLog:0},satisfied:true},{demand_id:"kitchen",share:{WoodLog:20},remaining:{WoodLog:30},satisfied:false}],stale:false},competition:[{resource:"WoodLog",window:"today",demand_ids:["house","kitchen"],shortfall:30}],discrepancies:[]},history:[{seq:4,revision:1,observation_id:"stock:1"}]}]},
  policies:{impacts:[{impact_id:"food-impact",revision:1,permission:{policy_id:"food:1",scope_id:"colonists",mode:"members",members:["pawn:A"]},action:"edit",protections:[{kind:"effective_rules",pawn_id:"pawn:B",policy_id:"food:1"}],assessment:{status:"scope_conflict",actual_members:["pawn:A","pawn:B"],affected_members:["pawn:A","pawn:B"],extra_members:["pawn:B"],reasons:["additional_members","protected_effect"]},history:[{seq:5,revision:1,status:"scope_conflict"}]}]},
  observations:{intents:[{observation_id:"watch:1",task_id:"shelter",revision:1,binding:{colony_id:"colony:1",load_epoch:"load:1",connection_epoch:"connection:1"},active:true,coverage_status:"partial",conditions:[{id:"door-watch",kind:"object_lifecycle",target_ids:["door:1"],sample_interval_ticks:250,expires_at_tick:10000,max_samples:20},{id:"wall-watch",kind:"object_lifecycle",target_ids:["wall:1"],sample_interval_ticks:250,expires_at_tick:10000,max_samples:20}],latest_facts:[{condition_id:"door-watch",status:"changed",sample_tick:500,receipt_id:"receipt:1",binding:{colony_id:"colony:1",load_epoch:"load:1",connection_epoch:"connection:1"}}],history:[{kind:"observation.requested",seq:6,revision:1}]}],coverage:[{observation_id:"watch:1",revision:1,status:"partial",binding:{colony_id:"colony:1",load_epoch:"load:1",connection_epoch:"connection:1"},covered_condition_ids:["door-watch"],uncovered_condition_ids:["wall-watch"],receipt_id:"receipt:1",ack:{reason:"Wall currently unavailable"}}]}};
}

test("colony projects distinguish discussion candidates, adopted revisions and native fulfillment without issuing writes",async()=>{
 for(const locale of ["zh-CN","en"]){
  const h=setup(locale,{session:{colony_id:"colony:1",load_epoch:"load:1"},autoplay:{enabled:false,colony_work:colonyWorkFixture()}});
  try{await flush();await h.tab(locale==="en"?"Autoplay":"自动游玩");const content=h.root.textContent;
   assert.match(content,locale==="en"?/Candidate — not adopted/:/尚未正式采用/);assert.match(content,/Possible kitchen/);assert.match(content,/Shelter/);
   assert.match(content,locale==="en"?/Revision 2/:/修订 2/);assert.match(content,locale==="en"?/Construction complete: No/:/建筑已完工: 否/);
   assert.match(content,locale==="en"?/Purpose assessment: Not evaluated/:/用途评价: 尚未评价/);
   assert.match(content,locale==="en"?/Taken over by player/:/玩家已接管/);assert.match(content,locale==="en"?/Historical \/ needs reconciliation/:/历史证据 \/ 待重新核对/);
   assert.match(content,/Northern cells need observation/);assert.doesNotMatch(content,/colony\.(?:value|image|title)/);
   const image=h.root.querySelector('svg[role="img"]');assert.ok(image);assert.equal(image.namespaceURI,"http://www.w3.org/2000/svg");
   const svg=image.outerHTML;assert.equal(image.querySelector('script'),null);assert.match(svg,/&lt;script&gt;/);assert.match(svg,/viewBox="0 0 3 3"/);assert.match(svg,/url\(#unknown\)/);
   assert.equal(image.querySelectorAll('rect[stroke="#006d77"]').length,2);assert.match(content,/image-byte-hash/);assert.match(content,locale==="en"?/Area designated/:/区域已划定/);
   const plan=h.state.autoplay.colony_work.construction.plans[0];plan.protected_targets=[{element_id:"retired-old-bed",source:"native",object_id:"old-bed",reason:"user_cancelled",seq:77,target:{map_id:"map:1",position:{x:10,z:20},definition:"Bed",layer:"building"}}];plan.elements[1].stop={reason:"user_replaced",released_by:78};h.render();await flush();
   assert.match(h.root.textContent,/retired-old-bed/);assert.match(h.root.textContent,locale==="en"?/different plan does not release/:/换用其他方案不会解除/);assert.match(h.root.textContent,/map:1 · Bed · x 10, z 20/);assert.match(h.root.textContent,/78/);
   assert.ok(h.calls.every(call=>call.id==="workspace_view"),"reading project states must not consult, register observations or write game state");
  }finally{await h.close()}
 }
});

test("operating view preserves independent, joint and selected resource readings and shared policy effects",async()=>{
 for(const locale of ["en","zh-CN"]){
  const h=setup(locale,{session:{load_epoch:"load:1"},autoplay:{enabled:true,colony_work:colonyWorkFixture()}});
  try{await flush();await h.tab(locale==="en"?"Autoplay":"自动游玩");await h.click(locale==="en"?"Production and living":"生产与生活",'[role="tab"]');
   const content=h.root.textContent;assert.match(content,/WoodLog: 80/);assert.match(content,/WoodLog: 110/);assert.match(content,/WoodLog: 30/);assert.match(content,/WoodLog: 20/);
   assert.match(content,locale==="en"?/Independent requirement/:/独立需求/);assert.match(content,locale==="en"?/Joint requirement/:/联合需求/);assert.match(content,locale==="en"?/Selected allocation/:/已选分配/);
   assert.match(content,/pawn:A, pawn:B/);assert.match(content,locale==="en"?/Scope conflict/:/范围冲突/);assert.match(content,locale==="en"?/Effective rules/:/保护实际生效规则/);
   assert.match(content,locale==="en"?/Spawned on map ground.*Trader goods/:/地图地面物资.*商人物资/);assert.match(content,locale==="en"?/Impact assessment recorded at selection/:/选择时记录的影响评价/);
   h.state.autoplay.colony_work.resources.decisions[0].assessment.selected.stale=true;h.state.autoplay.colony_work.resources.decisions[0].assessment.discrepancies=[{kind:"stock_changed"}];h.render();await flush();
   assert.match(h.root.textContent,locale==="en"?/Stock changed/:/库存已变化/);assert.match(h.root.textContent,locale==="en"?/differs from the latest assessment/:/与最新评价存在差异/);
   assert.doesNotMatch(h.root.textContent,/colony\.(?:value|resource|policy)/);assert.ok(h.calls.every(call=>call.id==="workspace_view"));
   const decision=h.state.autoplay.colony_work.resources.decisions[0];decision.current_snapshot={...decision.snapshot,quantities:{WoodLog:65},game_tick:200};decision.current_demands=[{id:"kitchen",revision:2,window:"today",resources:{WoodLog:60}}];decision.evidence_status="current_epoch";h.render();await flush();
   assert.match(h.root.textContent,locale==="en"?/Latest stock assessment sample: WoodLog: 65/:/最近一次库存评价采样: WoodLog: 65/);assert.match(h.root.textContent,locale==="en"?/Current load evidence/:/当前读档证据/);
   decision.evidence_status="historical";for(const reading of [...decision.assessment.independent,...decision.assessment.joint,decision.assessment.selected.current_stock,...decision.assessment.selected.demands]){reading.historical_satisfied=reading.satisfied;reading.satisfied=null}decision.assessment.joint[0].status="historical";h.render();await flush();
   assert.match(h.root.textContent,locale==="en"?/Current sufficiency is unknown/:/当前是否充足尚不清楚/);
   const selectedTable=[...h.root.querySelectorAll('table')].find(table=>table.querySelector('thead').textContent.includes(locale==="en"?"Allocated share":"分配份额"));assert.ok(selectedTable);assert.match(selectedTable.textContent,locale==="en"?/Unknown/:/未知/);assert.doesNotMatch(selectedTable.querySelector('tbody').textContent,locale==="en"?/Yes/:/是/);
  }finally{await h.close()}
 }
});

test("observation view shows pending conditions, native receipts and lost coverage after load changes",async()=>{
 const h=setup("en",{session:{load_epoch:"load:1"},autoplay:{enabled:true,colony_work:colonyWorkFixture()}});
 try{await flush();await h.tab("Autoplay");await h.click("Observation coverage",'[role="tab"]');
  let content=h.root.textContent;assert.match(content,/watch:1/);assert.match(content,/Partial/);assert.match(content,/wall-watch/);assert.match(content,/receipt:1/);assert.match(content,/Wall currently unavailable/);assert.match(content,/10000/);assert.match(content,/500/);
  h.state.session.load_epoch="load:2";h.state.autoplay.colony_work.load_epoch="load:2";h.state.autoplay.colony_work.observations.intents[0].coverage_status="pending_rebind";Object.assign(h.state.autoplay.colony_work.observations.coverage[0],{status:"pending_rebind",binding:{colony_id:"colony:1",load_epoch:"load:2",connection_epoch:"connection:2"},covered_condition_ids:[],uncovered_condition_ids:["door-watch","wall-watch"],receipt_id:null,ack:null});h.render();await flush();
  content=h.root.textContent;assert.match(content,/Waiting for connection registration/);assert.match(content,/Changed500Historical \/ needs reconciliationreceipt:1/);assert.doesNotMatch([...h.root.querySelectorAll('table')].at(-1).textContent,/receipt:1|Continuously covered/);
  h.state.autoplay.colony_work.observations.coverage[0].condition_states={"door-watch":"unknown","wall-watch":"expired","food-watch":"exhausted"};h.render();await flush();assert.match(h.root.textContent,/door-watch: Unknown/);assert.match(h.root.textContent,/wall-watch: Expired/);assert.match(h.root.textContent,/food-watch: Sample limit reached/);
  await h.click("Decision history",'[role="tab"]');content=h.root.textContent;for(const id of ["house:1","wood-choice","food-impact","watch:1"])assert.match(content,new RegExp(id));
  assert.ok(h.calls.every(call=>call.id==="workspace_view"));
 }finally{await h.close()}
});

test("old colony-work records remain visibly historical and do not become current evidence",async()=>{
 const h=setup("en",{session:{load_epoch:"load:other"},autoplay:{enabled:false,colony_work:colonyWorkFixture()}});
 try{await flush();await h.tab("Autoplay");assert.match(h.root.textContent,/another load or an unavailable session/);
  h.state.autoplay.colony_work.construction.plans[0].render_spec.bounds.width=65;h.render();await flush();assert.match(h.root.textContent,/No native observation image/);
  assert.ok(h.calls.every(call=>call.id==="workspace_view"));
 }finally{await h.close()}
});

test("visual input remains opt-in and saves an explicit positive reservation through the existing receipt",async()=>{
 for(const locale of ["en","zh-CN"]){
  const h=setup(locale);
  try{await flush();await h.tab(locale==="en"?"Settings":"设置");
   const checkbox=h.root.querySelector('input[type="checkbox"]');assert.ok(checkbox);assert.equal(checkbox.checked,false);
   const reservation=[...h.root.querySelectorAll('input[type="number"]')].at(-1);assert.equal(reservation.value,"0");
   assert.match(h.root.textContent,locale==="en"?/Local previews and exports remain available/:/仍可在本地预览与导出图像/);
   checkbox.checked=true;checkbox.dispatchEvent(new h.window.Event("change",{bubbles:true}));await flush();
   await h.click(locale==="en"?"Save model settings":"保存模型设置");assert.equal(h.calls.filter(call=>call.id==="begin_settings_save").length,0);assert.match(h.root.textContent,locale==="en"?/requires a value greater than zero/:/必须大于零/);
   await h.type(reservation,"2048");await h.click(locale==="en"?"Save model settings":"保存模型设置");
   const saved=h.calls.find(call=>call.id==="begin_settings_save");assert.ok(saved);assert.equal(saved.args.kind,"agent");assert.equal(saved.args.image_input_enabled,true);assert.equal(saved.args.image_input_tokens_per_image,2048);assert.equal(saved.args.ui_nonce,"test-nonce");assert.match(saved.args.operation_id,/^[a-f0-9]{32}$/);
   assert.match(h.root.textContent,locale==="en"?/Saved/:/已保存/);assert.ok(h.calls.every(call=>["workspace_view","begin_settings_save"].includes(call.id)),"changing endpoint capability must not start a consultation or a probe");
  }finally{await h.close()}
 }
});

test("visual capability edits survive dashboard refresh and cannot be saved with fractional reservations",async()=>{
 const h=setup("en");
 try{await h.tab("Settings");const reservation=[...h.root.querySelectorAll('input[type="number"]')].at(-1);await h.type(reservation,"1234");
  Object.assign(h.state.configuration,{image_input_enabled:true,image_input_tokens_per_image:4096});h.render();await flush();assert.equal(reservation.value,"1234");assert.equal(h.root.querySelector('input[type="checkbox"]').checked,false);
  await h.type(reservation,"1.5");await h.click("Save model settings");assert.equal(h.calls.filter(call=>call.id==="begin_settings_save").length,0);assert.match(h.root.textContent,/integer image reservation/);
 }finally{await h.close()}
});

test("planner purpose findings stay separate from native completion and area returns count only actual protected cells",async()=>{
 for(const locale of ["en","zh-CN"]){
  const work=colonyWorkFixture(), plan=work.construction.plans[0];
  plan.purpose_status="supported";plan.purpose_assessment={source:"planner_assessment",plan_id:"house:1",task_id:"shelter",revision:2,load_epoch:"load:1",goal:"Safe sleeping space",judgment:"supported",findings:["The recorded scene supports the requested sleeping layout."],evidence_ids:["scene:1","construction:1"],evidence_refs:[{observation_id:"scene:1",operation:"inspect_construction",sha256:"native-scene-hash",map_id:"map:1",game_tick:101,sample:{started_tick:100,finished_tick:101,load_epoch:"load:1"}},{observation_id:"construction:1",operation:"inspect_construction_intents",sha256:"native-progress-hash",map_id:"map:1",game_tick:105}],assessed_seq:8,assessed_tick:150,facts_through_seq:7,basis_fingerprint:"basis-hash",freshness:"current",stale:false,stale_reasons:[],current_revision:2,current_load_epoch:"load:1"};
  const scopes=[{object:"Area_BuildRoof_7",field:"area_flag:10:20"},{object:"Area_BuildRoof_7",field:"area_flag:11:20"}];
  const stop={source:"native",reason:"user_cancelled",scope:null,scopes,scope_returns:[{...scopes[0],return_seq:70}],target:{map_id:"map:1",kind:"roof",cells:[[10,20],[11,20],[10,21],[11,21]]}};
  plan.elements[2].stop=stop;plan.protected_targets=[{element_id:"roof:1",seq:60,...stop}];
  const h=setup(locale,{session:{load_epoch:"load:1"},autoplay:{enabled:true,colony_work:work}});
  try{await flush();await h.tab(locale==="en"?"Autoplay":"自动游玩");let content=h.root.textContent;
   assert.match(content,locale==="en"?/Planner finds supporting evidence/:/规划者认为有支持依据/);assert.match(content,locale==="en"?/Construction complete: No/:/建筑已完工: 否/);
   assert.match(content,locale==="en"?/This is a model judgment/:/这是模型判断/);assert.match(content,/The recorded scene supports/);assert.match(content,/scene:1, construction:1/);assert.match(content,/Safe sleeping space/);
   assert.match(content,locale==="en"?/Assessment event tick: 150/:/评价事件 tick: 150/);assert.match(content,/100–101/);assert.match(content,/native-scene-hash/);assert.match(content,/native-progress-hash/);
   assert.match(content,locale==="en"?/Protected cells: 2 · Returned: 1 · Awaiting return: 1/:/受保护格数：2 · 已归还：1 · 待归还：1/);assert.doesNotMatch(content,locale==="en"?/Protected cells: 4|Awaiting return: 3/:/受保护格数：4|待归还：3/);
   plan.purpose_status="unmet";plan.purpose_assessment.judgment="unmet";h.render();await flush();assert.match(h.root.textContent,locale==="en"?/Planner finds purpose unmet/:/规划者认为用途尚未满足/);
   plan.purpose_status="unknown";plan.purpose_assessment.judgment="unknown";h.render();await flush();assert.match(h.root.textContent,locale==="en"?/Planner cannot determine purpose fulfillment/:/规划者无法判断用途是否满足/);
   plan.purpose_status="stale";Object.assign(plan.purpose_assessment,{judgment:"supported",freshness:"stale",stale:true,stale_reasons:["revision_changed","load_epoch_changed","related_facts_changed"],current_revision:3,current_load_epoch:"load:2"});h.render();await flush();content=h.root.textContent;
   assert.match(content,locale==="en"?/Assessment needs renewal/:/评价待重新核对/);assert.match(content,locale==="en"?/Related native facts or continuing intent changed/:/相关原生事实或持续意图已变化/);assert.match(content,/load:2/);assert.match(content,locale==="en"?/Construction complete: No/:/建筑已完工: 否/);
   plan.elements[2].stop={reason:"user_cancelled",scope:scopes[0],released_by:71};plan.protected_targets=[];h.render();await flush();assert.match(h.root.textContent,locale==="en"?/Protected cells: 1 · Returned: 1 · Awaiting return: 0/:/受保护格数：1 · 已归还：1 · 待归还：0/);
   assert.doesNotMatch(h.root.textContent,/colony\.(?:purpose|returned)/);assert.ok(h.calls.every(call=>call.id==="workspace_view"));
  }finally{await h.close()}
 }
});
