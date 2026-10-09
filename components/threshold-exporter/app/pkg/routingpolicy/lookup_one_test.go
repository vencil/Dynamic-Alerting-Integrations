package routingpolicy

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"gopkg.in/yaml.v3"
)

// lookupEquivalent asserts lookupOne == lookup for every mapping node of
// doc (an alias is not entered: its target is checked where it is written)
// and every key the document writes, plus two it may not.
func lookupEquivalent(t *testing.T, label, doc string) int {
	t.Helper()
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(doc), &root); err != nil || len(root.Content) == 0 {
		return 0
	}
	keys := map[string]bool{"domain_policies": true, "zz-absent": true}
	var maps []*yaml.Node
	stack := []*yaml.Node{&root}
	for len(stack) > 0 {
		n := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if n.Kind == yaml.MappingNode {
			maps = append(maps, n)
			for i := 0; i+1 < len(n.Content); i += 2 {
				keys[deref(n.Content[i]).Value] = true
			}
		}
		if n.Kind != yaml.AliasNode {
			stack = append(stack, n.Content...)
		}
	}
	checked := 0
	for _, m := range maps {
		for k := range keys {
			if want, got := lookup(m, k), lookupOne(m, k); want != got {
				t.Errorf("%s: key %q at line %d: lookup = %v, lookupOne = %v", label, k, m.Line, nodeAt(want), nodeAt(got))
			}
			checked++
		}
	}
	return checked
}

func nodeAt(n *yaml.Node) string {
	if n == nil {
		return "nil"
	}
	return fmt.Sprintf("%s@%d:%d", kindName(n), n.Line, n.Column)
}

