package gitops

// DryRunValidate answers, without writing, the question Write would answer
// before it writes: would this body for this tenant be refused? (#2124)
//
// ⛔ IT CALLS THE WRITE PATH'S OWN CHECKS, IT DOES NOT RE-ASSEMBLE THEM. POST
// /tenants/{id}/validate used to hand-copy validate()'s checks (size gate, root
// keys, merge-with-defaults + ValidateTenantKeys, custom alerts) and so missed
// every check it did not copy — invalid YAML (a duplicate mapping key, a syntax
// error), a second YAML document, an added foreign `tenants:` section, the
// stateful eol-expansion guard, and the #1673/#2078 placement refusals — each
// answering `valid: true` for a body PUT then refused. The copy is the bug;
// this function is the one place the dry-run gets its verdict from.
//
// The sequence is write()'s steps 0 and 1, in the same order:
//
//  1. guardTenantID             — the reserved-id backstop
//  2. w.tenantFilePath          — which file the write would land on, refusing an
//     ambiguous tenant (confd.ErrAmbiguousTenantFile) and one another conf.d
//     file declares (ErrTenantDeclaredElsewhere / ErrTenantTreeScan)
//  3. validate(configDir, …)    — the full blocking + advisory check
//
// ⚠️ Those three calls are still written out twice — here and at the top of
// write() — because write() is not being changed in this fix. If write()'s
// pre-validation sequence ever changes, this must change with it; a later
// refactor can make write() call this and remove the second copy.
//
// ⛔ WHY A *Writer METHOD AND NOT A FREE FUNCTION OF configDir. tenantFilePath's
// conf.d walk is bounded by scanTree, whose stuck-walk breaker (stuckTreeScans)
// lives on the Writer. Sharing the production Writer means a walk blocked on a
// FIFO fails every later dry-run AND write fast, instead of each dry-run
// starting a fresh 5 s walk that leaks one more blocked goroutine.
//
// No side effects: it takes neither the admission token nor w.mu (write() does
// not either, before validate) and it reads the tree, it never writes to it.
//
// Return channels:
//   - err: the write would be refused BEFORE validation — reserved id,
//     ambiguous tenant file, tenant declared elsewhere, or the placement walk
//     could not run. Typed exactly as Write returns it, so the caller can render
//     it the way the write path does (the declared-elsewhere error names another
//     file's path, which only the server log may see).
//   - errs: validate()'s blocking set — non-empty means Write would return
//     ErrValidation carrying these strings. Like validate, it short-circuits:
//     a structural failure is reported alone.
//   - notices: validate()'s advisory set (#1231 1b), never blocking.
func (w *Writer) DryRunValidate(tenantID, yamlContent string) (errs, notices []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return nil, nil, err
	}
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, nil, err
	}
	errs, notices = validate(w.configDir, tenantID, filePath, yamlContent)
	return errs, notices, nil
}
