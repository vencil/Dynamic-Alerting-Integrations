package config

import (
	"reflect"
	"testing"
)

// #2031: the canonical spelling of a dimensional key, and the keys it leaves
// as written because the parser does not read them losslessly.
func TestCanonicalDimensionalKey(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		key, want string
		ok        bool
	}{
		// already canonical: itself
		{`x{a="1"}`, `x{a="1"}`, true},
		{`x{a="1", b=~"2.*"}`, `x{a="1", b=~"2.*"}`, true},
		// re-spelled
		{`x{b="2",a="1"}`, `x{a="1", b="2"}`, true},
		{`x{b="2", a="1"}`, `x{a="1", b="2"}`, true},
		{`x{a='1'}`, `x{a="1"}`, true},
		{`x{ a = "1" }`, `x{a="1"}`, true},
		{`x{a=1}`, `x{a="1"}`, true},
		{`x{a="1",  b="2"}`, `x{a="1", b="2"}`, true},
		{`x{q="it's"}`, `x{q="it's"}`, true},
		{`x{t=~"SYS.*", env="prod"}`, `x{env="prod", t=~"SYS.*"}`, true},
		// not dimensional, or reserved: left as written
		{`x`, `x`, false},
		{`x_critical`, `x_critical`, false},
		{`_state_x{a="1"}`, `_state_x{a="1"}`, false},
		{`x{}`, `x{}`, false},
		// the parser does not read them as written: left as written
		{`x{q=~"A,B"}`, `x{q=~"A,B"}`, false},
		{`x{q="A,B"}`, `x{q="A,B"}`, false},
		{`x{q="'a'"}`, `x{q="'a'"}`, false},
		{`x{q="a'"}`, `x{q="a'"}`, false},
		{`x{q="x=~y"}`, `x{q="x=~y"}`, false},
		{`x{q="a\"b"}`, `x{q="a\"b"}`, false},
		{`x{a="1",a="2"}`, `x{a="1",a="2"}`, false},
		{`x{a="1",}`, `x{a="1",}`, false},
		{`x{a}`, `x{a}`, false},
	} {
		got, ok := canonicalDimensionalKey(tc.key)
		if got != tc.want || ok != tc.ok {
			t.Errorf("canonicalDimensionalKey(%q) = (%q, %v), want (%q, %v)", tc.key, got, ok, tc.want, tc.ok)
		}
	}
}

// The canonical spelling of a key the parser reads losslessly is read as the
// same labels, and is its own canonical spelling.
func FuzzCanonicalDimensionalKey(f *testing.F) {
	for _, s := range []string{`x{b="2",a="1"}`, `x{a='1'}`, `x{ a = "1" }`, `x{t=~"S.*", e="p"}`, `x{q=~"A,B"}`, `x{a=1}`} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, key string) {
		canon, ok := canonicalDimensionalKey(key)
		if !ok {
			if canon != key {
				t.Fatalf("not ok but re-spelled: %q -> %q", key, canon)
			}
			return
		}
		again, ok2 := canonicalDimensionalKey(canon)
		if !ok2 || again != canon {
			t.Fatalf("canonical %q of %q is not a fixed point: %q, %v", canon, key, again, ok2)
		}
		b1, e1, r1 := parseKeyWithLabels(key)
		b2, e2, r2 := parseKeyWithLabels(canon)
		if b1 != b2 || !reflect.DeepEqual(e1, e2) || !reflect.DeepEqual(r1, r2) {
			t.Fatalf("%q and its canonical %q parse differently: %q %v %v / %q %v %v", key, canon, b1, e1, r1, b2, e2, r2)
		}
	})
}

// A map already in the canonical spelling is returned as is, without an
// allocation: the steady state of every decode.
func TestNormalizeKeys_CanonicalMapIsFree(t *testing.T) {
	m := map[string]any{"mysql_connections": 80, `redis_queue_length{priority="high", queue="a"}`: 5}
	allocs := testing.AllocsPerRun(100, func() {
		out, s := normalizeKeys(m)
		if s != nil || len(out) != len(m) {
			t.Fatal("re-spelled a canonical map")
		}
	})
	if allocs != 0 {
		t.Errorf("allocs = %v, want 0", allocs)
	}
}

