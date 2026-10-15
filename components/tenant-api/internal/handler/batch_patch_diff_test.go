package handler

// #2903 (ADR-036 §4, "批次 patch 的合併"): a batch patch is still merged by
// Go — mergePatchYAML decodes the tenant file into yaml.v3 nodes, sets /
// removes keys under tenants.<id> and re-encodes it — and only the merged
// file is shown to the generator. The generator's verdict on the merged file
// cannot see a merge that changed the file on its own:
//
//   - a base the generator cannot read, made readable by the re-encode (the
//     documents after the first `---` dropped);
//   - a base the generator reads, whose meaning the edit changed without any
//     finding (a key removed while a `<<` merge key still supplies it).
//
// tenant-api's rule is an invariant on the merge's result
// (verifyPartialWrite): decoded by yaml.v3, the merged file must be the base
// with exactly the edit applied, and define the same anchors; otherwise the
// write is refused (ErrMergeBaseNotLoadable, config_error
// not_round_trippable). yaml.v3's reading is not the generator's, so this
// test holds the invariant to the generator, in both directions. For every
// case, one side is Go's merge read back by the generator; the other is the
// generator's own read of the base with the same patch laid over
// tenants.<id> (testdata/batch_patch_diff.py):
//
//   - Go too loose (it merges where the round trip changed something): the
//     two sides differ over the WHOLE file — every other tenant and key
//     included — or a base the generator cannot read comes out readable.
//     Both are red.
//   - Go stricter than needed (it refuses a file it could rewrite): only
//     more refusals; the cases pin each one (want "base_refused").
//
// A shape the generator cannot read that the round trip keeps as it is
// (want "unreadable_kept") leaves the merged file just as unreadable, so the
// generator refuses the write.
//
// Needs python3 with PyYAML (as generator_real_test.go): skips locally
// without it, fails under CI.

import (
	"bytes"
	"encoding/json"
	"errors"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
)

type patchDiffCase struct {
	name   string
	base   string
	tenant string
	patch  map[string]string
	unset  []string
	// want: what mergePatchYAML does — "merged", "noop", "base_refused"
	// (ErrMergeBaseNotLoadable, before the merge), "error" (any other
	// refusal, e.g. a scalar over a structured key), or "unreadable_kept"
	// (merged, over a base the generator cannot read, into a file it
	// cannot read either).
	want string
}

const twoTenants = "tenants:\n  t1:\n    mysql_connections: \"80\"\n  t2:\n    mysql_connections: \"70\"\n"

