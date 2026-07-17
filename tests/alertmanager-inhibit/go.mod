// Test-scoped module: keeps the prometheus/alertmanager dependency OUT of the
// two production Go modules (threshold-exporter, tenant-api), whose go.mod feeds
// image builds, Trivy scans and release provenance. Nothing here is ever built
// into an artifact — it exists only to evaluate checked-in Alertmanager configs
// against Alertmanager's own parser + matcher implementation.
//
// The alertmanager dependency is pinned to the version k8s actually deploys
// (see k8s/03-monitoring/deployment-alertmanager.yaml) so the semantics under
// test are the semantics that run in production.
module github.com/vencil/dynamic-alerting/tests/alertmanager-inhibit

go 1.25.0

require (
	github.com/prometheus/alertmanager v0.33.1
	gopkg.in/yaml.v3 v3.0.1
)

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/golang-jwt/jwt/v5 v5.3.0 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/jpillora/backoff v1.0.0 // indirect
	github.com/kr/text v0.2.0 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/mwitkow/go-conntrack v0.0.0-20190716064945-2f068394615f // indirect
	github.com/prometheus/client_golang v1.23.2 // indirect
	github.com/prometheus/client_model v0.6.2 // indirect
	github.com/prometheus/common v0.67.5 // indirect
	github.com/prometheus/procfs v0.16.1 // indirect
	github.com/rogpeppe/go-internal v1.14.1 // indirect
	go.yaml.in/yaml/v2 v2.4.4 // indirect
	golang.org/x/net v0.55.0 // indirect
	golang.org/x/oauth2 v0.35.0 // indirect
	golang.org/x/sys v0.45.0 // indirect
	golang.org/x/text v0.37.0 // indirect
	google.golang.org/protobuf v1.36.11 // indirect
)
