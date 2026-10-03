package routingpolicy

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

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

// randomMergeDoc writes a document of anchored mappings that merge earlier
// ones (an alias, a sequence of aliases, or an inline mapping), with own
// keys that repeat merged ones, null and mapping values, and alias keys.
func randomMergeDoc(r *rand.Rand) string {
	keys := []string{"a", "b", "c", "domain_policies"}
	val := func() string {
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
					srcs = append(srcs, fmt.Sprintf("*m%d", r.Intn(i)))
				}
				parts = append(parts, "<<: ["+strings.Join(srcs, ", ")+"]")
			default:
				parts = append(parts, fmt.Sprintf("<<: {%s: %s, <<: *m%d}", keys[r.Intn(len(keys))], val(), r.Intn(i)))
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
	// ~1.2s under -race beside the whole module's tests; the quadratic
	// lookup took ~29s and 5.4GB at n=16000). Allocation is the signal.
	if dSmall > 5*time.Second || dLarge > 10*time.Second {
		t.Errorf("took %v (n=16000) and %v (n=32000)", dSmall, dLarge)
	}
	if bSmall > 256<<20 {
		t.Errorf("allocated %d MB at n=16000", bSmall>>20)
	}
	if ratio := float64(bLarge) / float64(bSmall); ratio > 3 {
		t.Errorf("allocation grew %.1fx from n to 2n (%d → %d bytes): not linear", ratio, bSmall, bLarge)
	}
}
