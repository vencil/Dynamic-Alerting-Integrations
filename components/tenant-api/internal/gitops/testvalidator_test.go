package gitops

import "context"

// permissiveValidator passes every judgement. #1535 made the validator
// required — a Writer without one refuses every write (ErrNoValidator) —
// so a test about something other than the generator's verdict installs
// this one (newTestWriter). Tests about the verdict install their own
// (recordingValidator, …) over it. The cross-package twin is
// gitopstest.Permissive; this package's tests cannot import that one
// (gitopstest imports gitops).
type permissiveValidator struct{}

func (permissiveValidator) ValidateWrites(context.Context, string, []ProposedFile) error { return nil }

// newTestWriter is NewWriter with permissiveValidator installed.
func newTestWriter(configDir, gitDir string) *Writer {
	w := NewWriter(configDir, gitDir)
	w.SetValidator(permissiveValidator{})
	return w
}
