package handler

// The shared-config parsers must refuse a file whose SHAPE they do not
// recognise, not just one that fails to lex as YAML.
//
// parseGroupsFile / parseViewsFile rebuild the entire shared file from what
// they read under the writer lock, so "what I read" being wrong is a whole-file
// wipe rather than a bad single entry. yaml.v3 is not strict: a document with a
// typo'd top-level key, or someone else's document entirely, decodes into a
// zero config without error — indistinguishable from a genuinely empty file to
// everything downstream. The docstring on parseGroupsFile has always promised
// this can't happen; these tests are what makes the promise true.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

func TestParseSharedConfig_RejectsUnrecognisedShape(t *testing.T) {
	t.Parallel()

	// Valid YAML, wrong shape — every one of these used to parse as "zero
	// entries" and become the base of the next write.
	badShapes := map[string]string{
		"typo'd top-level key": "grops:\n  g-a:\n    label: a\n",
		"someone else's doc":   "data:\n  foo: bar\n",
		"ConfigMap wrapper":    "apiVersion: v1\nkind: ConfigMap\ndata:\n  x: |\n    groups: {}\n",
		"document is a list":   "- g-a\n- g-b\n",
		"whitespace only":      "\n",
	}
	for name, in := range badShapes {
		t.Run("groups/"+name, func(t *testing.T) {
			t.Parallel()
			if _, err := parseGroupsFile([]byte(in)); err == nil {
				t.Fatalf("parseGroupsFile accepted an unrecognised shape as an empty base:\n%s", in)
			}
		})
		t.Run("views/"+name, func(t *testing.T) {
			t.Parallel()
			if _, err := parseViewsFile([]byte(in)); err == nil {
				t.Fatalf("parseViewsFile accepted an unrecognised shape as an empty base:\n%s", in)
			}
		})
	}
}

// The controls. Without these, the test above would also pass if the parsers
// simply rejected everything — including the shapes the writer itself emits.
func TestParseSharedConfig_AcceptsLegitimateShapes(t *testing.T) {
	t.Parallel()

	// What MarshalConfig emits for an empty config, which is what lands on
	// disk after the last group is deleted.
	emptyGroups, err := groups.MarshalConfig(&groups.GroupsConfig{Groups: map[string]groups.Group{}})
	if err != nil {
		t.Fatal(err)
	}
	for name, in := range map[string]string{
		"round-trip of an empty config": string(emptyGroups),
		"explicit empty mapping":        "groups: {}\n",
		"key present, null value":       "groups:\n",
		"populated":                     "groups:\n  g-a:\n    label: a\n    members: [t-a]\n",
	} {
		t.Run("groups/"+name, func(t *testing.T) {
			t.Parallel()
			if _, err := parseGroupsFile([]byte(in)); err != nil {
				t.Fatalf("parseGroupsFile rejected a legitimate file: %v\n%s", err, in)
			}
		})
	}

	// A missing file stays the first-write empty start — that path is what
	// makes a fresh deployment work at all.
	cfg, err := parseGroupsFile(nil)
	if err != nil {
		t.Fatalf("missing file must remain an empty start, got: %v", err)
	}
	if len(cfg.Groups) != 0 {
		t.Errorf("empty start should hold no groups, got %d", len(cfg.Groups))
	}
}

// The damage path end to end: a hand-edited _groups.yaml whose top-level key is
// misspelled must not be silently rebuilt into a file containing only the entry
// being written. The pre-existing groups are not recoverable from the write, so
// refusing is the only correct answer.
func TestPutGroup_UnrecognisedFileShapeIsRefusedNotRebuilt(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	path := filepath.Join(configDir, "_groups.yaml")

	// Twenty groups' worth of content, reachable only through the real key.
	corrupted := "grops:\n  g-a:\n    label: a\n  g-b:\n    label: b\n"
	if err := os.WriteFile(path, []byte(corrupted), 0o644); err != nil {
		t.Fatal(err)
	}
	runGit(t, configDir, "add", "_groups.yaml")
	runGit(t, configDir, "commit", "-m", "hand-edited with a typo")

	rbacMgr := newRBACManager(t, partialWriterRBAC)
	d := &Deps{
		Writer: gitops.NewWriter(configDir, configDir),
		Groups: groups.NewManager(configDir),
		RBAC:   rbacMgr,
	}
	h := wrapWithRBACMiddleware(PutGroup(d), rbacMgr, rbac.PermRead, nil)

	body, _ := json.Marshal(PutGroupRequest{Label: "gx", Members: []string{"t-owned"}})
	req := newRequestWithChiParam("PUT", "/api/v1/groups/g-x", "id", "g-x", bytes.NewBuffer(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "readers,owners")
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code == http.StatusOK {
		t.Fatalf("write succeeded against a file whose shape was not recognised; body: %s", w.Body.String())
	}

	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(after) != corrupted {
		t.Errorf("the refused write rewrote the file anyway:\nbefore:\n%s\nafter:\n%s", corrupted, after)
	}
	if strings.Contains(string(after), "g-x") {
		t.Error("the new entry landed on disk despite the refusal")
	}
}