// TestLookupOne_EquivalentToLookup (#2659 round 4): the bounded single-key
// lookup gives lookup's answer — corpus: every document of the shared
// matrices that writes a merge key, hand-written precedence and cycle
// cases, and random merge / alias graphs.
func TestLookupOne_EquivalentToLookup(t *testing.T) {
	t.Parallel()
	_, thisFile, _, _ := runtime.Caller(0)
	shared := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..", "tests", "shared")
	var corpus []string
	var parity struct {
		Trees []struct {
			Files map[string]string `json:"files"`
		} `json:"trees"`
	}
	raw, err := os.ReadFile(filepath.Join(shared, "routing_policy_parity_matrix.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &parity); err != nil {
		t.Fatal(err)
	}
	for _, tree := range parity.Trees {
		for _, src := range tree.Files {
			if strings.Contains(src, "<<") {
				corpus = append(corpus, src)
			}
		}
	}
	fromMatrix := len(corpus)
	if fromMatrix == 0 {
		t.Fatal("no merge document in the parity matrix — the corpus is vacuous")
	}
	corpus = append(corpus,
		"a: &a {x: 1, domain_policies: {f: {}}}\nb: &b {x: 2, domain_policies: null}\n<<: [*a, *b]\n",
		"a: &a {x: 1}\nb: &b {x: 2}\n<<: [*b, *a]\nx: 3\n",
		"a: &a {x: 1}\n<<: *a\n<<: {x: 2, y: 1}\n",
		"a: &a {<<: {x: 1}, y: 2}\nb: {<<: *a, x: 3}\nc: {<<: [{y: 9}, *a]}\n",
		"k: &k domain_policies\nm: {*k : null, <<: {domain_policies: {}}}\n",
		"x: &x {<<: *x, a: 1}\n",
		"&t {<<: *t, domain_policies: ~}\n",
		"a: &a {b: &b {<<: *a, k: 1}, j: 2}\nc: {<<: [*b, *a]}\n",
		"a: &a {!!merge x: {k: 1}, k: 2}\nb: {<<: *a}\n",
		"a: {x: 1, x: 2, <<: {x: 3}}\n",
		// #2677: several merge keys in one mapping, the later one winning
		"!!merge q: {domain_policies: null}\n<<: {domain_policies: {fin: {}}}\n",
		"a: &a {x: 1}\nb: {!!merge p: *a, <<: [{x: 2}, {x: 3}], !!merge r: {y: 1}}\n",
		"a: &a {x: 1}\nb: {<<: [{!!merge p: {x: 2}, <<: *a}, {x: 4}], !!merge r: {y: 1}}\n",
	)
	checked := 0
	for i, doc := range corpus {
		c := lookupEquivalent(t, fmt.Sprintf("corpus[%d]", i), doc)
		if c == 0 {
			t.Errorf("corpus[%d] does not parse — it compares nothing:\n%s", i, doc)
		}
		checked += c
	}
	for seed := int64(1); seed <= 400; seed++ {
		doc := randomMergeDoc(rand.New(rand.NewSource(seed)))
		c := lookupEquivalent(t, fmt.Sprintf("seed %d", seed), doc)
		if c == 0 {
			t.Errorf("seed %d does not parse — it compares nothing:\n%s", seed, doc)
		}
		checked += c
	}
	if checked < 10000 {
		t.Errorf("only %d (mapping, key) pairs compared", checked)
	}
	t.Logf("%d matrix documents, %d (mapping, key) pairs compared", fromMatrix, checked)
}

// TestLookup_MergePrecedenceIsPyYAMLs (#2677): which merge source supplies a
// key, judged against pyyamlcompat.Decode — a separate port that builds
// SafeConstructor.flatten_mapping's pair list literally, so it is not this
// package's first-hit search restated. Corpus: the hand-written #2677 shapes
// and the random merge graphs (several `<<` per mapping, sequences, nested
// sources). A document whose own keys repeat is skipped: there PyYAML keeps
// the last pair and lookup the first, and every reader refuses the file.
func TestLookup_MergePrecedenceIsPyYAMLs(t *testing.T) {
	t.Parallel()
	corpus := []string{
		"!!merge q: {domain_policies: null}\n<<: {domain_policies: {fin: {}}}\n",
		"<<: {domain_policies: {fin: {}}}\n!!merge q: {domain_policies: null}\n",
		"!!merge p: {x: 1}\n<<: [{x: 2}, {x: 3}]\n",
		"<<: [{x: 2}, {x: 3}]\n!!merge p: {x: 1}\n",
		"<<: [{!!merge p: {x: 1}, <<: {x: 2}}, {x: 3}]\n",
		"!!merge p: {x: 1}\n<<: {x: 2}\nx: 3\n",
	}
	for seed := int64(1); seed <= 2000; seed++ { // most repeat an own key: skipped below
		corpus = append(corpus, randomMergeDoc(rand.New(rand.NewSource(seed))))
	}
	compared, multi := 0, 0
	for i, doc := range corpus {
		var root yaml.Node
		if err := yaml.Unmarshal([]byte(doc), &root); err != nil || len(root.Content) == 0 {
			t.Fatalf("corpus[%d] does not parse: %v\n%s", i, err, doc)
		}
		maps, ownDup := mappingsOf(&root)
		if ownDup {
			continue
		}
		for _, m := range maps {
			py, ok := pyyamlcompat.Decode(m).(map[string]any)
			if !ok {
				continue // a key PyYAML does not build as a string (none here)
			}
			merges := 0
			for j := 0; j+1 < len(m.Content); j += 2 {
				if isMergeKey(m.Content[j]) {
					merges++
				}
			}
			if merges > 1 {
				multi++
			}
			for _, k := range []string{"a", "b", "c", "x", "y", "domain_policies"} {
				want, wantOK := py[k]
				got := lookup(m, k)
				if !wantOK {
					if got != nil {
						t.Errorf("corpus[%d] line %d key %q: PyYAML has no such key, lookup = %v", i, m.Line, k, nodeAt(got))
					}
					continue
				}
				if got == nil || !reflect.DeepEqual(pyyamlcompat.Decode(got), want) {
					t.Errorf("corpus[%d] line %d key %q: lookup = %v, PyYAML = %#v\n%s", i, m.Line, k, nodeAt(got), want, doc)
				}
				compared++
			}
		}
	}
	if compared < 1000 || multi < 100 {
		t.Errorf("only %d keys compared, %d mappings with several merge keys", compared, multi)
	}
	t.Logf("%d keys compared, %d mappings with several merge keys", compared, multi)
}

// hasUnsupported reports whether v holds a value pyyamlcompat marks as one
// PyYAML does not build (there: the whole safe_load fails).
func hasUnsupported(v any) bool {
	switch v := v.(type) {
	case pyyamlcompat.Unsupported:
		return true
	case map[string]any:
		for _, c := range v {
			if hasUnsupported(c) {
				return true
			}
		}
	case []any:
		for _, c := range v {
			if hasUnsupported(c) {
				return true
			}
		}
	}
	return false
}

// TestMergeKeyShape (#2677 R1): a merge value PyYAML's flatten_mapping
// refuses (not a mapping, nor a sequence of mappings) makes PyYAML refuse the
// whole document — the generator drops the file. parseDoc (da-guard)
// refuses it, `<<` and the tagged spelling alike; pyyamlcompat.Decode marks
// the same mapping Unsupported. What PyYAML
// merges (an empty sequence, an alias to a mapping) passes.
func TestMergeKeyShape(t *testing.T) {
	t.Parallel()
	const pol = "domain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n"
	for src, refused := range map[string]bool{
		"!!merge q: 5\n" + pol:           true,
		"!!merge q: ~\n" + pol:           true,
		"!!merge q: [1]\n" + pol:         true,
		"<<: 5\n" + pol:                  true,
		"<<: [{x: 1}, [2]]\n" + pol:      true,
		"x: {<<: null}\n" + pol:          true,
		"<<: []\n" + pol:                 false,
		"a: &a {x: 1}\n<<: [*a]\n" + pol: false,
		"!!merge q: {x: 1}\n" + pol:      false,
	} {
		var root yaml.Node
		if err := yaml.Unmarshal([]byte(src), &root); err != nil {
			t.Fatalf("%q: %v", src, err)
		}
		if py := hasUnsupported(pyyamlcompat.Decode(&root)); py != refused {
			t.Errorf("%q: pyyamlcompat refuses = %v, table says %v", src, py, refused)
		}
		if _, perr := parseDoc([]byte(src), true); (perr != nil) != refused {
			t.Errorf("%q: parseDoc err = %v, want refused %v", src, perr, refused)
		}
	}
}

// TestMergeKeyShape_AliasedSequenceIsLinear (#2677): one sequence of n
// mappings aliased as the merge value of n mappings is checked once, not
// once per merge key — mergeKeyShape runs on every LoadRoot (tenant-api:
// every request). Judged by the nodes looked at against the input's size,
// not a clock: a check per merge key looks at n*(n+1).
func TestMergeKeyShape_AliasedSequenceIsLinear(t *testing.T) {
	t.Parallel()
	const n = 2000
	var b strings.Builder
	b.WriteString("s: &S [")
	for i := 0; i < n; i++ {
		b.WriteString("{}, ")
	}
	b.WriteString("{}]\nm:\n")
	for i := 0; i < n; i++ {
		b.WriteString("  - {<<: *S}\n")
	}
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(b.String()), &doc); err != nil {
		t.Fatal(err)
	}
	work, err := mergeKeyShapeWork(&doc)
	if err != nil {
		t.Fatal(err)
	}
	if work > 2*(n+1) {
		t.Errorf("%d merge-value nodes looked at for a document of %d mappings: the aliased sequence is rescanned", work, 2*n+2)
	}
}

