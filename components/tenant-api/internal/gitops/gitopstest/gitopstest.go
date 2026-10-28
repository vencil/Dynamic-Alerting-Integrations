// Package gitopstest is test support for code that builds a gitops.Writer.
// Import it from _test.go files only: production wires the generator gate
// (cmd/server/main.go), and nothing else may stand in for it.
//
// #1535 made the Writer's WriteValidator required — a Writer without one
// refuses every content-changing write with gitops.ErrNoValidator — so a
// test that writes through a Writer for any other reason than the
// generator's verdict builds it with NewWriter here.
package gitopstest

import (
	"context"

	"github.com/vencil/tenant-api/internal/gitops"
)

// Permissive is a gitops.WriteValidator that passes every write.
type Permissive struct{}

// ValidateWrites passes, with no key verdict.
func (Permissive) ValidateWrites(context.Context, string, []gitops.ProposedFile) (*gitops.KeyVerdict, error) {
	return nil, nil
}

// NewWriter is gitops.NewWriter with Permissive installed. A test that is
// about the verdict installs its own validator over it (SetValidator).
func NewWriter(configDir, gitDir string) *gitops.Writer {
	w := gitops.NewWriter(configDir, gitDir)
	w.SetValidator(Permissive{})
	return w
}
