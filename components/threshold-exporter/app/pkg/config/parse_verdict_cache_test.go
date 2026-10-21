package config

// judgeConfFile's content cache (parse_verdict.go, #3206): a verdict taken
// from it is the verdict judged now, also after the bounded set is dropped
// and when many goroutines judge at once (run under -race), and a refusal's
// message is cut by one rule on both paths.

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

var verdictCacheDocs = []string{
	"tenants:\n  t1:\n    x: \"80\"\n",
	"tenants:\n  t1: {}\n---\n",
	"x: !!binary aGk=\n",
	"~: 1\n",
	"x: 1\nx: 2\n",
	"",
}

// fresh is the verdict judged with nothing cached.
func fresh(data []byte) error { return newVerdictCache(1<<10, nil).judge(data) }

func sameVerdict(a, b error) bool {
	ka, oka := UnreadableKindOf(a)
	kb, okb := UnreadableKindOf(b)
	if (a == nil) != (b == nil) || oka != okb || ka != kb {
		return false
	}
	return a == nil || a.Error() == b.Error()
}

func TestVerdictCache_HitIsTheVerdictJudgedNow(t *testing.T) {
	t.Parallel()
	c := newVerdictCache(1<<10, nil)
	for _, d := range verdictCacheDocs {
		miss, hit, want := c.judge([]byte(d)), c.judge([]byte(d)), fresh([]byte(d))
		if !sameVerdict(miss, want) || !sameVerdict(hit, want) {
			t.Errorf("%q: miss %v, hit %v, judged now %v", d, miss, hit, want)
		}
		if _, bad := parsetree.Parse([]byte(d)); (bad == nil) != (hit == nil) {
			t.Errorf("%q: cached %v, Parse %v", d, hit, bad)
		}
	}
}

func TestVerdictCache_FullIsDroppedAndStillRight(t *testing.T) {
	t.Parallel()
	var logs []string
	c := newVerdictCache(2, func(f string, a ...any) { logs = append(logs, fmt.Sprintf(f, a...)) })
	for round := 0; round < 3; round++ {
		for _, d := range verdictCacheDocs {
			if got, want := c.judge([]byte(d)), fresh([]byte(d)); !sameVerdict(got, want) {
				t.Errorf("round %d %q: %v, want %v", round, d, got, want)
			}
		}
	}
	if len(c.m) > 2 {
		t.Errorf("the cache holds %d entries past its bound 2", len(c.m))
	}
	if len(logs) == 0 || !strings.HasPrefix(logs[0], "DEBUG: parse-tree verdict cache reached 2 entries and was cleared") {
		t.Errorf("no DEBUG line for the drop: %q", logs)
	}
}

func TestVerdictCache_ConcurrentJudgementsAgree(t *testing.T) {
	t.Parallel()
	c := newVerdictCache(3, func(string, ...any) {})
	want := make([]error, len(verdictCacheDocs))
	for i, d := range verdictCacheDocs {
		want[i] = fresh([]byte(d))
	}
	var wg sync.WaitGroup
	errs := make(chan string, 64)
	for g := 0; g < 16; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				j := (g + i) % len(verdictCacheDocs)
				if got := c.judge([]byte(verdictCacheDocs[j])); !sameVerdict(got, want[j]) {
					select {
					case errs <- fmt.Sprintf("%q: %v, want %v", verdictCacheDocs[j], got, want[j]):
					default:
					}
				}
			}
		}(g)
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Error(e)
	}
}

// A refusal quoting a 1 MiB key: the message is parse-tree's own, already
// bounded at its source (parsetree.MaxMessageBytes), and the same text on a
// miss, on a hit and through the public decode entry.
func TestVerdictCache_LongMessageIsParseTreesOwn(t *testing.T) {
	t.Parallel()
	k := strings.Repeat("k", 1<<20)
	data := []byte("? " + k + "\n: 1\n? " + k + "\n: 2\n")
	_, bad := parsetree.Parse(data)
	if bad == nil || bad.Kind != parsetree.KindDuplicateKey {
		t.Fatalf("precondition: parse-tree gives %v, want duplicate_key", bad)
	}
	c := newVerdictCache(8, nil)
	miss, hit := c.judge(data), c.judge(data)
	_, public := ParseTenantFile(data)
	for name, err := range map[string]error{"miss": miss, "hit": hit, "ParseTenantFile": public} {
		var ue *UnreadableFileError
		if !errors.As(err, &ue) || ue.Kind != bad.Kind || ue.Message != bad.Message {
			t.Errorf("%s: %v; want parse-tree's %s with its message as is", name, err, bad.Kind)
		}
	}
}
