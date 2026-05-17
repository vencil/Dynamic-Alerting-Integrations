package federation

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeProm is a stand-in Prometheus Series API. It distinguishes the
// validator's two probes by the `match[]` selector: the tenant-labelled
// probe carries `tenant!=""`, the existence probe carries no matcher.
type fakeProm struct {
	labelled []map[string]string // series for the `{tenant!=""}` probe
	all      []map[string]string // series for the bare-metric probe
	status   int                 // when non-zero, respond with this HTTP status
}

func (f *fakeProm) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if f.status != 0 {
			w.WriteHeader(f.status)
			return
		}
		data := f.all
		if strings.Contains(r.URL.Query().Get("match[]"), `tenant!=""`) {
			data = f.labelled
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": data})
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestNewAdmissionValidator_EmptyURLDisables(t *testing.T) {
	if NewAdmissionValidator("") != nil {
		t.Error("NewAdmissionValidator(\"\") should return nil (feature disabled)")
	}
	if NewAdmissionValidator("  ") != nil {
		t.Error("NewAdmissionValidator(blank) should return nil")
	}
	if NewAdmissionValidator("http://prometheus:9090") == nil {
		t.Error("NewAdmissionValidator(url) should return a validator")
	}
}

func TestAdmissionCheck(t *testing.T) {
	tests := []struct {
		name      string
		fake      fakeProm
		wantState AdmissionState
		wantPII   []string
	}{
		{
			name:      "hard block — metric has data but no series carries the tenant label",
			fake:      fakeProm{labelled: nil, all: []map[string]string{{"__name__": "m", "instance": "x"}}},
			wantState: AdmissionHardBlock,
		},
		{
			name:      "warn — no samples in the window",
			fake:      fakeProm{labelled: nil, all: nil},
			wantState: AdmissionWarn,
		},
		{
			name:      "pass — a tenant-labelled series exists",
			fake:      fakeProm{labelled: []map[string]string{{"__name__": "m", "tenant": "db-a", "instance": "x"}}},
			wantState: AdmissionPass,
		},
		{
			name: "pass — shared metric: tenant series exist, unlabelled platform series do NOT block",
			fake: fakeProm{
				labelled: []map[string]string{{"__name__": "m", "tenant": "db-a"}},
				all:      []map[string]string{{"__name__": "m"}, {"__name__": "m", "tenant": "db-a"}},
			},
			wantState: AdmissionPass,
		},
		{
			name:      "pass with PII advisory — a tenant series label name looks like PII",
			fake:      fakeProm{labelled: []map[string]string{{"__name__": "m", "tenant": "db-a", "customer_email": "a@b.c"}}},
			wantState: AdmissionPass,
			wantPII:   []string{"customer_email"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srv := tt.fake.server(t)
			v := NewAdmissionValidator(srv.URL)
			got, err := v.Check(context.Background(), "m")
			if err != nil {
				t.Fatalf("Check() error = %v", err)
			}
			if got.State != tt.wantState {
				t.Errorf("Check() state = %q, want %q (reason: %s)", got.State, tt.wantState, got.Reason)
			}
			if len(got.PIILabels) != len(tt.wantPII) {
				t.Fatalf("Check() PIILabels = %v, want %v", got.PIILabels, tt.wantPII)
			}
			for i := range tt.wantPII {
				if got.PIILabels[i] != tt.wantPII[i] {
					t.Errorf("PIILabels[%d] = %q, want %q", i, got.PIILabels[i], tt.wantPII[i])
				}
			}
		})
	}
}

func TestAdmissionCheck_BackendErrorPropagates(t *testing.T) {
	// A backend error must surface as an error, not be silently
	// swallowed — the caller maps an indeterminate result to the
	// soft-gate path, which it can only do if it sees the error.
	srv := (&fakeProm{status: http.StatusInternalServerError}).server(t)
	v := NewAdmissionValidator(srv.URL)
	if _, err := v.Check(context.Background(), "m"); err == nil {
		t.Error("Check() error = nil, want non-nil on backend HTTP 500")
	}
}

func TestScanPIILabels(t *testing.T) {
	tests := []struct {
		name   string
		labels map[string]string
		want   []string
	}{
		{
			name:   "benign labels — no hits",
			labels: map[string]string{"__name__": "m", "tenant": "db-a", "instance": "host:9090", "job": "node"},
			want:   nil,
		},
		{
			name:   "PII-looking label names flagged, sorted",
			labels: map[string]string{"__name__": "m", "user_ip": "1.2.3.4", "customer": "acme", "tenant": "db-a"},
			want:   []string{"customer", "user_ip"},
		},
		{
			name:   "metric name itself is never flagged",
			labels: map[string]string{"__name__": "customer_email_total", "tenant": "db-a"},
			want:   nil,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := scanPIILabels(tt.labels)
			if len(got) != len(tt.want) {
				t.Fatalf("scanPIILabels() = %v, want %v", got, tt.want)
			}
			for i := range tt.want {
				if got[i] != tt.want[i] {
					t.Errorf("scanPIILabels()[%d] = %q, want %q", i, got[i], tt.want[i])
				}
			}
		})
	}
}
