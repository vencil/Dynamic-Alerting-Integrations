package routingpolicy

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"
)

// TestDomainPoliciesShapeError (#2659): the shape check da-guard reports as
// domain_policy_unusable, on a node parse only. Where it is an error,
// ParseDomainPolicies refuses the document with the same error — one
// predicate (domainPoliciesNode), not two copies.
func TestDomainPoliciesShapeError(t *testing.T) {
	t.Parallel()
	for _, src := range []string{
		"domain_policies: null\n", "domain_policies: ~\n", "domain_policies:\n", "domain_policies: []\n",
		"domain_policies: [a]\n", "domain_policies: x\n", "domain_policies: 1\n",
		// merge-supplied, found as lookup finds it
		"base: &b\n  domain_policies: null\n<<: *b\n",
		"base: &b\n  domain_policies: {fin: {}}\n<<: *b\ndomain_policies: ~\n", // own key wins
		// #2677: one merge sequence: its first source wins
		"<<: [{domain_policies: null}, {domain_policies: {fin: {}}}]\n",
	} {
		err := DomainPoliciesShapeError([]byte(src))
		if err == nil {
			t.Errorf("%q: nil, want the shape error", src)
			continue
		}
		if _, _, perr := ParseDomainPolicies([]byte(src)); perr == nil || perr.Error() != err.Error() {
			t.Errorf("%q: ParseDomainPolicies err = %v, want the same %v", src, perr, err)
		}
	}
	for _, src := range []string{
		"domain_policies: {}\n", "domain_policies: {fin: {tenants: [t1]}}\n", "other: 1\n", "", "# only a comment\n",
		"base: &b\n  domain_policies: {fin: {}}\n<<: *b\n",
		"base: &b\n  domain_policies: null\n<<: *b\ndomain_policies: {}\n", // own key wins
		"<<: [{domain_policies: {fin: {}}}, {domain_policies: null}]\n",
		"x: &b {a: 1}\ndomain_policies: {fin: {<<: *b, constraints: {<<: {forbidden_receiver_types: [slack]}}}}\n",
		// not this predicate's: the caller's own decode judges them
		"[a, b]\n", "x\n", "domain_policies: [unclosed\n",
	} {
		if err := DomainPoliciesShapeError([]byte(src)); err != nil {
			t.Errorf("%q: %v, want nil", src, err)
		}
	}
}

// TestDomainPoliciesShapeError_TaggedMergeKey (#2677): a merge key spelled
// other than `<<` anywhere in the file is one yaml.v3 reads as a plain key,
// so tenant-api (which decodes the file with yaml.v3) cannot read the file as
// the generator does: DomainPoliciesShapeError refuses it. ParseDomainPolicies
// (da-guard) reads it as PyYAML does — the later of two merge keys wins, a
// nested source flattened first.
func TestDomainPoliciesShapeError_TaggedMergeKey(t *testing.T) {
	t.Parallel()
	const fin = "{domain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}}"
	for src, daGuardUnusable := range map[string]bool{
		"!!merge q: {domain_policies: null}\n<<: " + fin + "\n":                   false,
		"<<: {domain_policies: null}\n!!merge q: " + fin + "\n":                   false,
		"<<: " + fin + "\n!!merge q: {domain_policies: null}\n":                   true,
		"<<: [{!!merge a: {domain_policies: null}, <<: {domain_policies: {}}}]\n": false,
		"<<: {!!merge a: {domain_policies: null}}\n":                              true,
		"x: {!!merge a: {domain_policies: null}}\n":                               false,
		// under domain_policies, in a domain, in its constraints, an alias value
		"domain_policies:\n  !!merge q: {fin: {tenants: [t1]}}\n":                                   false,
		"domain_policies: {fin: {!!merge q: {tenants: [t1]}}}\n":                                    false,
		"domain_policies: {fin: {constraints: {!!merge q: {forbidden_receiver_types: [slack]}}}}\n": false,
		"x: &b {tenants: [t1]}\ndomain_policies: {fin: {!!merge q: *b}}\n":                          false,
	} {
		if err := DomainPoliciesShapeError([]byte(src)); err == nil || !strings.Contains(err.Error(), "tenant-api") {
			t.Errorf("%q: DomainPoliciesShapeError = %v, want the tagged merge key refused", src, err)
		}
		if _, _, err := ParseDomainPolicies([]byte(src)); (err != nil) != daGuardUnusable {
			t.Errorf("%q: ParseDomainPolicies err = %v, want unusable %v", src, err, daGuardUnusable)
		}
	}
}

