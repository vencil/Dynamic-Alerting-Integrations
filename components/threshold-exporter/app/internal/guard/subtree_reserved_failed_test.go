package guard

// A defaults file the exporter drops (ParseFailed — since ADR-036 step 6,
// #3206, every file `da-guard parse-tree` refuses) writes no reserved key:
// checkSubtreeReservedKeys skips it, as checkDefaultsWrapper does, so an
// exit-3 tree carries no subtree_default_reserved_key finding for it.

import (
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestSubtreeReservedKeys_SkipsAFileTheExporterDrops(t *testing.T) {
	t.Parallel()
	const dropped, kept = "team/_defaults.yaml", "other/_defaults.yaml"
	input := func(parseFailed []string) CheckInput {
		return CheckInput{
			EffectiveConfigs: map[string]map[string]any{"tx": {}, "ty": {}},
			SubtreeReservedKeys: map[string]map[string][]config.SubtreeReservedWrite{
				"tx": {"_silent_mode": {{File: dropped, Value: "warning"}}},
				"ty": {"_silent_mode": {{File: kept, Value: "warning"}}},
			},
			DefaultsFiles: []config.DefaultsFile{
				{Name: dropped, Data: []byte("_silent_mode: warning\nx: !!binary aGk=\n")},
				{Name: kept, Data: []byte("defaults:\n  _silent_mode: warning\n")},
			},
			ParseFailed: parseFailed,
		}
	}
	tenantsOf := func(fs []Finding) map[string]bool {
		out := map[string]bool{}
		for _, f := range fs {
			out[f.TenantID] = true
		}
		return out
	}
	// Control: with nothing dropped both files are reported.
	if got := tenantsOf(checkSubtreeReservedKeys(input(nil))); !got["tx"] || !got["ty"] {
		t.Fatalf("control: findings for %v, want tx and ty", got)
	}
	got := tenantsOf(checkSubtreeReservedKeys(input([]string{dropped})))
	if got["tx"] || !got["ty"] {
		t.Errorf("findings for %v; want ty only (%s is dropped)", got, dropped)
	}
}