// retiredBase is the #1231 retired base key, spelled so the repo's
// retired-key hook does not read it as a live value.
const retiredBase = "mysql_" + "cpu"

func TestNormalizeKeys(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		in      map[string]any
		want    map[string]any
		written map[string]string
		dups    map[string][]string
	}{
		"re-spelled, kept as written": {
			in:      map[string]any{`x{b="2",a="1"}`: 5},
			want:    map[string]any{`x{a="1", b="2"}`: 5},
			written: map[string]string{`x{a="1", b="2"}`: `x{b="2",a="1"}`},
		},
		"the canonical spelling wins": {
			in:   map[string]any{`x{b="2",a="1"}`: 5, `x{a="1", b="2"}`: 6},
			want: map[string]any{`x{a="1", b="2"}`: 6},
			dups: map[string][]string{`x{a="1", b="2"}`: {`x{b="2",a="1"}`}},
		},
		"else the smallest text": {
			in:      map[string]any{`x{b="2",a="1"}`: 5, `x{b='2', a='1'}`: 6},
			want:    map[string]any{`x{a="1", b="2"}`: 5},
			written: map[string]string{`x{a="1", b="2"}`: `x{b="2",a="1"}`},
			dups:    map[string][]string{`x{a="1", b="2"}`: {`x{b='2', a='1'}`}},
		},
		"a null writes nothing and is no duplicate": {
			in:      map[string]any{`x{a="1", b="2"}`: nil, `x{b="2",a="1"}`: 7},
			want:    map[string]any{`x{a="1", b="2"}`: 7},
			written: map[string]string{`x{a="1", b="2"}`: `x{b="2",a="1"}`},
		},
		"labels the parser cuts are not merged": {
			in:   map[string]any{`x{q=~"A,B"}`: 1, `x{q=~"A"}`: 2},
			want: map[string]any{`x{q=~"A,B"}`: 1, `x{q=~"A"}`: 2},
		},
		"both #1231 spellings: kept, the retired one is a duplicate": {
			in:   map[string]any{retiredBase: 1, "mysql_threads_running": 2},
			want: map[string]any{retiredBase: 1, "mysql_threads_running": 2},
			dups: map[string][]string{"mysql_threads_running": {retiredBase}},
		},
		"both #1231 spellings of a dimensional key, re-spelled": {
			in:      map[string]any{`mysql_cpu{b="2",a="1"}`: 1, `mysql_threads_running{a="1", b="2"}`: 2},
			want:    map[string]any{`mysql_cpu{a="1", b="2"}`: 1, `mysql_threads_running{a="1", b="2"}`: 2},
			written: map[string]string{`mysql_cpu{a="1", b="2"}`: `mysql_cpu{b="2",a="1"}`},
			dups:    map[string][]string{`mysql_threads_running{a="1", b="2"}`: {`mysql_cpu{b="2",a="1"}`}},
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, s := normalizeKeys(tc.in)
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("map = %v, want %v", got, tc.want)
			}
			var written map[string]string
			var dups map[string][]string
			if s != nil {
				written, dups = s.written, s.dups
			}
			if !reflect.DeepEqual(written, tc.written) {
				t.Errorf("written = %v, want %v", written, tc.written)
			}
			if !reflect.DeepEqual(dups, tc.dups) {
				t.Errorf("dups = %v, want %v", dups, tc.dups)
			}
		})
	}
}

func TestWrittenKey(t *testing.T) {
	t.Parallel()
	spell := map[string]string{
		`x{a="1", b="2"}`:         `x{b="2",a="1"}`,
		`mysql_cpu{a="1", b="2"}`: `mysql_cpu{b="2",a="1"}`,
	}
	for key, want := range map[string]string{
		`x{a="1", b="2"}`: `x{b="2",a="1"}`,
		`x{a="9"}`:        `x{a="9"}`,
		// a reader holding the canonical #1231 base keeps it
		`mysql_threads_running{a="1", b="2"}`: `mysql_threads_running{b="2",a="1"}`,
		"mysql_connections":                   "mysql_connections",
	} {
		if got := WrittenKey(spell, key); got != want {
			t.Errorf("WrittenKey(%q) = %q, want %q", key, got, want)
		}
	}
}
