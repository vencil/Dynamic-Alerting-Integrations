package platform

import "testing"

// TestTenantIDFromBranch pins the tenant a branch name yields, in both the
// old "<prefix><tenant>/<ts>" shape and the "<ts>-<hex>" one #1724 gave the
// writer: the suffix sits after the tenant segment, so it changes nothing.
func TestTenantIDFromBranch(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ ref, want string }{
		{BranchPrefix + "db-a/20261020-141027", "db-a"},
		{BranchPrefix + "db-a/20261020-141027-0123456789ab", "db-a"},
		{BranchPrefix + "db-a-prod/20261020-141027-0123456789ab", "db-a-prod"},
		{BatchBranchPrefix + "20261020-141027", ""},
		{BatchBranchPrefix + "20261020-141027-0123456789ab", ""},
		{"feature/db-a/20261020-141027-0123456789ab", ""},
	} {
		if got := TenantIDFromBranch(c.ref); got != c.want {
			t.Errorf("TenantIDFromBranch(%q) = %q, want %q", c.ref, got, c.want)
		}
	}
	if !IsBatchBranch(BatchBranchPrefix + "20261020-141027-0123456789ab") {
		t.Error("a suffixed batch branch is not IsBatchBranch")
	}
}
