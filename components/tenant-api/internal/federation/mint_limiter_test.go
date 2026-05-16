package federation

import (
	"testing"
	"time"
)

func TestMintLimiter_AllowsUpToWindowCap(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	now := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		if !l.allow("tenant", now) {
			t.Fatalf("mint %d within the window cap was denied", i)
		}
	}
	if l.allow("tenant", now) {
		t.Error("mint past the window cap was allowed")
	}
}

func TestMintLimiter_WindowSlidesForward(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	t0 := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		l.allow("tenant", t0)
	}
	// Still inside the window: the cap still holds.
	if l.allow("tenant", t0.Add(mintWindow-time.Second)) {
		t.Error("mint still inside the window was allowed past the cap")
	}
	// Once the window has fully elapsed, the old hits age out.
	if !l.allow("tenant", t0.Add(mintWindow+time.Second)) {
		t.Error("mint after the window elapsed was denied — old hits were not evicted")
	}
}

func TestMintLimiter_DeniedAttemptNotRecorded(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	t0 := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		l.allow("tenant", t0)
	}
	// Hammer the limiter while it is at the cap. A denied attempt must
	// not be recorded — otherwise a client spamming the endpoint would
	// keep pushing its own window forward and never recover.
	for i := 0; i < 10; i++ {
		if l.allow("tenant", t0.Add(mintWindow-time.Second)) {
			t.Fatal("expected denial while at the cap")
		}
	}
	if !l.allow("tenant", t0.Add(mintWindow+time.Second)) {
		t.Error("denied attempts pushed the window forward — they must not be recorded")
	}
}

func TestMintLimiter_PerTenantIsolation(t *testing.T) {
	t.Parallel()
	l := newMintLimiter()
	now := time.Now()
	for i := 0; i < maxMintsPerWindow; i++ {
		l.allow("tenant-busy", now)
	}
	if l.allow("tenant-busy", now) {
		t.Error("tenant-busy past its cap was allowed")
	}
	if !l.allow("tenant-idle", now) {
		t.Error("tenant-idle should have an independent window")
	}
}
