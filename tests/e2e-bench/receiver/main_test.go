package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRingBuffer_PreservesChronologicalOrder(t *testing.T) {
	rb := newRingBuffer(5)
	for i := int64(1); i <= 3; i++ {
		rb.add(Post{ReceivedUnixNs: i, Status: "firing", TenantID: "x"})
	}
	snap := rb.snapshot()
	if len(snap) != 3 {
		t.Fatalf("expected 3 posts, got %d", len(snap))
	}
	for i, p := range snap {
		if p.ReceivedUnixNs != int64(i+1) {
			t.Errorf("position %d: expected ts=%d, got %d", i, i+1, p.ReceivedUnixNs)
		}
	}
}

func TestRingBuffer_WrapsAroundDroppingOldest(t *testing.T) {
	rb := newRingBuffer(3)
	for i := int64(1); i <= 5; i++ {
		rb.add(Post{ReceivedUnixNs: i, Status: "firing", TenantID: "x"})
	}
	snap := rb.snapshot()
	if len(snap) != 3 {
		t.Fatalf("expected 3 posts after wrap, got %d", len(snap))
	}
	// After wrap, ts 1 and 2 are dropped; remaining is 3, 4, 5 in order.
	expected := []int64{3, 4, 5}
	for i, p := range snap {
		if p.ReceivedUnixNs != expected[i] {
			t.Errorf("position %d: expected ts=%d, got %d", i, expected[i], p.ReceivedUnixNs)
		}
	}
}

func TestQuery_FiltersBySinceTenantStatus(t *testing.T) {
	rb := newRingBuffer(10)
	// Pre-populate: 4 posts with mixed tenants and statuses.
	rb.add(Post{ReceivedUnixNs: 100, Status: "firing", TenantID: "a"})
	rb.add(Post{ReceivedUnixNs: 200, Status: "firing", TenantID: "b"})
	rb.add(Post{ReceivedUnixNs: 300, Status: "resolved", TenantID: "a"})
	rb.add(Post{ReceivedUnixNs: 400, Status: "firing", TenantID: "a"})

	cases := []struct {
		name   string
		filter queryFilter
		wantTs []int64
	}{
		{"no filter", queryFilter{}, []int64{100, 200, 300, 400}},
		{"since 200", queryFilter{since: 200}, []int64{200, 300, 400}},
		{"tenant a", queryFilter{tenantID: "a"}, []int64{100, 300, 400}},
		{"tenant a + firing", queryFilter{tenantID: "a", status: "firing"}, []int64{100, 400}},
		{"since 250 + tenant a", queryFilter{since: 250, tenantID: "a"}, []int64{300, 400}},
		{"no match (tenant z)", queryFilter{tenantID: "z"}, []int64{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := rb.query(tc.filter)
			if len(got) != len(tc.wantTs) {
				t.Fatalf("expected %d posts, got %d", len(tc.wantTs), len(got))
			}
			for i, p := range got {
				if p.ReceivedUnixNs != tc.wantTs[i] {
					t.Errorf("idx %d: expected ts=%d, got %d", i, tc.wantTs[i], p.ReceivedUnixNs)
				}
			}
		})
	}
}

func TestExtractTenantID_PrefersTenantOverTenantId(t *testing.T) {
	if got := extractTenantID(map[string]string{"tenant": "a", "tenant_id": "b"}); got != "a" {
		t.Errorf("expected 'tenant' to win; got %q", got)
	}
	if got := extractTenantID(map[string]string{"tenant_id": "b"}); got != "b" {
		t.Errorf("expected 'tenant_id' fallback; got %q", got)
	}
	if got := extractTenantID(map[string]string{}); got != "" {
		t.Errorf("expected empty for no labels; got %q", got)
	}
}

func TestHookEndpoint_FlattensAlertsArray(t *testing.T) {
	rb := newRingBuffer(10)
	srv := httptest.NewServer(newServer(rb))
	defer srv.Close()

	payload := `{
		"status":"firing",
		"alerts":[
			{"status":"firing","labels":{"alertname":"X","tenant":"bench-run-1"}},
			{"status":"firing","labels":{"alertname":"X","tenant":"bench-run-2"}}
		]
	}`
	resp, err := http.Post(srv.URL+"/hook", "application/json", strings.NewReader(payload))
	if err != nil {
		t.Fatalf("POST /hook: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	snap := rb.snapshot()
	if len(snap) != 2 {
		t.Fatalf("expected 2 posts (one per alert), got %d", len(snap))
	}
	if snap[0].TenantID != "bench-run-1" || snap[1].TenantID != "bench-run-2" {
		t.Errorf("expected tenant_ids [bench-run-1, bench-run-2], got [%s, %s]", snap[0].TenantID, snap[1].TenantID)
	}
}

func TestHookEndpoint_TolerantOfNonAlertmanagerJson(t *testing.T) {
	rb := newRingBuffer(10)
	srv := httptest.NewServer(newServer(rb))
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/hook", "application/json", bytes.NewReader([]byte(`{"ping":true}`)))
	if err != nil {
		t.Fatalf("POST /hook: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 even for non-AM payload, got %d", resp.StatusCode)
	}
	if len(rb.snapshot()) != 1 {
		t.Errorf("expected 1 stored post (with empty fields), got %d", len(rb.snapshot()))
	}
}

func TestPostsEndpoint_ReturnsJSONArray(t *testing.T) {
	rb := newRingBuffer(10)
	rb.add(Post{ReceivedUnixNs: 100, Status: "firing", TenantID: "a", AlertName: "X"})

	srv := httptest.NewServer(newServer(rb))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/posts")
	if err != nil {
		t.Fatalf("GET /posts: %v", err)
	}
	defer resp.Body.Close()

	var posts []Post
	if err := json.NewDecoder(resp.Body).Decode(&posts); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(posts) != 1 || posts[0].TenantID != "a" {
		t.Errorf("unexpected posts: %+v", posts)
	}
}

func TestHealthz_ReturnsOK(t *testing.T) {
	rb := newRingBuffer(10)
	srv := httptest.NewServer(newServer(rb))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("expected 200, got %d", resp.StatusCode)
	}
}
