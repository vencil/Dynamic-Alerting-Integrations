package gitops

import (
	"context"
	"errors"
	"testing"
)

// TestIsGitLockContention pins the narrow string match for the "another process
// holds the index lock" failure. Only the index.lock-already-exists shape maps
// to a retryable overload; unrelated git failures must NOT (they are genuine
// 500-class faults, not "retry later").
func TestIsGitLockContention(t *testing.T) {
	cases := []struct {
		name string
		out  string
		want bool
	}{
		{
			name: "classic Unable to create / File exists",
			out:  "fatal: Unable to create '/conf.d/.git/index.lock': File exists.\n\nAnother git process seems to be running in this repository...",
			want: true,
		},
		{
			name: "lowercase variant",
			out:  "fatal: unable to create 'index.lock': file exists",
			want: true,
		},
		{
			name: "index.lock + File exists without the create phrasing",
			out:  "error: could not lock config file index.lock: File exists",
			want: true,
		},
		{
			name: "unrelated merge conflict is NOT contention",
			out:  "CONFLICT (content): Merge conflict in tenants/db-a.yaml",
			want: false,
		},
		{
			name: "generic fatal is NOT contention",
			out:  "fatal: not a git repository",
			want: false,
		},
		{
			name: "ref.lock (no index.lock substring) is NOT matched by this narrow check",
			out:  "error: cannot lock ref 'refs/heads/main': Unable to create '.git/refs/heads/main.lock': File exists",
			want: false, // intentionally narrow: only index.lock contention → overload
		},
		{
			name: "empty output",
			out:  "",
			want: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isGitLockContention([]byte(tc.out)); got != tc.want {
				t.Errorf("isGitLockContention(%q) = %v, want %v", tc.out, got, tc.want)
			}
		})
	}
}

// TestGitErr_LockContentionMapsToOverloaded: a non-deadline git failure whose
// output is the index.lock-contention shape is wrapped as ErrWriteOverloaded so
// the handler returns a retryable 503 (not a 500). A non-deadline, non-lock
// failure stays a plain error (→ 500). The deadline path is covered separately
// (TestGitExecTimeout*).
func TestGitErr_LockContentionMapsToOverloaded(t *testing.T) {
	w := NewWriter(t.TempDir(), "")
	// A fresh (non-deadline) context — gitErr must take the non-timeout branch.
	ctx := context.Background()
	sentinel := errors.New("exit status 128")

	lockOut := []byte("fatal: Unable to create '/conf.d/.git/index.lock': File exists.")
	if err := w.gitErr(ctx, "commit", sentinel, lockOut); !errors.Is(err, ErrWriteOverloaded) {
		t.Errorf("lock-contention git error = %v, want wrapped ErrWriteOverloaded", err)
	}

	// A non-lock failure on the same (non-deadline) path must NOT be remapped —
	// it stays the underlying error so the handler returns a 500.
	otherOut := []byte("fatal: not a git repository")
	err := w.gitErr(ctx, "commit", sentinel, otherOut)
	if errors.Is(err, ErrWriteOverloaded) {
		t.Errorf("non-lock git error wrongly mapped to ErrWriteOverloaded: %v", err)
	}
	if !errors.Is(err, sentinel) {
		t.Errorf("non-lock git error should wrap the underlying error, got %v", err)
	}
}
