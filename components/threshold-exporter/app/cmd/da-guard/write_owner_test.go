package main

import (
	"fmt"
	"io/fs"
	"reflect"
	"strings"
	"testing"
)

// TestOwnerWarning: the retry and the warning, with a fake chown — runs in
// every environment, whoever runs it (the real-kernel path is
// TestEdit_AnOwnerItCannotSetIsAWarning, which needs uid 0).
func TestOwnerWarning(t *testing.T) {
	type call struct{ uid, gid int }
	cases := []struct {
		name       string
		refuse     func(uid, gid int) bool
		calls      []call
		want, also string
		never      string
	}{
		{"both set", func(int, int) bool { return false }, []call{{10, 20}}, "", "", ""},
		{"the owner refused, the group set alone", func(uid, _ int) bool { return uid != -1 },
			[]call{{10, 20}, {-1, 20}}, "owner (uid 10)", "group (gid 20) is kept", "owner and group"},
		{"both refused", func(int, int) bool { return true },
			[]call{{10, 20}, {-1, 20}}, "owner and group (uid 10, gid 20)", "", "is kept"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var got []call
			chown := func(uid, gid int) error {
				got = append(got, call{uid, gid})
				if tc.refuse(uid, gid) {
					return fmt.Errorf("chown: %w", fs.ErrPermission)
				}
				return nil
			}
			msg := ownerWarning(chown, 10, 20)
			if !reflect.DeepEqual(got, tc.calls) {
				t.Fatalf("calls %v, want %v", got, tc.calls)
			}
			switch {
			case tc.want == "" && msg != "":
				t.Fatalf("warning %q, want none", msg)
			case tc.want != "" && (!strings.Contains(msg, tc.want) || !strings.Contains(msg, tc.also) ||
				(tc.never != "" && strings.Contains(msg, tc.never))):
				t.Fatalf("warning %q: want %q and %q, never %q", msg, tc.want, tc.also, tc.never)
			}
		})
	}
}
