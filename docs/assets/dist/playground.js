import{b as L}from"./chunk-ZFO45COR.js";import{g as h,h as C}from"./chunk-WKRYAROX.js";import{a as A}from"./chunk-ZHLICTNA.js";import{a as b,b as N,c as U,d as M,e as Y}from"./chunk-6CZKKW3I.js";var y=b(N(),1),R=b(U(),1);var v=b(N(),1);var g=window.__t||((a,s)=>s);function O(a){let{doc:s,error:r}=C(a);return r?{success:!1,error:r.message,line:r.line,column:r.column}:{success:!0,data:s}}function T(a){let s=(c,u)=>({valid:!1,errors:[{rule:c,message:u}],summary:{tenants:0}}),r=O(a);if(!r.success)return s(g("YAML \u8A9E\u6CD5","YAML Syntax"),g(`\u89E3\u6790\u932F\u8AA4\uFF1A${r.error}`,`Parse error: ${r.error}`));let m=r.data;if(!h(m)||!Object.prototype.hasOwnProperty.call(m,"tenants"))return s(g("\u7D50\u69CB","Structure"),g('\u6839\u7BC0\u9EDE\u5FC5\u9808\u662F\u542B "tenants:" \u9375\u7684 mapping','Root must be a mapping with a "tenants:" key'));let p=m.tenants;if(!h(p))return s(g("\u7D50\u69CB","Structure"),g('"tenants" \u5FC5\u9808\u662F mapping\uFF08\u79DF\u6236\u540D\u7A31: \u8A2D\u5B9A\uFF09','"tenants" must be a mapping (tenant name: config)'));let i=[],n=Object.keys(p);for(let c of n){let u=p[c];u!==null&&!h(u)&&i.push({rule:g("\u79DF\u6236\u7D50\u69CB","Tenant Structure"),message:g(`\u79DF\u6236 "${c}" \u7684\u503C\u5FC5\u9808\u662F mapping`,`Tenant "${c}" must be a mapping`)})}return{valid:i.length===0,errors:i,summary:{tenants:n.length}}}var e=b(M(),1),o=window.__t||((a,s)=>s);function B(a=new Date){return{minimal:`# This is ALL a tenant needs to write \u2014 just 3 lines!
tenants:
  my-app:
    mysql_connections: "100"`,mariadb:`tenants:
  db-a:
    mysql_connections: "70"
    mysql_connections_critical: "95"
    mysql_threads_running: "40"
    _silent_mode: "disable"
    _routing:
      receiver_type: "webhook"
      webhook_url: "https://webhook.example.com/alerts"
      group_wait: "30s"
      repeat_interval: "4h"`,postgresql:`tenants:
  db-b:
    pg_connections: "150"
    pg_connections_critical: "200"
    pg_cache_hit_ratio: "85"
    pg_query_time: "5000"
    _state_maintenance:
      expires: "2026-03-20T06:00:00Z"
    _routing:
      receiver_type: "slack"
      webhook_url: "https://hooks.slack.com/services/example"
      group_wait: "1m"
      group_interval: "5m"
      repeat_interval: "12h"`,redis:`tenants:
  cache:
    redis_memory: "80"
    redis_memory_critical: "95"
    redis_evictions: "1000"
    redis_connected_clients: "5000"
    _silent_mode:
      target: "warning"
      expires: "${L(a)}"
      reason: "Cache migration"
    _routing:
      receiver_type: "email"
      webhook_url: "mailto:ops@example.com"
      group_wait: "45s"
      repeat_interval: "6h"`,kafka:`tenants:
  streaming:
    kafka_lag: "100000"
    kafka_lag_critical: "500000"
    kafka_broker_active: "3"
    kafka_controller_active: "1"
    kafka_isr_shrank: "0"
    _routing:
      receiver_type: "teams"
      webhook_url: "https://teams.example.com/webhook"
      group_wait: "2m"
      group_interval: "3m"
      repeat_interval: "24h"`,"routing-profiles":`# v2.1.0: Cross-Domain Routing Profiles (ADR-007)
_routing_defaults:
  receiver_type: "webhook"
  group_wait: "30s"
  repeat_interval: "4h"

routing_profiles:
  standard-webhook:
    receiver_type: "webhook"
    group_wait: "30s"
    repeat_interval: "4h"
  urgent-slack:
    receiver_type: "slack"
    group_wait: "10s"
    repeat_interval: "1h"

tenants:
  db-a:
    mysql_connections: "80"
    _routing:
      profile: "standard-webhook"
      webhook_url: "https://hooks.example.com/db-a"
  db-b:
    pg_connections: "120"
    _routing:
      profile: "urgent-slack"
      webhook_url: "https://hooks.slack.com/services/db-b"

_domain_policy:
  allowed_domains: ["*.example.com", "hooks.slack.com"]
  denied_domains: ["*.internal.corp"]`}}function z(a,s){let r=a.split(`
`),m=s.split(`
`),p=Math.max(r.length,m.length),i=[];for(let n=0;n<p;n++){let c=r[n],u=m[n];c===void 0?i.push({type:"removed",line:u,num:n+1}):u===void 0?i.push({type:"added",line:c,num:n+1}):c!==u?i.push({type:"changed",line:c,oldLine:u,num:n+1}):i.push({type:"same",line:c,num:n+1})}return i}function I(a){try{return btoa(unescape(encodeURIComponent(a)))}catch{return""}}function K(a){try{return decodeURIComponent(escape(atob(a)))}catch{return null}}function H(){try{let a=new URLSearchParams(window.location.hash.slice(1)),s=a.get("yaml"),r=a.get("tpl");return{yaml:s?K(s):null,tpl:r||null}}catch{return{yaml:null,tpl:null}}}function x(){let a=(0,v.useMemo)(()=>B(),[]),s=H(),[r,m]=(0,v.useState)(s.yaml||a[s.tpl]||a.mariadb),[p,i]=(0,v.useState)(s.tpl||"mariadb"),[n,c]=(0,v.useState)(!1),[u,S]=(0,v.useState)(""),{copied:_,copy:$}=A(2500),d=(0,v.useMemo)(()=>T(r),[r]),k=(0,v.useMemo)(()=>z(r,a[p]),[r,p]),w=k.some(t=>t.type!=="same"),D=()=>{m(a[p])},P=t=>{let l=t.target.value;i(l),m(a[l])},j=()=>{let t=new Blob([r],{type:"application/x-yaml"}),l=URL.createObjectURL(t),f=document.createElement("a");f.href=l,f.download="tenant-config.yaml",f.click(),URL.revokeObjectURL(l)},q=()=>{let t=I(r),f=window.location.origin+window.location.pathname+window.location.search+"#yaml="+t;S(f),$(f)};return(0,e.jsxs)("div",{className:"flex h-screen bg-[color:var(--da-color-surface)]",children:[(0,e.jsx)("div",{className:"fixed top-0 left-0 right-0 bg-[color:var(--da-color-card-bg)] border-b border-[color:var(--da-color-surface-border)] p-4 shadow-sm z-10",children:(0,e.jsxs)("div",{className:"max-w-7xl mx-auto flex items-center justify-between",children:[(0,e.jsxs)("div",{children:[(0,e.jsx)("h1",{className:"text-2xl font-bold text-[color:var(--da-color-fg)]",children:o("\u79DF\u6236 YAML \u8A9E\u6CD5\u6AA2\u67E5\u5668","Tenant YAML Syntax Checker")}),(0,e.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mt-1",children:o("\u6AA2\u67E5\u79DF\u6236 YAML \u7684\u8A9E\u6CD5\u8207\u7D50\u69CB\uFF08\u4E0D\u6AA2\u67E5\u503C\u7684\u8A9E\u610F\uFF09","Checks tenant YAML syntax and structure (not the meaning of values)")})]}),(0,e.jsxs)("div",{className:"flex gap-3",children:[(0,e.jsxs)("select",{value:p,onChange:P,"aria-label":o("\u9078\u64C7\u7BC4\u672C","Select template"),className:"px-3 py-2 bg-[color:var(--da-color-card-bg)] border border-[color:var(--da-color-surface-border)] rounded-md text-sm font-medium text-[color:var(--da-color-fg)] hover:bg-[color:var(--da-color-surface)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-accent)]",children:[(0,e.jsx)("option",{value:"minimal",children:o("\u6700\u5C0F\u5316 (3\u884C!)","Minimal (3 lines!)")}),(0,e.jsx)("option",{value:"mariadb",children:o("MariaDB \u793A\u4F8B","MariaDB Example")}),(0,e.jsx)("option",{value:"postgresql",children:o("PostgreSQL \u793A\u4F8B","PostgreSQL Example")}),(0,e.jsx)("option",{value:"redis",children:o("Redis \u793A\u4F8B","Redis Example")}),(0,e.jsx)("option",{value:"kafka",children:o("Kafka \u793A\u4F8B","Kafka Example")})]}),(0,e.jsx)("button",{onClick:D,className:"px-4 py-2 bg-[color:var(--da-color-fg)] text-[color:var(--da-color-card-bg)] rounded-md text-sm font-medium hover:bg-[color:var(--da-color-accent-hover)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-fg)]",children:o("\u91CD\u7F6E","Reset")}),(0,e.jsxs)("button",{onClick:()=>c(!n),className:`px-4 py-2 rounded-md text-sm font-medium focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-info)] ${n?"bg-[color:var(--da-color-info)] text-[color:var(--da-color-card-bg)]":"bg-[color:var(--da-color-info-soft)] text-[color:var(--da-color-info)] hover:bg-[color:var(--da-color-info-soft)]"}`,children:[n?o("\u96B1\u85CF\u5DEE\u7570","Hide Diff"):o("\u5DEE\u7570\u5C0D\u6BD4","Diff"),w&&!n&&(0,e.jsx)("span",{className:"ml-1 text-xs",children:"\u25CF"})]}),(0,e.jsx)("button",{onClick:j,disabled:!d.valid,className:"px-4 py-2 bg-[color:var(--da-color-success)] text-[color:var(--da-color-card-bg)] rounded-md text-sm font-medium hover:bg-[color:var(--da-color-success)] disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-success)]",children:o("\u532F\u51FA .yaml","Export .yaml")}),(0,e.jsx)("button",{onClick:q,className:`px-4 py-2 rounded-md text-sm font-medium focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-accent)] ${_?"bg-[color:var(--da-color-accent)] text-[color:var(--da-color-card-bg)]":"bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)] hover:bg-[color:var(--da-color-accent-soft)]"}`,children:_?(0,e.jsxs)(e.Fragment,{children:[(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})," ",o("\u5DF2\u8907\u88FD","Link Copied")]}):o("\u5206\u4EAB\u9023\u7D50","Share Link")})]})]})}),(0,e.jsxs)("div",{className:"flex w-full pt-24",children:[(0,e.jsxs)("div",{className:"w-1/2 border-r border-[color:var(--da-color-surface-border)] flex flex-col bg-[color:var(--da-color-card-bg)]",children:[(0,e.jsxs)("div",{className:"px-6 py-4 border-b border-[color:var(--da-color-surface-border)]",children:[(0,e.jsx)("h2",{className:"text-lg font-semibold text-[color:var(--da-color-fg)]",children:o("\u79DF\u6236 YAML","Tenant YAML")}),(0,e.jsx)("p",{className:"text-xs text-[color:var(--da-color-muted)] mt-1",children:o("\u5728\u4E0B\u65B9\u7DE8\u8F2F YAML\u3002\u8A9E\u6CD5\u6AA2\u67E5\u5373\u6642\u66F4\u65B0\u3002","Edit YAML below. The syntax check updates in real-time.")})]}),n&&(0,e.jsxs)("div",{className:"border-b border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] px-6 py-3 max-h-48 overflow-y-auto",children:[(0,e.jsx)("div",{className:"text-xs font-semibold text-[color:var(--da-color-fg)] mb-2",children:o("\u76F8\u5C0D\u65BC\u7BC4\u672C\u7684\u8B8A\u5316:","Changes vs. template:")}),(0,e.jsxs)("pre",{className:"font-mono text-xs leading-relaxed",children:[k.map((t,l)=>{if(t.type==="same")return null;let f=t.type==="added"?"text-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)]":t.type==="removed"?"text-[color:var(--da-color-error)] bg-[color:var(--da-color-error-soft)]":"text-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)]";return(0,e.jsxs)("div",{className:`${f} px-2 py-0.5 rounded`,children:[(0,e.jsx)("span",{className:"text-[color:var(--da-color-muted)] mr-2",children:t.num}),t.type==="removed"?"- ":t.type==="added"?"+ ":"~ ",t.line]},l)}),!w&&(0,e.jsx)("div",{className:"text-[color:var(--da-color-muted)]",children:o("\u76F8\u5C0D\u65BC\u7BC4\u672C\u6C92\u6709\u66F4\u6539\u3002","No changes from template.")})]})]}),(0,e.jsxs)("div",{className:"flex-1 overflow-hidden flex",children:[(0,e.jsx)("div",{className:"w-12 bg-[color:var(--da-color-tag-bg)] border-r border-[color:var(--da-color-surface-border)] flex flex-col items-center py-4 text-xs text-[color:var(--da-color-tag-fg)] font-mono",children:r.split(`
`).map((t,l)=>(0,e.jsx)("div",{className:"h-6 flex items-center justify-center",children:l+1},l))}),(0,e.jsx)("textarea",{value:r,onChange:t=>m(t.target.value),"aria-label":o("\u79DF\u6236 YAML \u7DE8\u8F2F\u5668","Tenant YAML editor"),className:"flex-1 p-4 font-mono text-sm text-[color:var(--da-color-fg)] bg-[color:var(--da-color-card-bg)] focus:outline-none focus:ring-2 focus:ring-inset focus:ring-[color:var(--da-color-accent)] resize-none",spellCheck:"false"})]})]}),(0,e.jsxs)("div",{className:"w-1/2 flex flex-col bg-[color:var(--da-color-surface)] overflow-hidden",children:[(0,e.jsx)("div",{className:"px-6 py-4 border-b border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-card-bg)]",children:(0,e.jsxs)("div",{className:"flex items-center justify-between",children:[(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-lg font-semibold text-[color:var(--da-color-fg)]",children:o("\u6AA2\u67E5\u7D50\u679C","Check Results")}),(0,e.jsx)("p",{className:"text-xs text-[color:var(--da-color-muted)] mt-1",role:"status","aria-live":"polite","data-testid":"validation-status",children:d.errors.length===0?o("YAML \u8A9E\u6CD5\u8207\u7D50\u69CB\u6B63\u78BA","YAML syntax and structure OK"):o(`\u627E\u5230 ${d.errors.length} \u500B\u932F\u8AA4`,`${d.errors.length} error(s) found`)})]}),(0,e.jsx)("div",{className:"text-right",children:(0,e.jsx)("div",{className:"text-3xl font-bold",children:d.valid?(0,e.jsx)("span",{className:"text-[color:var(--da-color-success)]",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})}):(0,e.jsx)("span",{className:"text-[color:var(--da-color-error)]",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2717"})})})})]})}),(0,e.jsxs)("div",{className:"flex-1 overflow-y-auto p-6 space-y-6",role:"region","aria-label":o("\u6AA2\u67E5\u7D50\u679C","Check results"),tabIndex:0,children:[(0,e.jsx)("div",{"data-testid":"semantics-not-checked",className:"text-xs text-[color:var(--da-color-muted)]",children:o("\u672C\u5DE5\u5177\u4EE5 js-yaml \u6AA2\u67E5 YAML \u8A9E\u6CD5\u8207\u7D50\u69CB\uFF0C\u8207 exporter \u4F7F\u7528\u7684 yaml.v3 \u5728\u5C11\u6578\u908A\u89D2\u8B80\u6CD5\u4E0D\u540C\uFF08\u5169\u500B\u65B9\u5411\u90FD\u53EF\u80FD\uFF1A\u9019\u88E1\u901A\u904E\u4F46 exporter \u62D2\u6536\uFF0C\u6216\u53CD\u4E4B\uFF09\uFF0C\u4E5F\u4E0D\u6AA2\u67E5\u8A9E\u610F\uFF1B\u662F\u5426\u751F\u6548\u4EE5 exporter \u70BA\u6E96\u3002","This tool checks YAML syntax and structure with js-yaml, which differs from the exporter's yaml.v3 on a few edge cases (in both directions: passing here but rejected by the exporter, or the reverse), and it does not check semantics. Whether a config takes effect is up to the exporter.")}),d.errors.length>0&&(0,e.jsxs)("div",{children:[(0,e.jsxs)("h3",{className:"font-semibold text-[color:var(--da-color-error)] mb-3 flex items-center gap-2",children:[(0,e.jsx)("span",{className:"text-lg",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2717"})})," ",o("\u932F\u8AA4","Errors")," (",d.errors.length,")"]}),(0,e.jsx)("div",{className:"space-y-2",children:d.errors.map((t,l)=>(0,e.jsxs)("div",{className:"bg-[color:var(--da-color-error-soft)] border border-[color:var(--da-color-error)] rounded-md p-3 text-sm text-[color:var(--da-color-error)]",children:[(0,e.jsx)("div",{className:"font-mono font-bold text-xs text-[color:var(--da-color-error)] mb-1",children:t.rule}),(0,e.jsx)("div",{children:t.message})]},l))})]}),d.valid&&(0,e.jsxs)("div",{className:"bg-[color:var(--da-color-success-soft)] border border-[color:var(--da-color-success)] rounded-md p-4 text-center",children:[(0,e.jsx)("div",{className:"text-2xl mb-2",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})}),(0,e.jsx)("div",{className:"text-[color:var(--da-color-success)] font-semibold",children:o("YAML \u53EF\u89E3\u6790","YAML parses cleanly")}),(0,e.jsx)("div",{className:"text-xs text-[color:var(--da-color-success)] mt-2",children:o(`${d.summary.tenants} \u500B\u79DF\u6236`,`${d.summary.tenants} tenant(s)`)})]})]})]})]})]})}var E=document.getElementById("root");E&&(0,R.createRoot)(E).render(y.default.createElement(Y,{scope:"playground"},y.default.createElement(x)));
//# sourceMappingURL=playground.js.map