// mappingsOf lists doc's mapping nodes (aliases not entered) and whether any
// of them writes one key twice (by source text, an alias key by its anchor's).
func mappingsOf(doc *yaml.Node) (maps []*yaml.Node, ownDup bool) {
	stack := []*yaml.Node{doc}
	for len(stack) > 0 {
		n := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if n.Kind == yaml.MappingNode {
			maps = append(maps, n)
			seen := map[string]bool{}
			for i := 0; i+1 < len(n.Content); i += 2 {
				if k := n.Content[i]; !isMergeKey(k) {
					ownDup = ownDup || seen[deref(k).Value]
					seen[deref(k).Value] = true
				}
			}
		}
		if n.Kind != yaml.AliasNode {
			stack = append(stack, n.Content...)
		}
	}
	return maps, ownDup
}

// randomMergeDoc writes a document of anchored mappings that merge earlier
// ones (an alias, a sequence of aliases, or an inline mapping), with own
// keys that repeat merged ones, null and mapping values, and alias keys.
func randomMergeDoc(r *rand.Rand) string {
	keys := []string{"a", "b", "c", "domain_policies"}
	// inline: anchors written on an inline merge value (`<<: &i3 {...}`);
	// usable: how many of them a body may alias — those of earlier bodies
	// only, so every alias follows its anchor (#2677 R1: an anchor inside a
	// merge value that a mapping without merge keys aliases).
	inline, usable := 0, 0
	val := func() string {
		if usable > 0 && r.Intn(5) == 0 {
			return fmt.Sprintf("*i%d", r.Intn(usable))
		}
		switch r.Intn(4) {
		case 0:
			return "null"
		case 1:
			return "{}"
		case 2:
			return "[1]"
		}
		return fmt.Sprint(r.Intn(100))
	}
	var b strings.Builder
	b.WriteString("s0: &s0 a\ns1: &s1 domain_policies\n")
	n := 3 + r.Intn(12)
	body := func(i int) string {
		usable = inline
		var parts []string
		for j := r.Intn(4); j > 0; j-- {
			if i > 0 && r.Intn(3) == 0 {
				parts = append(parts, fmt.Sprintf("*s%d : %s", r.Intn(2), val()))
				continue
			}
			parts = append(parts, keys[r.Intn(len(keys))]+": "+val())
		}
		for j := r.Intn(3); j > 0 && i > 0; j-- {
			switch r.Intn(3) {
			case 0:
				parts = append(parts, fmt.Sprintf("<<: *m%d", r.Intn(i)))
			case 1:
				var srcs []string
				for k := 1 + r.Intn(3); k > 0; k-- {
					if usable > 0 && r.Intn(4) == 0 {
						srcs = append(srcs, fmt.Sprintf("*i%d", r.Intn(usable)))
						continue
					}
					srcs = append(srcs, fmt.Sprintf("*m%d", r.Intn(i)))
				}
				parts = append(parts, "<<: ["+strings.Join(srcs, ", ")+"]")
			default:
				parts = append(parts, fmt.Sprintf("<<: &i%d {%s: %s, <<: *m%d}", inline, keys[r.Intn(len(keys))], val(), r.Intn(i)))
				inline++
			}
		}
		r.Shuffle(len(parts), func(x, y int) { parts[x], parts[y] = parts[y], parts[x] })
		return "{" + strings.Join(parts, ", ") + "}"
	}
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, "m%d: &m%d %s\n", i, i, body(i))
	}
	fmt.Fprintf(&b, "top: %s\n", body(n))
	return b.String()
}

