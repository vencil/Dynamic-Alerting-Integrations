package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// #2491: TA_WRITE_MODE and the RBAC path default to the MORE OPEN choice
// (direct write, open-read). Before the fix an empty value fell through
// envOrDefault to that default, indistinguishable from choosing it.

func TestResolveNonEmptyEnvContract(t *testing.T) {
	for _, tc := range []struct {
		name    string
		raw     string
		set     bool
		want    string
		wantErr bool
	}{
		{name: "unset keeps the default", set: false, want: "direct"},
		{name: "a value is kept as given", raw: "pr-github", set: true, want: "pr-github"},
		{name: "empty is refused, not read as unset", raw: "", set: true, wantErr: true},
		{name: "whitespace only is refused", raw: "  \t", set: true, wantErr: true},
		{name: "padding is left for the value's own parser", raw: " pr-github ", set: true, want: " pr-github "},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveNonEmptyEnv(tc.raw, tc.set, "direct")
			if tc.wantErr {
				if !errors.Is(err, errEnvSetButEmpty) {
					t.Fatalf("resolveNonEmptyEnv(%q, set) = %q, %v — want errEnvSetButEmpty", tc.raw, got, err)
				}
				if got == "direct" {
					t.Fatalf("rejected %q but still returned the default; that is the fallback #2491 removes", tc.raw)
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("resolveNonEmptyEnv(%q, %v) = %q, %v — want %q, nil", tc.raw, tc.set, got, err, tc.want)
			}
		})
	}
}

func TestCheckRBACFlag(t *testing.T) {
	for _, tc := range []struct {
		explicit bool
		v        string
		wantErr  bool
	}{
		{explicit: false, v: "", wantErr: false},    // no RBAC path anywhere: open-read, as documented
		{explicit: true, v: "", wantErr: true},      // --rbac=
		{explicit: true, v: "   ", wantErr: true},   // --rbac="   "
		{explicit: true, v: "/etc/rbac/_rbac.yaml"}, // the chart's form
		{explicit: false, v: "/conf.d/_rbac.yaml"},  // from TA_RBAC_PATH
	} {
		err := checkRBACFlag(tc.explicit, tc.v)
		if (err != nil) != tc.wantErr {
			t.Errorf("checkRBACFlag(%v, %q) = %v, wantErr %v", tc.explicit, tc.v, err, tc.wantErr)
		}
	}
}

func TestFlagExplicit(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want bool
	}{
		{args: nil, want: false},
		{args: []string{"--rbac="}, want: true},
		{args: []string{"--rbac", "/x"}, want: true},
		{args: []string{"--other=1"}, want: false},
	} {
		fs := flag.NewFlagSet("t", flag.ContinueOnError)
		fs.SetOutput(io.Discard)
		fs.String("rbac", "", "")
		fs.String("other", "", "")
		if err := fs.Parse(tc.args); err != nil {
			t.Fatalf("parse %v: %v", tc.args, err)
		}
		if got := flagExplicit(fs, "rbac"); got != tc.want {
			t.Errorf("flagExplicit after %v = %v, want %v", tc.args, got, tc.want)
		}
	}
}

// TestServerRefusesEmptyValuesAtStartup runs the real main() in a child
// process. Each case must exit non-zero BEFORE the server does anything, and
// say why. A unit test on the helpers alone would not prove main() calls them.
func TestServerRefusesEmptyValuesAtStartup(t *testing.T) {
	if os.Getenv("EMPTYENV_CRASHER") == "1" {
		os.Args = append([]string{"tenant-api"}, strings.Fields(os.Getenv("EMPTYENV_ARGS"))...)
		main()
		// Reaching here means main() returned; the parent sees exit 0.
		return
	}

	for _, tc := range []struct {
		name string
		env  []string
		args string
		want []string
	}{
		{name: "TA_WRITE_MODE empty", env: []string{"TA_WRITE_MODE="}, want: []string{"TA_WRITE_MODE", "#2491"}},
		{name: "TA_WRITE_MODE whitespace", env: []string{"TA_WRITE_MODE=  "}, want: []string{"TA_WRITE_MODE", "#2491"}},
		{name: "TA_RBAC_PATH empty", env: []string{"TA_RBAC_PATH="}, want: []string{"TA_RBAC_PATH", "#2491"}},
		{name: "--rbac= on the command line", args: "--rbac=", want: []string{"--rbac", "#2491"}},
		// #2903: no generator socket, no start.
		{name: "no generator socket", want: []string{"--generator-socket", "TA_GENERATOR_SOCKET", "#2903"}},
		{name: "TA_GENERATOR_SOCKET blank", env: []string{"TA_GENERATOR_SOCKET=  "}, want: []string{"--generator-socket", "#2903"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Bounded: if the check is gone, main() goes on to serve and
			// would never exit on its own.
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestServerRefusesEmptyValuesAtStartup$")
			env := []string{"EMPTYENV_CRASHER=1", "EMPTYENV_ARGS=" + tc.args}
			for _, kv := range os.Environ() {
				// The child must see only the variable under test.
				if !strings.HasPrefix(kv, "TA_") {
					env = append(env, kv)
				}
			}
			cmd.Env = append(env, tc.env...)
			out, err := cmd.CombinedOutput()
			if ctx.Err() != nil {
				t.Fatalf("main() with %v %q was still running after 30s: it started instead of refusing. Output:\n%s", tc.env, tc.args, out)
			}
			if err == nil {
				t.Fatalf("main() with %v %q exited 0; an empty value must refuse to start. Output:\n%s", tc.env, tc.args, out)
			}
			for _, w := range tc.want {
				if !strings.Contains(string(out), w) {
					t.Errorf("startup failure does not mention %q. Got:\n%s", w, out)
				}
			}
			// Must die before startup proper, not later for an unrelated reason.
			if strings.Contains(string(out), "tenant-api starting") {
				t.Errorf("main() got as far as starting up before failing. Output:\n%s", out)
			}
		})
	}
}
