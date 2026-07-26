import{a as D}from"./chunk-ZHLICTNA.js";import{a as y,b as G,c as F,d as I,e as L}from"./chunk-6CZKKW3I.js";var T=y(G(),1),Y=y(F(),1);var u=y(G(),1);var r=window.__t||((s,l)=>l),H=[{id:"tier",label:()=>r("\u90E8\u7F72\u5C64\u7D1A","Deployment Tier")},{id:"environment",label:()=>r("\u904B\u884C\u74B0\u5883","Environment")},{id:"tenants",label:()=>r("Tenant \u6578\u91CF","Tenant Count")},{id:"auth",label:()=>r("\u8A8D\u8B49 (Tier 2)","Authentication (Tier 2)")},{id:"packs",label:()=>r("Rule Packs","Rule Packs")},{id:"review",label:()=>r("\u6AA2\u8996\u8207\u7522\u51FA","Review & Generate")}],N=[{id:"tier1",name:r("Tier 1\uFF1AGit-Native","Tier 1: Git-Native"),desc:r("\u7D14 GitOps\uFF1AYAML + da-tools CLI + Helm values","Pure GitOps: YAML + da-tools CLI + Helm values"),features:[r("threshold-exporter \xD7 2 (HA)","threshold-exporter \xD7 2 (HA)"),r("Prometheus + Alertmanager (Helm)","Prometheus + Alertmanager (Helm)"),r("ConfigMap \u7BA1\u7406\u544A\u8B66\u898F\u5247","ConfigMap for alert rules"),r("\u7121 Portal / API","No Portal / API")],icon:"\u{1F4E6}",cost:r("\u4F4E","Low")},{id:"tier2",name:r("Tier 2\uFF1APortal + API","Tier 2: Portal + API"),desc:r("\u5B8C\u6574\u529F\u80FD\uFF1ATier 1 + da-portal + tenant-api + OAuth2","Full-featured: Tier 1 + da-portal + tenant-api + OAuth2"),features:[r("\u6240\u6709 Tier 1 \u529F\u80FD","All Tier 1 features"),r("da-portal UI (\u81EA\u8A17\u7BA1\u6216 SaaS)","da-portal UI (self-hosted or SaaS)"),r("tenant-api\uFF08RBAC + \u71B1\u66F4\u65B0\uFF09","tenant-api (RBAC + hot-reload)"),r("oauth2-proxy\uFF08GitHub / Google / OIDC\uFF09","oauth2-proxy (GitHub / Google / OIDC)")],icon:"\u{1F310}",cost:r("\u4E2D","Medium")}],P=[{id:"local",label:r("\u672C\u5730\u958B\u767C (Kind/Minikube)","Local Dev (Kind/Minikube)"),icon:"\u{1F4BB}",desc:r("2\u20134 CPU, 4\u20138 GB RAM, \u7C21\u5316\u90E8\u7F72","2\u20134 CPU, 4\u20138 GB RAM, simplified")},{id:"staging",label:r("\u6E2C\u8A66\u74B0\u5883 (Staging)","Staging Environment"),icon:"\u{1F9EA}",desc:r("4\u20138 CPU, 16 GB RAM, HA \u5C31\u7DD2","4\u20138 CPU, 16 GB RAM, HA-ready")},{id:"production",label:r("\u751F\u7522\u74B0\u5883 (Production)","Production Environment"),icon:"\u{1F680}",desc:r("8+ CPU, 32+ GB RAM, \u591A\u5340\u57DF","8+ CPU, 32+ GB RAM, multi-region")}],k=[{id:"small",label:r("\u5C0F\u578B (1\u201310)","Small (1\u201310)"),icon:"1\uFE0F\u20E3",replicas:{exporter:1,prometheus:1,alertmanager:1},retention:"7d",cardinality:500},{id:"medium",label:r("\u4E2D\u578B (10\u201350)","Medium (10\u201350)"),icon:"\u{1F4CA}",replicas:{exporter:2,prometheus:2,alertmanager:3},retention:"14d",cardinality:2e3},{id:"large",label:r("\u5927\u578B (50+)","Large (50+)"),icon:"\u{1F4C8}",replicas:{exporter:3,prometheus:3,alertmanager:3},retention:"30d",cardinality:5e3}],$=[{id:"github",label:"GitHub",icon:"\u{1F419}",desc:r("\u4F7F\u7528 GitHub \u5E33\u6236\u767B\u5165","Sign in with GitHub account"),scopes:["user:email","read:org"]},{id:"google",label:"Google",icon:"\u{1F535}",desc:r("\u4F7F\u7528 Google \u5E33\u6236\u767B\u5165","Sign in with Google account"),scopes:["openid","email","profile"]},{id:"oidc",label:"OIDC / Keycloak",icon:"\u{1F510}",desc:r("\u81EA\u8A17\u7BA1 OIDC\uFF08Keycloak\u3001Okta \u7B49\uFF09","Self-hosted OIDC (Keycloak, Okta, etc.)"),scopes:["openid","profile","email"]},{id:"gitlab",label:"GitLab",icon:"\u{1F98A}",desc:r("\u4F7F\u7528 GitLab \u5E33\u6236\u767B\u5165","Sign in with GitLab account"),scopes:["openid","profile","email"]}],A=[{id:"mariadb",label:"MariaDB/MySQL",category:"database",icon:"\u{1F42C}"},{id:"postgresql",label:"PostgreSQL",category:"database",icon:"\u{1F418}"},{id:"redis",label:"Redis",category:"database",icon:"\u{1F534}"},{id:"mongodb",label:"MongoDB",category:"database",icon:"\u{1F343}"},{id:"elasticsearch",label:"Elasticsearch",category:"database",icon:"\u{1F50E}"},{id:"oracle",label:"Oracle",category:"database",icon:"\u{1F3DB}\uFE0F"},{id:"db2",label:"DB2",category:"database",icon:"\u{1F537}"},{id:"clickhouse",label:"ClickHouse",category:"database",icon:"\u{1F5B1}\uFE0F"},{id:"kafka",label:"Kafka",category:"messaging",icon:"\u{1F4E8}"},{id:"rabbitmq",label:"RabbitMQ",category:"messaging",icon:"\u{1F430}"},{id:"jvm",label:"JVM",category:"runtime",icon:"\u2615"},{id:"nginx",label:"Nginx",category:"webserver",icon:"\u{1F310}"},{id:"kubernetes",label:"Kubernetes",category:"infrastructure",icon:"\u2388"}];var W={prometheus:"prom/prometheus:v3.13.1",alertmanager:"prom/alertmanager:v0.33.1",configReloader:"quay.io/prometheus-operator/prometheus-config-reloader:v0.92.1",oauth2Proxy:"quay.io/oauth2-proxy/oauth2-proxy:v7.15.3",thresholdExporter:"ghcr.io/vencil/threshold-exporter:v2.9.0",daPortal:"ghcr.io/vencil/da-portal:v2.9.0",tenantApi:"ghcr.io/vencil/tenant-api:v2.7.0"},w={...W,...window.__PLATFORM_DATA?.images||{}};function j(s){let l=w[s];if(!l)throw new Error(`images.js: unknown image key '${s}'. Known keys: ${Object.keys(w).join(", ")}`);let a=l.lastIndexOf(":");return a<0||l.indexOf("/",a)!==-1?{repository:l,tag:""}:{repository:l.slice(0,a),tag:l.slice(a+1)}}function v(s){return j(s).repository}function b(s){return j(s).tag}function B(s){let{tier:l,environment:a,tenantSize:i,auth:f,packs:h}=s,c=k.find(m=>m.id===i),S=l==="tier2";return`# Generated Helm values for ${N.find(m=>m.id===l)?.name}
# Environment: ${P.find(m=>m.id===a)?.label}
# Tenant count: ${c?.label}
# Generated: ${new Date().toISOString().split("T")[0]}

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# threshold-exporter Configuration
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

thresholdExporter:
  replicaCount: ${c?.replicas.exporter||2}
  image:
    repository: ${v("thresholdExporter")}
    tag: ${b("thresholdExporter")}
    pullPolicy: IfNotPresent

  resources:
    requests:
      cpu: ${a==="local"?"100m":a==="staging"?"250m":"500m"}
      memory: ${a==="local"?"128Mi":a==="staging"?"256Mi":"512Mi"}
    limits:
      cpu: ${a==="local"?"200m":a==="staging"?"500m":"1000m"}
      memory: ${a==="local"?"256Mi":a==="staging"?"512Mi":"1Gi"}

  # Hot-reload SHA-256 validation
  configValidation:
    enabled: true
    sha256: "" # Set after generating config

  # Cardinality guard: per-tenant max metrics
  cardinalityGuard:
    enabled: true
    maxPerTenant: ${c?.cardinality||2e3}

  # Three-state operating modes: normal / silent / maintenance
  tripleState:
    enabled: true
    defaultMode: normal

prometheus:
  replicaCount: ${c?.replicas.prometheus||2}
  image:
    repository: ${v("prometheus")}
    tag: ${b("prometheus")}

  resources:
    requests:
      cpu: ${a==="local"?"250m":a==="staging"?"500m":"1000m"}
      memory: ${a==="local"?"512Mi":a==="staging"?"1Gi":"2Gi"}
    limits:
      cpu: ${a==="local"?"500m":a==="staging"?"1000m":"2000m"}
      memory: ${a==="local"?"1Gi":a==="staging"?"2Gi":"4Gi"}

  # Data retention based on tenant size
  retention: "${c?.retention||"14d"}"

  # Rule packs from ConfigMap + Projected Volume
  ruleConfigMaps:
    - name: platform-rules
      key: rules.yaml
    ${h.length>0?`# Auto-mounted rule packs via Projected Volume:
    # ${h.map(m=>`- name: rules-${m}`).join(`
    # `)}`:""}

  # ServiceMonitor for threshold-exporter
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s

alertmanager:
  replicaCount: ${c?.replicas.alertmanager||3}
  image:
    repository: ${v("alertmanager")}
    tag: ${b("alertmanager")}

  resources:
    requests:
      cpu: ${a==="local"?"100m":a==="staging"?"250m":"500m"}
      memory: ${a==="local"?"128Mi":a==="staging"?"256Mi":"512Mi"}
    limits:
      cpu: ${a==="local"?"200m":a==="staging"?"500m":"1000m"}
      memory: ${a==="local"?"256Mi":a==="staging"?"512Mi":"1Gi"}

  # Dynamic route generation + config reload sidecar
  configReload:
    enabled: true
    image: ${w.configReloader}

  # Cluster mode for HA
  clustering:
    enabled: ${a!=="local"?"true":"false"}
    peers:
      enabled: ${a!=="local"?"true":"false"}

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Platform Common Settings
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

platform:
  # Environment label for metric routing
  environment: ${a}

  # Namespace isolation
  namespaces:
    monitoring: monitoring
    # Add tenant namespaces as needed

  # Logging level
  logLevel: ${a==="production"?"warn":"info"}

  # Bilingual support (zh/en annotations)
  i18n:
    enabled: true
    defaultLanguage: en

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Tier 2: Portal + API Configuration
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
${S?`
daPortal:
  enabled: true
  replicaCount: ${a==="local"?1:c?.replicas.exporter||2}
  image:
    repository: ${v("daPortal")}
    tag: ${b("daPortal")}

  resources:
    requests:
      cpu: ${a==="local"?"100m":"250m"}
      memory: ${a==="local"?"256Mi":"512Mi"}
    limits:
      cpu: ${a==="local"?"200m":"500m"}
      memory: ${a==="local"?"512Mi":"1Gi"}

  # Portal ingress
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: da-portal.example.com
        paths:
          - path: /
            pathType: Prefix

tenantAPI:
  enabled: true
  replicaCount: ${a==="local"?1:2}
  image:
    repository: ${v("tenantApi")}
    tag: ${b("tenantApi")}

  resources:
    requests:
      cpu: ${a==="local"?"100m":"250m"}
      memory: ${a==="local"?"128Mi":"256Mi"}
    limits:
      cpu: ${a==="local"?"200m":"500m"}
      memory: ${a==="local"?"256Mi":"512Mi"}

  # RBAC hot-reload via atomic.Value
  rbac:
    enabled: true
    cacheRefreshInterval: 30s

  # Tenant API ingress
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: api.dynamic-alerting.example.com
        paths:
          - path: /v1
            pathType: Prefix

oauth2Proxy:
  enabled: true
  replicaCount: ${a==="local"?1:2}
  image:
    repository: ${v("oauth2Proxy")}
    tag: ${b("oauth2Proxy")}

  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 100m
      memory: 128Mi

  # OAuth2 provider configuration
  config:
    provider: "${f||"oidc"}"
    ${f==="github"?`oauth_url: "https://github.com/login/oauth/authorize"
    token_url: "https://github.com/login/oauth/access_token"
    user_info_url: "https://api.github.com/user"
    scopes: ["user:email", "read:org"]`:f==="google"?`oauth_url: "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: "https://oauth2.googleapis.com/token"
    user_info_url: "https://www.googleapis.com/oauth2/v2/userinfo"
    scopes: ["openid", "email", "profile"]`:f==="gitlab"?`oauth_url: "https://gitlab.com/oauth/authorize"
    token_url: "https://gitlab.com/oauth/token"
    user_info_url: "https://gitlab.com/api/v4/user"
    scopes: ["openid", "profile", "email"]`:`oauth_url: "https://your-keycloak.com/auth/realms/master/protocol/openid-connect/auth"
    token_url: "https://your-keycloak.com/auth/realms/master/protocol/openid-connect/token"
    user_info_url: "https://your-keycloak.com/auth/realms/master/protocol/openid-connect/userinfo"
    scopes: ["openid", "profile", "email"]`}
    client_id: "" # Set in secrets
    client_secret: "" # Set in secrets

  # Cookie configuration for session persistence
  cookie:
    domain: example.com
    secure: true
    httponly: true
    samesite: Lax
`:`
# Tier 1: Portal and API disabled
daPortal:
  enabled: false

tenantAPI:
  enabled: false

oauth2Proxy:
  enabled: false
`}

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Networking & Storage
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

persistence:
  # Prometheus TSDB storage
  prometheus:
    enabled: true
    storageClass: standard
    size: ${a==="local"?"5Gi":a==="staging"?"20Gi":"100Gi"}

  # Alertmanager state
  alertmanager:
    enabled: true
    storageClass: standard
    size: ${a==="local"?"1Gi":"5Gi"}

networkPolicy:
  enabled: ${a==="production"?"true":"false"}
  ingressNamespaces:
    - monitoring

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Observability & Debugging
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

monitoring:
  # Prometheus scrape config for self-monitoring
  prometheus:
    enabled: true
    interval: 60s

  # Log aggregation hints
  logging:
    level: ${a==="production"?"warn":"info"}
    format: json

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Security
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

rbac:
  create: true

serviceAccount:
  create: true
  name: threshold-exporter

podSecurityPolicy:
  enabled: ${a==="production"?"true":"false"}

# Secrets for OAuth2 (if Tier 2)
secrets:
  ${S?`oauth2:
    clientId: "" # Fill from secrets manager
    clientSecret: "" # Fill from secrets manager
  `:""}# Add any additional secrets here
`}var e=y(I(),1),t=window.__t||((s,l)=>l);function E(){let[s,l]=(0,u.useState)(0),[a,i]=(0,u.useState)({tier:"tier1",environment:"staging",tenantSize:"medium",auth:"github",packs:[]}),[f,h]=(0,u.useState)(!1),{copied:c,copy:S,reset:R}=D(),[m,O]=(0,u.useState)(!1),p=H.map((o,n)=>o.id==="auth"&&a.tier==="tier1"?null:o).filter(Boolean),d=s<p.length?s:0,g=p[d],M=d+1,U=()=>{if(g.id==="packs"&&a.packs.length===0){O(!0);return}O(!1),d<p.length-1?l(s+1):h(!0)},z=()=>{d>0&&l(s-1)},q=()=>{l(0),i({tier:"tier1",environment:"staging",tenantSize:"medium",auth:"github",packs:[]}),h(!1),R()},_=(0,u.useMemo)(()=>B(a),[a]),K=()=>S(_),x=(0,u.useMemo)(()=>({tier:N.find(o=>o.id===a.tier)?.name||"",environment:P.find(o=>o.id===a.environment)?.label||"",tenantSize:k.find(o=>o.id===a.tenantSize)?.label||"",auth:a.tier==="tier2"?$.find(o=>o.id===a.auth)?.label:t("N/A","N/A"),packs:a.packs.length>0?a.packs.join(", "):t("\u7121","None")}),[a]);return(0,e.jsx)("div",{className:"min-h-screen bg-gradient-to-br from-[color:var(--da-color-bg)] to-[color:var(--da-color-surface-hover)] p-8",children:(0,e.jsxs)("div",{className:"max-w-4xl mx-auto",children:[(0,e.jsx)("h1",{className:"text-4xl font-bold text-[color:var(--da-color-fg)] mb-2",children:t("\u90E8\u7F72\u8A2D\u5B9A\u7CBE\u9748","Deployment Profile Wizard")}),(0,e.jsx)("p",{className:"text-[color:var(--da-color-muted)] mb-8",children:t("\u900F\u904E\u5E7E\u500B\u7C21\u55AE\u6B65\u9A5F\uFF0C\u7522\u751F\u7B26\u5408\u4F60\u9700\u6C42\u7684 Helm values \u8A2D\u5B9A","Generate Helm values tailored to your deployment requirements in just a few steps")}),f?(0,e.jsxs)(e.Fragment,{children:[(0,e.jsxs)("div",{className:"flex items-center justify-between mb-6",children:[(0,e.jsx)("h2",{className:"text-2xl font-bold text-[color:var(--da-color-fg)]",children:t("Helm Values \u8A2D\u5B9A","Generated Helm Values")}),(0,e.jsx)("button",{onClick:q,className:"text-sm text-[color:var(--da-color-accent)] hover:underline",children:t("\u91CD\u65B0\u8A2D\u5B9A","Start Over")})]}),(0,e.jsxs)("div",{className:"bg-white rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] overflow-hidden mb-6",children:[(0,e.jsx)("div",{className:"bg-slate-900 text-slate-100 p-6 font-mono text-sm overflow-x-auto max-h-96",children:(0,e.jsx)("pre",{className:"whitespace-pre-wrap break-words",children:_})}),(0,e.jsxs)("div",{className:"bg-[color:var(--da-color-surface-hover)] border-t border-[color:var(--da-color-surface-border)] p-4 flex items-center justify-between",children:[(0,e.jsx)("p",{className:"text-xs text-[color:var(--da-color-muted)]",children:t("\u8907\u88FD\u4E0B\u65B9\u5167\u5BB9\u5230\u4F60\u7684 values.yaml \u6A94\u6848","Copy the above content to your values.yaml file")}),(0,e.jsx)("button",{onClick:K,className:`px-4 py-2 text-sm font-medium rounded-lg transition-all ${c?"bg-green-500 text-white":"bg-[color:var(--da-color-accent)] text-white hover:bg-[color:var(--da-color-accent-hover)]"}`,children:c?(0,e.jsxs)(e.Fragment,{children:[(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})," ",t("\u5DF2\u8907\u88FD","Copied")]}):t("\u8907\u88FD","Copy")})]})]}),(0,e.jsxs)("div",{className:"bg-[color:var(--da-color-accent-soft)] rounded-xl border border-[color:var(--da-color-accent-soft)] p-6 space-y-6",children:[(0,e.jsxs)("div",{children:[(0,e.jsx)("h3",{className:"font-semibold text-[color:var(--da-color-fg)] mb-4",children:t("\u5F8C\u7E8C\u6B65\u9A5F","Next Steps")}),(0,e.jsxs)("ol",{className:"text-sm text-[color:var(--da-color-fg)] space-y-2",children:[(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"1."}),(0,e.jsx)("span",{children:t("\u8907\u88FD\u4E0A\u65B9 values \u5230\u4F60\u7684 values.yaml \u6216 Helm chart \u76EE\u9304","Copy the values above to your values.yaml or Helm chart directory")})]}),(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"2."}),(0,e.jsx)("span",{children:t("\u6839\u64DA\u4F60\u7684\u74B0\u5883\u586B\u5165 example.com\u3001OAuth2 \u8A8D\u8B49\u8CC7\u8A0A\u7B49","Fill in example.com, OAuth2 credentials, and other environment-specific values")})]}),(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"3."}),(0,e.jsx)("span",{children:t("\u57F7\u884C\u9A57\u8B49\uFF1Ada-tools validate-config --config-dir ./conf.d","Run validation: da-tools validate-config --config-dir ./conf.d")})]}),(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"4."}),(0,e.jsx)("span",{children:t("\u4F7F\u7528 Helm \u90E8\u7F72\uFF1Ahelm install threshold-exporter oci://ghcr.io/vencil/charts/threshold-exporter -f values.yaml -n monitoring","Deploy with Helm: helm install threshold-exporter oci://ghcr.io/vencil/charts/threshold-exporter -f values.yaml -n monitoring")})]})]})]}),a.tier==="tier2"&&(0,e.jsxs)("div",{className:"border-t border-[color:var(--da-color-accent-soft)] pt-4",children:[(0,e.jsx)("h4",{className:"font-semibold text-[color:var(--da-color-fg)] mb-3",children:t("Tier 2\uFF1A\u5EFA\u7ACB\u9996\u500B\u79DF\u6236","Tier 2: Create Your First Tenant")}),(0,e.jsxs)("ol",{className:"text-sm text-[color:var(--da-color-fg)] space-y-2",children:[(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"5."}),(0,e.jsxs)("span",{children:[t("\u4F7F\u7528 template-gallery \u6216 playground \u5EFA\u7ACB\u9996\u500B\u79DF\u6236\u914D\u7F6E\u3002\u67E5\u770B","Create your first tenant config using template-gallery or playground. See")," ",(0,e.jsx)("a",{href:"../../assets/jsx-loader.html?component=template-gallery",className:"text-[color:var(--da-color-accent)] underline hover:text-[color:var(--da-color-accent-hover)]",children:t("\u7BC4\u672C\u5EAB","template-gallery")})]})]}),(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"6."}),(0,e.jsxs)("span",{children:[t("\u900F\u904E Portal \u958B\u555F ","Open "),(0,e.jsx)("a",{href:"../../assets/jsx-loader.html?component=tenant-manager",className:"text-[color:var(--da-color-accent)] underline hover:text-[color:var(--da-color-accent-hover)]",children:t("\u79DF\u6236\u7BA1\u7406\u5DE5\u5177","tenant-manager")}),t(" \u4F86\u7BA1\u7406\u79DF\u6236\u8207\u7FA4\u7D44\u6210\u54E1\u8CC7\u683C"," to manage tenant and group membership")]})]}),(0,e.jsxs)("li",{className:"flex gap-3",children:[(0,e.jsx)("span",{className:"font-bold text-[color:var(--da-color-accent)]",children:"7."}),(0,e.jsxs)("span",{children:[t("\u67E5\u770B ","Review "),(0,e.jsx)("a",{href:"../../getting-started/",className:"text-[color:var(--da-color-accent)] underline hover:text-[color:var(--da-color-accent-hover)]",children:t("\u5B8C\u6574\u5165\u9580\u6307\u5357","complete getting-started guide")}),t(" \u6DF1\u5165\u4E86\u89E3\u591A\u79DF\u6236\u544A\u8B66\u914D\u7F6E"," to learn more about multi-tenant alerting")]})]})]})]})]})]}):(0,e.jsxs)(e.Fragment,{children:[(0,e.jsxs)("div",{className:"mb-8",children:[(0,e.jsxs)("div",{className:"flex items-center justify-between mb-3",children:[(0,e.jsx)("h3",{className:"text-sm font-semibold text-[color:var(--da-color-fg)]",children:t("\u9032\u5EA6","Progress")}),(0,e.jsxs)("span",{className:"text-xs text-[color:var(--da-color-muted)]",children:[M,"/",p.length]})]}),(0,e.jsx)("div",{className:"h-2 bg-[color:var(--da-color-tag-bg)] rounded-full overflow-hidden",children:(0,e.jsx)("div",{className:"h-full bg-[color:var(--da-color-accent)] transition-all duration-300",style:{width:`${M/p.length*100}%`}})})]}),(0,e.jsx)("div",{className:"flex gap-2 mb-8 overflow-x-auto pb-2",role:"list","aria-label":t("\u90E8\u7F72\u8A2D\u5B9A\u6B65\u9A5F","Deployment configuration steps"),children:p.map((o,n)=>(0,e.jsxs)("button",{role:"listitem","aria-current":n===d?"step":void 0,onClick:()=>l(n),className:`flex-shrink-0 px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${n===d?"bg-[color:var(--da-color-accent)] text-white":n<d?"bg-green-500 text-white":"bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)] "}`,children:[n<d?(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"}):n+1," ",o.label()]},o.id))}),(0,e.jsxs)("div",{className:"bg-white rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-8 mb-8",children:[g.id==="tier"&&(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-xl font-semibold text-[color:var(--da-color-fg)] mb-6",children:t("\u9078\u64C7\u90E8\u7F72\u5C64\u7D1A","Choose Deployment Tier")}),(0,e.jsx)("div",{className:"space-y-4",children:N.map(o=>(0,e.jsx)("button",{onClick:()=>i({...a,tier:o.id}),className:`w-full p-5 rounded-xl border-2 text-left transition-all ${a.tier===o.id?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] ":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] "}`,children:(0,e.jsxs)("div",{className:"flex items-start gap-4",children:[(0,e.jsx)("span",{className:"text-3xl",children:o.icon}),(0,e.jsxs)("div",{className:"flex-1",children:[(0,e.jsx)("h3",{className:"font-semibold text-[color:var(--da-color-fg)]",children:o.name}),(0,e.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mt-1",children:o.desc}),(0,e.jsx)("ul",{className:"text-xs text-[color:var(--da-color-muted)] mt-3 space-y-1",children:o.features.map((n,C)=>(0,e.jsxs)("li",{className:"flex items-center gap-2",children:[(0,e.jsx)("span",{className:"text-[color:var(--da-color-muted)]",children:"\u2022"})," ",n]},C))}),(0,e.jsxs)("p",{className:"text-xs font-medium text-[color:var(--da-color-muted)] mt-3",children:[t("\u6210\u672C","Cost"),": ",o.cost]})]}),a.tier===o.id&&(0,e.jsx)("span",{className:"text-[color:var(--da-color-accent)] font-bold",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})})]})},o.id))})]}),g.id==="environment"&&(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-xl font-semibold text-[color:var(--da-color-fg)] mb-6",children:t("\u9078\u64C7\u904B\u884C\u74B0\u5883","Choose Environment")}),(0,e.jsx)("div",{className:"space-y-3",children:P.map(o=>(0,e.jsx)("button",{onClick:()=>i({...a,environment:o.id}),className:`w-full p-4 rounded-xl border-2 text-left transition-all ${a.environment===o.id?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] ":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] "}`,children:(0,e.jsxs)("div",{className:"flex items-center gap-3",children:[(0,e.jsx)("span",{className:"text-2xl",children:o.icon}),(0,e.jsxs)("div",{className:"flex-1",children:[(0,e.jsx)("div",{className:"font-medium text-[color:var(--da-color-fg)]",children:o.label}),(0,e.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)] mt-0.5",children:o.desc})]}),a.environment===o.id&&(0,e.jsx)("span",{className:"text-[color:var(--da-color-accent)]",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})})]})},o.id))})]}),g.id==="tenants"&&(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-xl font-semibold text-[color:var(--da-color-fg)] mb-6",children:t("\u9078\u64C7 Tenant \u6578\u91CF","Choose Tenant Count")}),(0,e.jsx)("div",{className:"space-y-3",children:k.map(o=>(0,e.jsx)("button",{onClick:()=>i({...a,tenantSize:o.id}),className:`w-full p-4 rounded-xl border-2 text-left transition-all ${a.tenantSize===o.id?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] ":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] "}`,children:(0,e.jsxs)("div",{className:"flex items-center gap-3",children:[(0,e.jsx)("span",{className:"text-2xl",children:o.icon}),(0,e.jsxs)("div",{className:"flex-1",children:[(0,e.jsx)("div",{className:"font-medium text-[color:var(--da-color-fg)]",children:o.label}),(0,e.jsxs)("div",{className:"text-xs text-[color:var(--da-color-muted)] mt-2 space-y-1",children:[(0,e.jsxs)("div",{children:[t("\u8907\u88FD\u6578","Replicas"),": exporter=",o.replicas.exporter,", prometheus=",o.replicas.prometheus,", alertmanager=",o.replicas.alertmanager]}),(0,e.jsxs)("div",{children:[t("\u4FDD\u7559\u671F","Retention"),": ",o.retention," | ",t("\u57FA\u6578\u4E0A\u9650","Cardinality"),": ",o.cardinality]})]})]}),a.tenantSize===o.id&&(0,e.jsx)("span",{className:"text-[color:var(--da-color-accent)]",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})})]})},o.id))})]}),g.id==="auth"&&a.tier==="tier2"&&(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-xl font-semibold text-[color:var(--da-color-fg)] mb-6",children:t("\u9078\u64C7 OAuth2 \u4F9B\u61C9\u5546","Choose OAuth2 Provider")}),(0,e.jsx)("div",{className:"space-y-3",children:$.map(o=>(0,e.jsx)("button",{onClick:()=>i({...a,auth:o.id}),className:`w-full p-4 rounded-xl border-2 text-left transition-all ${a.auth===o.id?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] ":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] "}`,children:(0,e.jsxs)("div",{className:"flex items-center gap-3",children:[(0,e.jsx)("span",{className:"text-2xl",children:o.icon}),(0,e.jsxs)("div",{className:"flex-1",children:[(0,e.jsx)("div",{className:"font-medium text-[color:var(--da-color-fg)]",children:o.label}),(0,e.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)] mt-1",children:o.desc}),(0,e.jsxs)("div",{className:"text-xs text-[color:var(--da-color-muted)] mt-2",children:[t("\u7BC4\u570D","Scopes"),": ",o.scopes.join(", ")]})]}),a.auth===o.id&&(0,e.jsx)("span",{className:"text-[color:var(--da-color-accent)]",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})})]})},o.id))})]}),g.id==="packs"&&(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-xl font-semibold text-[color:var(--da-color-fg)] mb-2",children:t("\u9078\u64C7 Rule Packs","Select Rule Packs")}),(0,e.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mb-6",children:t("\u9078\u64C7\u4F60\u9700\u8981\u76E3\u63A7\u7684\u6280\u8853\u68E7\uFF08\u53EF\u9078\uFF0C\u7559\u7A7A\u5247\u4E0D\u542B\u984D\u5916 Rule Pack\uFF09","Select the technology stacks you need to monitor (optional, leave empty for defaults only)")}),(0,e.jsxs)("div",{className:"mb-4 flex gap-2",children:[(0,e.jsx)("button",{onClick:()=>i({...a,packs:A.map(o=>o.id)}),className:"px-3 py-1.5 text-xs font-medium bg-[color:var(--da-color-accent)] text-white rounded-lg hover:bg-[color:var(--da-color-accent-hover)]",children:t("\u5168\u9078","Select All")}),(0,e.jsx)("button",{onClick:()=>i({...a,packs:[]}),className:"px-3 py-1.5 text-xs font-medium bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] rounded-lg hover:bg-[color:var(--da-color-surface-hover)]",children:t("\u6E05\u9664","Clear")})]}),(0,e.jsx)("div",{className:"grid grid-cols-2 md:grid-cols-3 gap-3",children:A.map(o=>(0,e.jsxs)("button",{onClick:()=>{let n=a.packs.includes(o.id)?a.packs.filter(C=>C!==o.id):[...a.packs,o.id];i({...a,packs:n})},className:`p-3 rounded-lg border-2 text-center transition-all ${a.packs.includes(o.id)?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] ":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] "}`,children:[(0,e.jsx)("div",{className:"text-2xl mb-2",children:o.icon}),(0,e.jsx)("div",{className:"text-xs font-medium text-[color:var(--da-color-fg)]",children:o.label}),a.packs.includes(o.id)&&(0,e.jsx)("div",{className:"text-[color:var(--da-color-accent)] text-sm mt-1",children:(0,e.jsx)("span",{"aria-hidden":"true",children:"\u2713"})})]},o.id))}),m&&(0,e.jsx)("div",{role:"alert",className:"mt-4 p-3 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)] rounded-lg text-sm text-amber-800",children:t("\u8ACB\u81F3\u5C11\u9078\u64C7\u4E00\u500B Rule Pack\u3002\u6C92\u6709 Rule Pack \u7684\u90E8\u7F72\u5C07\u7121\u6CD5\u7522\u751F\u6709\u610F\u7FA9\u7684\u544A\u8B66\u898F\u5247\u3002","Please select at least one Rule Pack. A deployment without Rule Packs will not generate meaningful alerting rules.")})]}),g.id==="review"&&(0,e.jsxs)("div",{children:[(0,e.jsx)("h2",{className:"text-xl font-semibold text-[color:var(--da-color-fg)] mb-6",children:t("\u6AA2\u8996\u6458\u8981","Review Summary")}),(0,e.jsxs)("div",{className:"bg-[color:var(--da-color-surface-hover)] rounded-lg p-6 space-y-4",children:[(0,e.jsxs)("div",{className:"flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]",children:[(0,e.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-muted)]",children:t("\u90E8\u7F72\u5C64\u7D1A","Deployment Tier")}),(0,e.jsx)("span",{className:"font-semibold text-[color:var(--da-color-fg)]",children:x.tier})]}),(0,e.jsxs)("div",{className:"flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]",children:[(0,e.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-muted)]",children:t("\u904B\u884C\u74B0\u5883","Environment")}),(0,e.jsx)("span",{className:"font-semibold text-[color:var(--da-color-fg)]",children:x.environment})]}),(0,e.jsxs)("div",{className:"flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]",children:[(0,e.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-muted)]",children:t("Tenant \u6578\u91CF","Tenant Count")}),(0,e.jsx)("span",{className:"font-semibold text-[color:var(--da-color-fg)]",children:x.tenantSize})]}),a.tier==="tier2"&&(0,e.jsxs)("div",{className:"flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]",children:[(0,e.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-muted)]",children:t("\u8A8D\u8B49","Authentication")}),(0,e.jsx)("span",{className:"font-semibold text-[color:var(--da-color-fg)]",children:x.auth})]}),(0,e.jsxs)("div",{className:"flex justify-between items-center",children:[(0,e.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-muted)]",children:t("Rule Packs","Rule Packs")}),(0,e.jsx)("span",{className:"font-semibold text-[color:var(--da-color-fg)]",children:x.packs})]})]}),(0,e.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mt-6",children:t("\u9EDE\u64CA\u300C\u7522\u751F\u8F38\u51FA\u300D\u4EE5\u67E5\u770B\u5B8C\u6574\u7684 Helm values\u3002\u4F60\u53EF\u4EE5\u8907\u88FD\u5167\u5BB9\u5230\u4F60\u7684 values.yaml \u6A94\u6848\u3002",'Click "Generate Output" below to see your complete Helm values. You can then copy it to your values.yaml file.')})]})]}),(0,e.jsxs)("div",{className:"flex items-center justify-between",children:[(0,e.jsxs)("button",{onClick:z,disabled:d===0,className:"px-4 py-2.5 text-sm font-medium text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-fg)] disabled:opacity-30",children:["\u2190 ",t("\u4E0A\u4E00\u6B65","Back")]}),(0,e.jsxs)("button",{onClick:U,className:"px-6 py-2.5 text-sm font-medium bg-[color:var(--da-color-accent)] text-white rounded-lg hover:bg-[color:var(--da-color-accent-hover)] transition-colors",children:[d===p.length-1?t("\u7522\u751F\u8F38\u51FA","Generate Output"):t("\u4E0B\u4E00\u6B65","Next")," \u2192"]})]})]})]})})}var V=document.getElementById("root");V&&(0,Y.createRoot)(V).render(T.default.createElement(L,{scope:"deployment-wizard"},T.default.createElement(E)));
//# sourceMappingURL=deployment-wizard.js.map