// TestTenantsItemsAreSourceText (#2677 B1d): the generator reads a list under
// a `tenants` key with its scalar items as their source text
// (_lib_yaml_keys.ExporterKeyLoader, raw_text_sequences), never constructing
// them — so a merge-tagged item (`<<`, `!!merge x`, an alias naming an
// anchored `<<`) is a tenant id there, wherever the list is (block style,
// merged in, under a key no policy reader reads). The same list also built
// as a value elsewhere (written as one, or aliased from one) PyYAML cannot
// construct: the generator drops the file. Measured with _parse_config_files.
func TestTenantsItemsAreSourceText(t *testing.T) {
	t.Parallel()
	const c = "constraints: {forbidden_receiver_types: [slack]}"
	for name, tc := range map[string]struct {
		src     string
		tenants []string // nil: the file is refused
	}{
		"plain":     {"domain_policies: {fin: {tenants: [t1, <<], " + c + "}}\n", []string{"t1", "<<"}},
		"tagged":    {"domain_policies: {fin: {tenants: [t1, !!merge x], " + c + "}}\n", []string{"t1", "x"}},
		"alias":     {"x: {&v <<: {}}\ndomain_policies: {fin: {tenants: [t1, *v], " + c + "}}\n", []string{"t1", "<<"}},
		"block":     {"domain_policies:\n  fin:\n    tenants:\n    - t1\n    - <<\n    " + c + "\n", []string{"t1", "<<"}},
		"merged":    {"domain_policies: {fin: {<<: {tenants: [t1, <<]}, " + c + "}}\n", []string{"t1", "<<"}},
		"unread":    {"x: {tenants: [<<]}\ndomain_policies: {fin: {tenants: [t1], " + c + "}}\n", []string{"t1"}},
		"reused":    {"t: &t [t1, <<]\ndomain_policies: {fin: {tenants: *t, " + c + "}}\n", nil},
		"aliasedTo": {"domain_policies: {fin: {tenants: &t [t1, <<], " + c + "}}\nx: *t\n", nil},
	} {
		pols, _, err := ParseDomainPolicies([]byte(tc.src))
		if tc.tenants == nil {
			if err == nil {
				t.Errorf("%s: ParseDomainPolicies = %+v, want the file refused", name, pols)
			}
			continue
		}
		if err != nil || len(pols) != 1 || !reflect.DeepEqual(pols[0].Tenants, tc.tenants) {
			t.Errorf("%s: ParseDomainPolicies = %+v, %v; want fin for %v", name, pols, err, tc.tenants)
		}
	}
}

// TestAliasMergeKey (#2677 B1c): an alias key naming an anchored `<<` is a
// merge to PyYAML (its composer hands back the anchored node) and a plain
// key "<<" to yaml.v3's decode. da-guard reads it as the generator does
// (measured: fin forbids slack for t1 in each); tenant-api refuses the file.
// One case per place a policy is read: a domain, the top, constraints,
// domain_policies (anchored under `!!merge <<`).
func TestAliasMergeKey(t *testing.T) {
	t.Parallel()
	for name, src := range map[string]string{
		"p1": "x: {&m <<: {}}\ndomain_policies:\n  fin:\n    tenants: [t1]\n    *m : {constraints: {forbidden_receiver_types: [slack]}}\n",
		"p8": "x: {&m <<: {}}\n*m : {domain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}}\n",
		"p9": "x: {&m <<: {}}\ndomain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n" +
			"      *m : {forbidden_receiver_types: [slack]}\n",
		"p10": "x: {&m !!merge <<: {}}\ndomain_policies:\n  *m : {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
	} {
		pols, _, err := ParseDomainPolicies([]byte(src))
		if err != nil || len(pols) != 1 || pols[0].Domain != "fin" ||
			!reflect.DeepEqual(pols[0].Tenants, []string{"t1"}) ||
			!reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
			t.Errorf("%s: ParseDomainPolicies = %+v, %v; want fin forbidding slack for t1", name, pols, err)
		}
		if err := DomainPoliciesShapeError([]byte(src)); err == nil || !strings.Contains(err.Error(), "tenant-api") {
			t.Errorf("%s: DomainPoliciesShapeError = %v, want the alias merge key refused", name, err)
		}
	}
}

// Nothing is decoded, so lookup's merge expansion is what could loop or
// multiply: a top mapping merged into itself ends, and a merge fan-out is
// listed once per source.
func TestDomainPoliciesShapeError_MergeCycleAndFanOutAreBounded(t *testing.T) {
	t.Parallel()
	fan := "m0: &m0 {a: 1}\n"
	for i := 1; i <= 9; i++ {
		fan += fmt.Sprintf("m%d: &m%d {<<: [%s]}\n", i, i,
			strings.TrimSuffix(strings.Repeat(fmt.Sprintf("*m%d,", i-1), 10), ","))
	}
	for src, wantErr := range map[string]bool{
		"&t {<<: *t, domain_policies: ~}\n":         true,
		"&t {<<: *t, domain_policies: {fin: {}}}\n": false,
		fan + "<<: *m9\ndomain_policies: ~\n":       true,
		fan + "<<: *m9\n":                           false,
	} {
		start := time.Now()
		err := DomainPoliciesShapeError([]byte(src))
		if (err != nil) != wantErr {
			t.Errorf("%.40q: err = %v, want error %v", src, err, wantErr)
		}
		if d := time.Since(start); d > 2*time.Second {
			t.Errorf("%.40q: took %v", src, d)
		}
	}
}
