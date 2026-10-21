package config

import "testing"

// TestRoleOfEachUnreadableReason pins the whitelist: only a special file and
// a stat that found nothing are not a file, only a walk error is an
// unlistable directory; a read error, a stat refused for any other reason
// (permission, EIO) and a reason nobody named here are config files whose
// read fails (#3206).
func TestRoleOfEachUnreadableReason(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		u    UnreadableFile
		want Role
	}{
		{UnreadableFile{RelKey: "locked.yaml", Reason: UnreadableReadError}, RoleConfig},
		{UnreadableFile{RelKey: "denied.yaml", Reason: UnreadableStatError}, RoleConfig}, // EACCES / EIO
		{UnreadableFile{RelKey: "gone.yaml", Reason: UnreadableStatError, Missing: true}, RoleNotAFile},
		{UnreadableFile{RelKey: "queue.yaml", Reason: UnreadableSpecialFile}, RoleNotAFile},
		{UnreadableFile{RelKey: "sub", Reason: UnreadableWalkError}, RoleUnlistableDir},
		{UnreadableFile{RelKey: "future.yaml", Reason: "a_reason_added_later"}, RoleConfig},
		// Missing means something only on a stat error.
		{UnreadableFile{RelKey: "odd.yaml", Reason: UnreadableReadError, Missing: true}, RoleConfig},
	} {
		if got := tc.u.Role(); got != tc.want {
			t.Errorf("%+v: role %q, want %q", tc.u, got, tc.want)
		}
	}
	for _, nf := range []NotFile{{RelKey: "d.yaml", Reason: NotFileDirectory}, {RelKey: "l.yaml", Reason: NotFileDirLink}} {
		if got := nf.Role(); got != RoleNotAFile {
			t.Errorf("%+v: role %q, want %q", nf, got, RoleNotAFile)
		}
	}
	if RootWalkRole != RoleUnlistableDir {
		t.Errorf("root walk role %q", RootWalkRole)
	}
}
