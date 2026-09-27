package gitops

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// --- #1721: bodies whose committed bytes carry more than the struct decode saw ---
//
// Write commits the body VERBATIM, so every gate has to be judged on what those
// bytes mean to the exporter, not on what one struct decode of the first
// document happened to read. Each body below keeps a well-formed own section
// (URL id db-a) and smuggles a db-shared section past that decode.
//
// want is a substring of the refusal, so each case proves the NEW gate refused
// it rather than some unrelated later check.
var yamlShapeBypasses = []struct{ name, body, want string }{
	{
		// The end-of-document marker closes the first document; the second one
		// is a parse error, which the old extra-document count read as EOF.
		"end marker then an unparseable document",
		ownOnly + "...\ntenants:\n  db-shared:\n    _silent_mode: \"critical\"\n",
		"invalid YAML in document 2",
	},
	{
		// The struct decode never walks zzz, so the bad tag in it goes unseen;
		// the generic decode fails, which silenced both the root-key check and
		// the extra-document count.
		"unknown root key hiding a bad tag",
		ownOnly + "zzz: !!int \"abc\"\n---\ntenants:\n  db-shared:\n    _silent_mode: \"critical\"\n",
		"invalid YAML in document 1",
	},
	{
		"root merge key before the own section",
		"<<: &x\n  tenants:\n    db-shared:\n      _silent_mode: \"critical\"\n" + ownOnly,
		"merge key (<<) at the root",
	},
	{
		"root merge key after the own section",
		ownOnly + "<<:\n  tenants:\n    db-shared:\n      _silent_mode: \"critical\"\n",
		"merge key (<<) at the root",
	},
	{
		"root merge key in sequence form",
		"<<: [&a {tenants: {db-shared: {_silent_mode: \"critical\"}}}]\n" + ownOnly,
		"merge key (<<) at the root",
	},
	// A root key that is not a string makes CheckTenantRootKeys' map[string]any
	// decode fail (it then reports nothing), while the generic decode falls back
	// to map[any]any and succeeds — so neither saw the extra root content.
	{
		"null root key (~)",
		ownOnly + "~: {tenants: {db-shared: {_silent_mode: critical}}, defaults: {cpu_critical: 1}}\n",
		"YAML root keys must be plain strings",
	},
	{
		"null root key (null)",
		ownOnly + "null: {tenants: {db-shared: {_silent_mode: critical}}}\n",
		"YAML root keys must be plain strings",
	},
	{
		"null root key (NULL)",
		ownOnly + "NULL: {tenants: {db-shared: {_silent_mode: critical}}}\n",
		"YAML root keys must be plain strings",
	},
	{
		"empty explicit root key",
		ownOnly + "? \n: {tenants: {db-shared: {_silent_mode: critical}}}\n",
		"YAML root keys must be plain strings",
	},
	{
		"null key in a flow root",
		"{tenants: {db-a: {_silent_mode: warning}}, ~: {tenants: {db-shared: {_silent_mode: critical}}}}\n",
		"YAML root keys must be plain strings",
	},
}

func gitHead(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").CombinedOutput()
	if err != nil {
		t.Fatalf("git rev-parse HEAD: %v\n%s", err, out)
	}
	return strings.TrimSpace(string(out))
}

// TestWriteRefusesYAMLShapeBypasses drives the real Write: the gate has to hold
// at the call site that reaches disk and git.
func TestWriteRefusesYAMLShapeBypasses(t *testing.T) {
	for _, tc := range yamlShapeBypasses {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			before := gitHead(t, dir)
			_, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", tc.body)
			if err == nil {
				raw, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
				t.Fatalf("Write committed a body carrying db-shared: %q", raw)
			}
			if !errors.Is(err, ErrValidation) {
				t.Errorf("want ErrValidation, got %v", err)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("refused by the wrong check: want %q in %v", tc.want, err)
			}
			if _, serr := os.Stat(filepath.Join(dir, "db-a.yaml")); !os.IsNotExist(serr) {
				raw, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
				t.Errorf("rejected write still reached disk: %q", raw)
			}
			if after := gitHead(t, dir); after != before {
				t.Errorf("rejected write still moved HEAD: %s -> %s", before, after)
			}
		})
	}
}

// The PR-mode dry-run must give the same verdict as the write it predicts.
func TestDryRunValidateBodyOnlyRefusesYAMLShapeBypasses(t *testing.T) {
	for _, tc := range yamlShapeBypasses {
		t.Run(tc.name, func(t *testing.T) {
			errs, err := DryRunValidateBodyOnly("db-a", tc.body)
			if err != nil {
				t.Fatalf("unexpected pre-validation error: %v", err)
			}
			if len(errs) == 0 {
				t.Fatal("dry-run answered valid for a body Write must refuse")
			}
			if len(errs) != 1 || !strings.Contains(errs[0], tc.want) {
				t.Errorf("refused by the wrong check: want one error containing %q, got %v", tc.want, errs)
			}
		})
	}
}

// TestWriteAcceptsLegalYAMLShapes pins the other side: the gate must not refuse
// bodies a YAML emitter or a human may legitimately produce. Merge keys and
// aliases INSIDE a tenant section are ordinary YAML and stay legal — only a
// merge key at the ROOT can splice in a section the struct decode never names.
func TestWriteAcceptsLegalYAMLShapes(t *testing.T) {
	const own = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	for _, tc := range []struct{ name, body string }{
		{"plain", own},
		{"CRLF line endings", strings.ReplaceAll(own, "\n", "\r\n")},
		{"UTF-8 BOM", "\ufeff" + own},
		{"leading document marker", "---\n" + own},
		{"trailing empty document", own + "---\n"},
		{"end-of-document marker alone", own + "...\n"},
		{"end-of-document marker then a comment", own + "...\n# trailing note\n"},
		{"trailing comment-only document", own + "---\n# nothing here\n"},
		{"trailing null document", own + "---\n~\n"},
		{"TAG directive", "%TAG !e! tag:example.com,2000:\n---\n" + own},
		{"YAML 1.1 directive", "%YAML 1.1\n---\n" + own},
		{"no trailing newline", strings.TrimSuffix(own, "\n")},
		{"trailing blank lines", own + "\n\n\n"},
		{"alias inside the tenant section",
			"tenants:\n  db-a:\n    _silent_mode: &m \"warning\"\n    _severity_dedup: *m\n"},
		{"merge key inside the tenant section",
			"tenants:\n  db-a:\n    <<: {_silent_mode: \"warning\"}\n"},
		{"explicitly tagged string root key", "!!str tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"},
		{"explicitly tagged root mapping", "--- !!map\n" + own},
		{"flow root", "{tenants: {db-a: {_silent_mode: \"warning\"}}}\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", tc.body); err != nil {
				t.Fatalf("legal body refused: %v", err)
			}
			raw, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
			if err != nil {
				t.Fatalf("accepted write did not reach disk: %v", err)
			}
			if string(raw) != tc.body {
				t.Errorf("committed bytes differ from the body: got %q want %q", raw, tc.body)
			}
		})
	}
}
