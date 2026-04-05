module github.com/vencil/tenant-api

go 1.26.1

require (
	github.com/go-chi/chi/v5 v5.2.5
	github.com/vencil/threshold-exporter v0.0.0
	gopkg.in/yaml.v3 v3.0.1
)

replace github.com/vencil/threshold-exporter => ../threshold-exporter/app
