package gitops

// #3834: a schedule_unread finding about the written tenant is
// KeyValidation's schedule finding (#3269), with its ratchet (a schedule
// already on disk that the write leaves as it is written is a notice): the
// verdict's copy neither refuses the write nor is repeated as the tenant's
// advice — refusing it would refuse every write to a tenant whose untouched
// schedule is on disk. About another tenant of the file it is shown as any
// finding. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"testing"
)

func TestKeyVerdictRefusal_ScheduleUnreadIsKeyValidations(t *testing.T) {
	t.Parallel()
	files := []ProposedFile{{Path: "team/ta.yaml", Tenants: []string{"ta"}}}
	sched := kvFinding("_silent_mode", "team/ta.yaml", "ta")
	sched.Kind, sched.Blocks = KindScheduleUnread, true
	if r := keyVerdictRefusal(files, &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{sched}}); r != nil {
		t.Errorf("a schedule_unread finding refused: %v", r)
	}
	ctx := WithTreeVisible(WithTenantNames(context.Background(), func(string) bool { return true }), true)
	neighbour := sched
	neighbour.Tenants = []string{"tb"}
	adv := adviseKeyVerdict(ctx, files, &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{sched, neighbour}})
	if len(adv.byTenant["ta"]) != 0 {
		t.Errorf("the written tenant's schedule_unread repeated as advice: %q", adv.byTenant)
	}
	if len(adv.whole) != 1 {
		t.Errorf("another tenant's schedule_unread not shown: %q", adv.whole)
	}
}