// mergeChain is n mappings, each merging the previous one, merged at the top
// beside an empty domain_policies (R3-F1): lookup flattens it in O(n²).
func mergeChain(n int) string {
	var b strings.Builder
	b.WriteString("m0: &m0 {k0: 1}\n")
	for i := 1; i < n; i++ {
		fmt.Fprintf(&b, "m%d: &m%d {<<: *m%d, k%d: 1}\n", i, i, i-1, i)
	}
	fmt.Fprintf(&b, "<<: *m%d\ndomain_policies: {}\n", n-1)
	return b.String()
}

func shapeErrorCost(t *testing.T, data []byte) (time.Duration, uint64) {
	t.Helper()
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	start := time.Now()
	if err := DomainPoliciesShapeError(data); err != nil {
		t.Fatalf("DomainPoliciesShapeError: %v", err)
	}
	d := time.Since(start)
	runtime.ReadMemStats(&after)
	return d, after.TotalAlloc - before.TotalAlloc
}

// TestDomainPoliciesShapeError_MergeChainIsLinear (R3-F1): the predicate
// tenant-api runs before any decode costs O(nodes) on a merge chain.
// Not parallel: it measures allocation.
func TestDomainPoliciesShapeError_MergeChainIsLinear(t *testing.T) {
	small, large := []byte(mergeChain(16000)), []byte(mergeChain(32000))
	dSmall, bSmall := shapeErrorCost(t, small)
	dLarge, bLarge := shapeErrorCost(t, large)
	t.Logf("n=16000 (%d KB): %v, %d MB; n=32000: %v, %d MB", len(small)>>10, dSmall, bSmall>>20, dLarge, bLarge>>20)
	// Wall clock is only a coarse ceiling (measured ~55ms at n=16000 alone,
	// ~1s under -race beside the whole module's tests locally, 5.07s on a
	// slow CI runner under -race and coverage, #2763; the quadratic lookup
	// took ~29s and 5.4GB at n=16000). Allocation is the signal.
	if dSmall > 15*time.Second || dLarge > 30*time.Second {
		t.Errorf("took %v (n=16000) and %v (n=32000)", dSmall, dLarge)
	}
	if bSmall > 256<<20 {
		t.Errorf("allocated %d MB at n=16000", bSmall>>20)
	}
	if ratio := float64(bLarge) / float64(bSmall); ratio > 3 {
		t.Errorf("allocation grew %.1fx from n to 2n (%d → %d bytes): not linear", ratio, bSmall, bLarge)
	}
}
