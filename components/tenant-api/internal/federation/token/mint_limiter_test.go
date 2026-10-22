package token

import (
	"testing"
	"time"
)

func TestMintLimiter_AllowsUpToWindowCap(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	now := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		if ok, _ := l.admit("tenant", now); !ok {
			t.Fatalf("mint %d within the window cap was denied", i)
		}
	}
	if ok, _ := l.admit("tenant", now); ok {
		t.Error("mint past the window cap was allowed")
	}
}

func TestMintLimiter_WindowSlidesForward(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	t0 := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		l.admit("tenant", t0)
	}
	// Still inside the window: the cap still holds.
	if ok, _ := l.admit("tenant", t0.Add(mintWindow-time.Second)); ok {
		t.Error("mint still inside the window was allowed past the cap")
	}
	// Once the window has fully elapsed, the old hits age out.
	if ok, _ := l.admit("tenant", t0.Add(mintWindow+time.Second)); !ok {
		t.Error("mint after the window elapsed was denied — old hits were not evicted")
	}
}

func TestMintLimiter_DeniedAttemptNotRecorded(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	t0 := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		l.admit("tenant", t0)
	}
	// Hammer the limiter while it is at the cap. A denied attempt must
	// not be recorded — otherwise a client spamming the endpoint would
	// keep pushing its own window forward and never recover.
	for i := 0; i < 10; i++ {
		if ok, _ := l.admit("tenant", t0.Add(mintWindow-time.Second)); ok {
			t.Fatal("expected denial while at the cap")
		}
	}
	if ok, _ := l.admit("tenant", t0.Add(mintWindow+time.Second)); !ok {
		t.Error("denied attempts pushed the window forward — they must not be recorded")
	}
}

func TestMintLimiter_EvictsIdleTenants(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	t0 := time.Now()
	l.admit("tenant-gone", t0)
	if len(l.hits) != 1 {
		t.Fatalf("expected 1 tracked tenant after a mint, got %d", len(l.hits))
	}
	// A later mint by a different tenant, past tenant-gone's window,
	// must evict tenant-gone's now-stale key — otherwise the map leaks
	// one key per tenant that ever minted and never returned.
	l.admit("tenant-other", t0.Add(mintWindow+time.Second))
	if _, ok := l.hits["tenant-gone"]; ok {
		t.Error("idle tenant key was not evicted — mintLimiter map leaks")
	}
	if _, ok := l.hits["tenant-other"]; !ok {
		t.Error("the active tenant's key should still be tracked")
	}
}

func TestMintLimiter_PerTenantIsolation(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	now := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		l.admit("tenant-busy", now)
	}
	if ok, _ := l.admit("tenant-busy", now); ok {
		t.Error("tenant-busy past its cap was allowed")
	}
	if ok, _ := l.admit("tenant-idle", now); !ok {
		t.Error("tenant-idle should have an independent window")
	}
}

// A denied attempt says how long until the oldest mint in the window ages
// out — the Retry-After the handler sends (#3226).
func TestMintLimiter_DeniedSaysWhenTheNextMintIsAdmitted(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	t0 := time.Now()
	l.admit("tenant", t0)
	for i := 1; i < maxMintsPerWindow; i++ {
		l.admit("tenant", t0.Add(10*time.Second))
	}
	at := t0.Add(20 * time.Second)
	ok, wait := l.admit("tenant", at)
	if ok {
		t.Fatal("mint past the window cap was allowed")
	}
	if want := mintWindow - 20*time.Second; wait != want {
		t.Errorf("wait = %v, want %v (the oldest mint ages out at t0+window)", wait, want)
	}
	if ok, _ := l.admit("tenant", at.Add(wait+time.Nanosecond)); !ok {
		t.Error("mint once wait has passed was denied")
	}
}
