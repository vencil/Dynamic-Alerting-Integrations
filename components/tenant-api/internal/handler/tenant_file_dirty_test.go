package handler

// #1535 item 6: a direct write whose tenant file differs from HEAD in
// tenant-api's copy answers 409 TENANT_FILE_DIRTY with a fixed text that
// names no path; a direct batch rejects the whole batch (422), the op
// carrying the code. Nothing is written either way. (The cases git
// decides — staged, untracked, symlink targets — are pinned in gitops.)

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
)

func TestTenantFileDirty_PutAndBatchAnswers(t *testing.T) {
	t.Parallel()
	const edit = "tenants:\n  t-a:\n    mysql_connections: \"99\"\n"
	dir := seedGitTree(t, wmTree())
	if err := os.WriteFile(filepath.Join(dir, "t-a.yaml"), []byte(edit), 0o644); err != nil {
		t.Fatal(err)
	}
	head := gitHead(t, dir)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}

	w := wmPut(d, "tenants:\n  t-a:\n    mysql_connections: \"60\"\n", "")
	if w.Code != http.StatusConflict {
		t.Fatalf("PUT status = %d, want 409; body: %s", w.Code, w.Body.String())
	}
	var env ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatal(err)
	}
	if env.Code != errcode.TenantFileDirty || env.Error != msgTenantFileDirty {
		t.Errorf("PUT envelope = %+v, want %s with the fixed text", env, errcode.TenantFileDirty)
	}
	if strings.Contains(w.Body.String(), dir) || strings.Contains(w.Body.String(), "t-a.yaml") {
		t.Errorf("PUT body names a path: %s", w.Body.String())
	}

	bw := wmBatch(d, "")
	if bw.Code != http.StatusUnprocessableEntity {
		t.Fatalf("batch status = %d, want 422; body: %s", bw.Code, bw.Body.String())
	}
	var br BatchResponse
	if err := json.Unmarshal(bw.Body.Bytes(), &br); err != nil {
		t.Fatal(err)
	}
	if br.Status != batchStatusRejected || len(br.Results) != 1 || br.Results[0].Code != errcode.TenantFileDirty ||
		br.Results[0].Message != msgTenantFileDirty {
		t.Errorf("batch = %+v, want rejected with the op carrying %s", br, errcode.TenantFileDirty)
	}

	if gitHead(t, dir) != head {
		t.Error("HEAD moved")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml")); string(got) != edit {
		t.Errorf("the outside edit was not kept: %q", got)
	}
}