var patchDiffCases = []patchDiffCase{
	// --- the base reads the same either way --------------------------------
	{name: "plain", base: twoTenants, tenant: "t1", patch: map[string]string{"mysql_connections": "90"}, want: "merged"},
	{name: "add-key", base: twoTenants, tenant: "t1", patch: map[string]string{"redis_memory": "50"}, want: "merged"},
	{name: "new-tenant-in-file", base: twoTenants, tenant: "t3", patch: map[string]string{"redis_memory": "50"}, want: "merged"},
	{name: "new-file", base: "", tenant: "t1", patch: map[string]string{"redis_memory": "50"}, want: "merged"},
	{name: "unset-routing", tenant: "t1", unset: []string{"_routing"}, want: "merged",
		base: "tenants:\n  t1:\n    redis_memory: \"1\"\n    _routing:\n      receiver: {type: webhook, url: \"https://a.example/x\"}\n  t2:\n    _routing: disable\n"},
	{name: "unset-absent-noop", base: twoTenants, tenant: "t1", unset: []string{"_routing"}, want: "noop"},
	// Patch values the generator would type if they were written plain:
	// each must stay the string the patch carries.
	{name: "values-needing-quotes", base: twoTenants, tenant: "t1", want: "merged", patch: map[string]string{
		"a_yes": "yes", "a_on": "on", "a_null": "null", "a_tilde": "~", "a_empty": "", "a_int": "010",
		"a_float": "1e3", "a_octal": "0o17", "a_under": "1_000", "a_date": "2001-12-14", "a_inf": ".inf",
		"a_colon": "a: b", "a_hash": "x #y", "a_lead": " lead", "a_nl": "a\nb", "a_uni": "é😀", "a_bang": "!x",
		"a_star": "*x", "a_amp": "&x", "a_dash": "- x", "a_quote": "'\"",
	}},
	// Siblings written in shapes PyYAML and yaml.v3 type differently: Go's
	// re-encode must keep the generator's reading of each.
	{name: "sibling-scalar-shapes", tenant: "t1", patch: map[string]string{"redis_memory": "1"}, want: "merged",
		base: "tenants:\n  t1: {}\n  t2:\n    b_yes: yes\n    b_on: On\n    b_octal: 0o17\n    b_old_octal: 017\n" +
			"    b_under: 1_000\n    b_date: 2001-12-14\n    b_ts: 2001-12-14t21:59:43.10-05:00\n    b_inf: .inf\n" +
			"    b_sexa: 1:20\n    b_str: !!str 12\n    b_bin: !!binary aGk=\n    b_float: 1e3\n    b_null: ~\n" +
			"    b_empty:\n    b_hex: 0x1F\n    b_folded: >\n      a\n      b\n    b_literal: |\n      a\n      b\n"},
	// Anchors and a `<<` in another tenant's block: not where the edit looks.
	{name: "anchors-and-merge-keys", tenant: "t1", patch: map[string]string{"redis_memory": "1"}, want: "merged",
		base: "x_shared: &s\n  redis_memory: \"5\"\ntenants:\n  t1:\n    mysql_mem: \"2\"\n  t2:\n    <<: *s\n    redis_memory: \"9\"\n"},
	{name: "flow-and-comments", tenant: "t2", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "# head\ntenants: # t\n  t1: {redis_memory: \"1\", _routing: {receiver: {type: slack, api_url: \"https://h.example/x\"}}} # c\n  t2:\n    redis_memory: \"2\" # c2\n"},
	{name: "quoted-and-numeric-ids", tenant: "010", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "tenants:\n  \"t1\":\n    redis_memory: \"1\"\n  010:\n    redis_memory: \"2\"\n  '011': {}\n"},
	{name: "indent-4-crlf", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "tenants:\r\n    t1:\r\n        redis_memory: \"1\"\r\n    t2:\r\n        redis_memory: \"2\"\r\n"},
	{name: "document-markers", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "---\ntenants:\n  t1:\n    redis_memory: \"1\"\n...\n"},
	{name: "long-string", tenant: "t1", want: "merged",
		patch: map[string]string{"_metadata_note": strings.Repeat("word ", 60) + "end"},
		base:  "tenants:\n  t1:\n    redis_memory: \"1\"\n  t2:\n    note: " + strings.Repeat("long ", 60) + "end\n"},
	{name: "routes-match-null-key", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "tenants:\n  t1: {}\n  t2:\n    _routing:\n      receiver: {type: webhook, url: \"https://a.example/x\"}\n" +
			"      routes:\n        - match: {~: x}\n          receiver: {type: webhook, url: \"https://b.example/x\"}\n"},
	{name: "other-top-level-keys", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "_routing_profile_note: keep\ntenants:\n  t1: {}\nextra:\n  - 1\n  - yes\n"},
	{name: "scalar-over-structured", base: "tenants:\n  t1:\n    _metadata: {owner: a}\n", tenant: "t1",
		patch: map[string]string{"_metadata": "x"}, want: "error"},

	// --- refused: the merge would change what the edit does not ----------
	// The documents after the first `---` would be dropped by the re-encode.
	{name: "multi-document", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1:\n    redis_memory: \"1\"\n---\ntenants:\n  t2:\n    redis_memory: \"2\"\n"},
	{name: "multi-document-empty-second", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1:\n    redis_memory: \"1\"\n---\n"},
	// The edit finds keys by their text. Removing `_routing` leaves the one a
	// `<<` supplies: the generator would read a disabled tenant (Go loose,
	// were it merged).
	{name: "unset-over-merge-key", tenant: "t1", unset: []string{"_routing"}, want: "base_refused",
		base: "x: &s\n  _routing: disable\ntenants:\n  t1:\n    <<: *s\n    _routing:\n      receiver: {type: webhook, url: \"https://a.example/x\"}\n"},
	// Setting a key a `<<` also supplies: the explicit key wins, as before.
	{name: "patch-beside-merge-key", tenant: "t1", patch: map[string]string{"_silent_mode": "warning"}, want: "merged",
		base: "x: &s\n  _silent_mode: critical\ntenants:\n  t1:\n    <<: *s\n"},
	// An alias key `*k` naming `_routing`: the edit removes `_routing` and the
	// alias key's value survives (Go loose, were it merged).
	{name: "alias-key-in-block", tenant: "t1", unset: []string{"_routing"}, want: "base_refused",
		base: "k: &k _routing\ntenants:\n  t1:\n    _routing: disable\n    *k : {receiver: {type: webhook, url: \"https://a.example/x\"}}\n"},
	// The tenant id written as an alias: the edit would add a second `t1`.
	{name: "alias-tenant-id", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "k: &k t1\ntenants:\n  *k : {redis_memory: \"1\"}\n"},
	// Already refused by the typed decode (tenantConfigError): a repeated key
	// in a tenant's block, a repeated tenant id, not YAML.
	{name: "duplicate-patched-key", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1:\n    redis_memory: \"1\"\n    redis_memory: \"2\"\n"},
	{name: "duplicate-unset-key", tenant: "t1", unset: []string{"_routing"}, want: "base_refused",
		base: "tenants:\n  t1:\n    _routing: disable\n    _routing: {receiver: {type: slack, api_url: \"https://h.example/x\"}}\n"},
	{name: "duplicate-in-sibling-routing", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\n  t2:\n    _routing:\n      receiver: {type: webhook, url: \"https://a.example/x\", url: \"https://b.example/x\"}\n"},
	{name: "duplicate-tenant-id", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1:\n    redis_memory: \"1\"\n  t1:\n    redis_memory: \"2\"\n"},
	{name: "not-yaml", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: [\n"},

	// Blind review #2903: a block scalar the re-encode rewrites (a leading
	// blank line, a more-indented folded line, kept or stripped trailing
	// blanks) — in the edited tenant or another one.
	{name: "block-literal-strip-leading-blank", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: |-\n\n      a\n"},
	{name: "block-literal-leading-blank", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: |\n\n      a\n"},
	{name: "block-literal-leading-space-line", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: |\n      \n      a\n"},
	{name: "block-folded-more-indented", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: >\n      a\n       b\n      c\n"},
	{name: "block-folded-keep-trailing", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: >+\n      a\n\n"},
	{name: "block-folded-leading-blanks", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: >\n\n\n      a\n"},
	{name: "block-in-other-tenant", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1: {}\n  t2:\n    d: |-\n\n      a\n"},
	// A TAB inside a block scalar: yaml.v3 refuses the file (malformed_yaml),
	// the generator reads it (Go stricter).
	{name: "block-literal-tab", tenant: "t1", patch: map[string]string{"z": "1"}, want: "base_refused",
		base: "tenants:\n  t1:\n    d: |\n      \tTAB\n"},
	// An anchor defined twice: the edit removes one definition and an alias
	// would name the other.
	{name: "anchor-redefined-value", tenant: "t1", patch: map[string]string{"x": "5"}, want: "base_refused",
		base: "tenants:\n  t0:\n    x: &v \"9\"\n  t1:\n    x: &v \"1\"\n    z: *v\n"},
	{name: "anchor-redefined-key-unset", tenant: "t1", unset: []string{"_routing"}, want: "base_refused",
		base: "tenants:\n  t0:\n    &k _routing: disable\n  t1:\n    &k _routing: disable\n    z: *k\n"},
	// Removing the anchored value leaves its alias undefined.
	{name: "anchor-undefined-after-unset", tenant: "t1", unset: []string{"_routing"}, want: "base_refused",
		base: "tenants:\n  t1:\n    _routing: &v disable\n    z: *v\n"},
	// One block shared through an alias: patching t1 would change t2 too.
	{name: "shared-block-patch-owner", tenant: "t1", patch: map[string]string{"z": "5"}, want: "base_refused",
		base: "tenants:\n  t1: &b\n    a: \"1\"\n  t2: *b\n"},
	{name: "shared-block-patch-alias", tenant: "t2", patch: map[string]string{"z": "5"}, want: "error",
		base: "tenants:\n  t1: &b\n    a: \"1\"\n  t2: *b\n"},

	// --- read by the generator, merged unchanged ------------------------
	// An alias cycle: yaml.v3 cannot decode it as values (Go stricter).
	{name: "alias-cycle", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra: &a\n  b: *a\n"},
	{name: "merge-key-then-explicit", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "tenants:\n  t1: {}\nextra:\n  <<: {a: 1}\n  a: 2\n"},
	{name: "same-number-other-text", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "merged",
		base: "tenants:\n  t1: {}\nextra:\n  1: a\n  01: b\n"},

	// --- unreadable, and the re-encode keeps it so ----------------------
	// yaml.v3 cannot decode these as values either: refused before any
	// write (the generator could not read them anyway).
	{name: "duplicate-in-unread-key", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1:\n    redis_memory: \"1\"\nextra:\n  a: 1\n  a: 2\n"},
	{name: "duplicate-via-alias-key", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "unreadable_kept",
		base: "k: &k a\ntenants:\n  t1: {}\nextra:\n  a: 1\n  *k : 2\n"},
	{name: "duplicate-quoted-and-plain", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra:\n  a: 1\n  'a': 2\n"},
	{name: "mapping-key", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra:\n  ? {a: 1}\n  : x\n"},
	{name: "sequence-key", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra:\n  ? [a]\n  : x\n"},
	{name: "unknown-tag", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "unreadable_kept",
		base: "tenants:\n  t1: {}\nextra: !foo bar\n"},
	{name: "int-tag-not-int", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra: !!int abc\n"},
	{name: "bool-tag-not-bool", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra: !!bool maybe\n"},
	{name: "map-tag-on-sequence", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "unreadable_kept",
		base: "tenants:\n  t1: {}\nextra: !!map [1]\n"},
	{name: "date-out-of-range", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "unreadable_kept",
		base: "tenants:\n  t1: {}\nextra: 2001-13-45\n"},
	{name: "merge-key-scalar", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "base_refused",
		base: "tenants:\n  t1: {}\nextra:\n  <<: 1\n"},
	{name: "python-tag", tenant: "t1", patch: map[string]string{"redis_memory": "3"}, want: "unreadable_kept",
		base: "tenants:\n  t1: {}\nextra: !!python/name:os.system ''\n"},
}

// generatorDiff runs testdata/batch_patch_diff.py over the cases.
func generatorDiff(t *testing.T, in []map[string]any) []struct {
	BaseReadable   bool   `json:"base_readable"`
	BaseError      string `json:"base_error"`
	MergedReadable bool   `json:"merged_readable"`
	MergedError    string `json:"merged_error"`
	Equal          bool   `json:"equal"`
	Want           string `json:"want"`
	Got            string `json:"got"`
} {
	t.Helper()
	if out, err := exec.Command("python3", "-c", "import yaml").CombinedOutput(); err != nil {
		requireOrSkip(t, "python3 with PyYAML is not available: "+strings.TrimSpace(string(out)))
	}
	// <root>/scripts/tools/ops/generator_service.py → <root>
	root := filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(realGeneratorScript(t)))))
	body, err := json.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("python3", filepath.Join("testdata", "batch_patch_diff.py"), root)
	cmd.Stdin = bytes.NewReader(body)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("batch_patch_diff.py: %v\n%s", err, stderr.String())
	}
	var res []struct {
		BaseReadable   bool   `json:"base_readable"`
		BaseError      string `json:"base_error"`
		MergedReadable bool   `json:"merged_readable"`
		MergedError    string `json:"merged_error"`
		Equal          bool   `json:"equal"`
		Want           string `json:"want"`
		Got            string `json:"got"`
	}
	if err := json.Unmarshal(out, &res); err != nil || len(res) != len(in) {
		t.Fatalf("batch_patch_diff.py answered %d results for %d cases (%v): %s", len(res), len(in), err, out)
	}
	return res
}

func TestBatchPatchMerge_OfflineDiffAgainstGenerator(t *testing.T) {
	in := make([]map[string]any, len(patchDiffCases))
	outcome := make([]string, len(patchDiffCases))
	for i, c := range patchDiffCases {
		patch := c.patch
		if patch == nil {
			patch = map[string]string{}
		}
		unset := c.unset
		if unset == nil {
			unset = []string{}
		}
		merged, err := mergePatchYAML([]byte(c.base), c.tenant, patch, unset)
		var mergedArg any
		switch {
		case err == nil:
			outcome[i], mergedArg = "merged", merged
		case errors.Is(err, gitops.ErrMergeNoOp):
			outcome[i] = "noop"
		case errors.Is(err, gitops.ErrMergeBaseNotLoadable):
			outcome[i] = "base_refused"
		default:
			outcome[i] = "error"
		}
		in[i] = map[string]any{"base": c.base, "merged": mergedArg, "tenant": c.tenant, "patch": patch, "unset": unset}
	}
	res := generatorDiff(t, in)
	refusedUnreadable := 0
	for i, c := range patchDiffCases {
		r := res[i]
		wantOutcome := c.want
		if wantOutcome == "unreadable_kept" {
			wantOutcome = "merged"
			if r.BaseReadable || r.MergedReadable {
				t.Errorf("%s: unreadable_kept needs a base and a merged file the generator cannot read "+
					"(base readable %v, merged readable %v)", c.name, r.BaseReadable, r.MergedReadable)
			}
		}
		if outcome[i] != wantOutcome {
			t.Errorf("%s: mergePatchYAML %s, the case says %s", c.name, outcome[i], c.want)
		}
		// The rule: a base the generator cannot read is refused before the
		// merge — or merged into a file it cannot read either (the gate
		// then refuses it). Never into one it can read.
		if !r.BaseReadable {
			switch {
			case outcome[i] == "base_refused":
				refusedUnreadable++
			case outcome[i] == "merged" && !r.MergedReadable:
			default:
				t.Errorf("%s: the generator cannot read the base (%s), but mergePatchYAML %s "+
					"(merged readable: %v) — it must refuse before merging", c.name, r.BaseError, outcome[i],
					r.MergedReadable)
			}
		}
		if outcome[i] != "merged" || !r.BaseReadable {
			continue
		}
		// The diff: Go's merge as the generator reads it == the generator's
		// read of the base with the same patch, over the whole file.
		if !r.MergedReadable {
			t.Errorf("%s: the generator cannot read Go's merged file: %s", c.name, r.MergedError)
			continue
		}
		if !r.Equal {
			t.Errorf("%s: Go's merge changes what the generator reads\n want (base + patch): %s\n got (Go's merge):    %s",
				c.name, r.Want, r.Got)
		}
	}
	// Not vacuous: the corpus does hold bases the generator cannot read.
	if refusedUnreadable < 5 {
		t.Errorf("only %d cases have a base the generator cannot read; the refusal rule is barely exercised",
			refusedUnreadable)
	}
}

// verifyPartialWrite names the first difference only inside the written
// tenant's own block; anywhere else — another tenant's block may be one the
// caller cannot read — it says "another place in the file".
func TestVerifyPartialWrite_NamesOnlyTheOwnBlock(t *testing.T) {
	for _, c := range []struct {
		name, base, want, never string
		unset                   []string
		patch                   map[string]string
	}{
		{name: "own block", unset: []string{"_routing"},
			base: "x: &s\n  _routing: disable\ntenants:\n  t1:\n    <<: *s\n    _routing:\n      receiver: {type: webhook, url: \"https://a.example/x\"}\n",
			want: "first difference: tenants.t1._routing"},
		{name: "other tenant", patch: map[string]string{"z": "1"},
			base: "tenants:\n  t1: {}\n  t2:\n    d: |-\n\n      a\n",
			want: "first difference: another place in the file", never: "t2"},
	} {
		t.Run(c.name, func(t *testing.T) {
			if c.patch == nil {
				c.patch = map[string]string{}
			}
			_, err := mergePatchYAML([]byte(c.base), "t1", c.patch, c.unset)
			if err == nil || !strings.Contains(err.Error(), c.want) || (c.never != "" && strings.Contains(err.Error(), c.never)) {
				t.Errorf("err = %v; want it to contain %q (and not %q)", err, c.want, c.never)
			}
		})
	}
}

// The keys an edit sets must hold exactly the new value in the merged file
// (here a plain `true`, which decodes as a boolean, not the string "true"),
// and the keys it removes must be gone.
func TestVerifyPartialWrite_ChecksTheEditedValues(t *testing.T) {
	const base = "tenants:\n  t1:\n    a: \"1\"\n    b: \"2\"\n"
	for _, c := range []struct {
		name, merged string
		edits        []partialEdit
		ok           bool
	}{
		{"set as a string", "tenants:\n  t1:\n    a: \"true\"\n    b: \"2\"\n", []partialEdit{{key: "a", set: true, checkValue: true, value: "true"}}, true},
		{"set as another type", "tenants:\n  t1:\n    a: true\n    b: \"2\"\n", []partialEdit{{key: "a", set: true, checkValue: true, value: "true"}}, false},
		{"removed", "tenants:\n  t1:\n    b: \"2\"\n", []partialEdit{{key: "a"}}, true},
		{"not removed", base, []partialEdit{{key: "a"}}, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			err := verifyPartialWrite([]byte(base), []byte(c.merged), "t1", c.edits)
			if (err == nil) != c.ok {
				t.Errorf("err = %v, want ok=%v", err, c.ok)
			}
		})
	}
}
