package gitops

import "github.com/vencil/tenant-api/internal/errkind"

// The ErrorKind methods below say which kind of failure each of this
// package's error types is (#3226). The HTTP answer for a kind is the
// handler's (its kindTable); this package names the kind and nothing else.
// The sentinels (ErrConflict, ErrWriteOverloaded, …) carry no method — the
// handler's ordered sentinel table names those.

// ErrorKind: TENANT_STORE_FAULT when an entry is a store fault, else
// TENANT_TREE_UNREADABLE — the same split as Unwrap.
func (e *TreeUnreadableError) ErrorKind() errkind.Kind {
	if e.StoreFault {
		return errkind.TenantStoreFault
	}
	return errkind.TenantTreeUnreadable
}

// ErrorKind is errkind.TenantFileUnreadable.
func (e *TargetUnreadableError) ErrorKind() errkind.Kind { return errkind.TenantFileUnreadable }

// ErrorKind is errkind.TenantGitManaged.
func (e *GitManagedError) ErrorKind() errkind.Kind { return errkind.TenantGitManaged }

// ErrorKind is errkind.PreconditionFailed.
func (e *PreconditionError) ErrorKind() errkind.Kind { return errkind.PreconditionFailed }

// ErrorKind is errkind.GitPushFailed.
func (e *PushError) ErrorKind() errkind.Kind { return errkind.GitPushFailed }

// ErrorKind is errkind.BaseRestoreFailed.
func (e *BaseRestoreError) ErrorKind() errkind.Kind { return errkind.BaseRestoreFailed }

// ErrorKind is errkind.BatchCommitted.
func (e *BatchCommittedError) ErrorKind() errkind.Kind { return errkind.BatchCommitted }

// ErrorKind is errkind.WriteOutcomeUnknown.
func (e *WriteOutcomeUnknownError) ErrorKind() errkind.Kind { return errkind.WriteOutcomeUnknown }

// ErrorKind is errkind.Validation: a placeholder in the body is a validation
// refusal (it unwraps to ErrValidation).
func (e *PlaceholderError) ErrorKind() errkind.Kind { return errkind.Validation }
