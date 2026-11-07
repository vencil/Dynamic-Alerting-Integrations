package gitops

// #3502: a dead_entry finding about the written tenant is KeyValidation's,
// with its ratchet (an entry already on disk that the write leaves as it is
// written is a notice): the verdict's copy neither refuses the write nor is
// repeated as the tenant's advice. About another tenant of the file it is
// shown as any finding. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"testing"
)

func TestKeyVerdictRefusal_DeadEntryIsKeyValidations(t *testing.T) {
	t.Parallel()
	files := []ProposedFile{{Path: "team/ta.yaml", Tenants: []string{"ta"}}}
	dead := kvFinding("mysql_connections", "team/ta.yaml", "ta")
	dead.Kind, dead.Blocks = KindDeadEntry, true
	if r := keyVerdictRefusal(files, &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{dead}}); r != nil {
		t.Errorf("a dead_entry finding refused: %v", r)
	}
	ctx := WithTreeVisible(WithTenantNames(context.Background(), func(string) bool { return true }), true)
	neighbour := dead
	neighbour.Tenants = []string{"tb"}
	adv := adviseKeyVerdict(ctx, files, &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{dead, neighbour}})
	if len(adv.byTenant["ta"]) != 0 {
		t.Errorf("the written tenant's dead_entry repeated as advice: %q", adv.byTenant)
	}
	if len(adv.whole) != 1 {
		t.Errorf("another tenant's dead_entry not shown: %q", adv.whole)
	}
}
