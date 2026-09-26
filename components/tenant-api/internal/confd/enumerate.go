package confd

import (
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// This file is the third half of the package's promise (#1680, the #1911
// family). confd.go answers "does this NAME count as a tenant file";
// resolve.go answers "WHICH file is tenant X's"; this file answers "WHICH
// tenant files does this directory hold" and "is this one USABLE, and if
// not, why".
//
// Before it existed, five callers each hand-wrote the same enumeration loop
// around the shared name predicate — handler.loadAllTenants,
// account.ListTenantIDs, orphan.scanKnownTenants, orphan.scanSubsetTenants and
// matchTenantFiles below — and only the first one also read the file. It
// silently `continue`d on a read or parse failure, so a tenant whose file
// broke (malformed YAML, a dangling symlink, a symlink to a directory)
// vanished from GET /api/v1/tenants with a 200, while the federation planes,
// the startup guard and the write plane all still counted it.
//
// ⛔ THE CONTRACT IS "REPORT, CALLERS DECIDE" — not a shared gate. The owner
// ruled out both uniform answers on #1680, for structural reasons:
//
//   - Making usability a shared GATE (every loop skips unusable files) would
//     make the orphan detector under-count live tenants — the direction its
//     own comment calls catastrophic, because every artifact of a missing
//     tenant is then flagged as orphaned — and would leave the write plane
//     unable to resolve a broken file, so no API could ever repair it.
//   - Making every loop CONTENT-BLIND cannot work for the list handler,
//     whose summary fields ARE the parsed content.
//
// So enumeration (ListTenantFiles) is one loop that never looks at content,
// and usability (ReadTenantFile) is one classifier that only the caller who
// needs content calls. The federation enumerators and the write-plane
// resolver deliberately stay content-blind; the list handler reads, and
// reports a broken file as a degraded row instead of dropping it.

// TenantFile is one conf.d entry that classifies as a tenant config file.
type TenantFile struct {
	// ID is the tenant id, TenantIDFromFile's answer for Name.
	ID string
	// Name is the directory entry's base name, as os.ReadDir returned it.
	Name string
}

// ListTenantFiles is THE conf.d enumeration loop: every entry of dir that is
// not a directory and whose name satisfies TenantIDFromFile, in os.ReadDir
// order (sorted by filename). It never reads file content.
//
// The IsDir test is the DirEntry's own type bit, i.e. lstat semantics: a
// symlink — dangling, or pointing at a directory — is NOT a directory here
// and IS listed. That is deliberate and matches every caller's behaviour
// before this helper existed; whether such an entry is usable is
// ReadTenantFile's question, answered only for callers that ask it.
//
// Duplicates are NOT folded: `acme.yaml` beside `acme.yml` yields two entries
// with the same ID. Each caller keeps its own stance on that shape (the list
// handler refuses the whole listing, the write-plane resolver answers
// ErrAmbiguousTenantFile, the orphan detector counts the tenant once).
//
// A ReadDir error is returned unchanged, so callers can keep telling a
// missing directory (errors.Is(err, fs.ErrNotExist)) from any other failure.
func ListTenantFiles(dir string) ([]TenantFile, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	files := make([]TenantFile, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		id, ok := TenantIDFromFile(e.Name())
		if !ok {
			continue
		}
		files = append(files, TenantFile{ID: id, Name: e.Name()})
	}
	return files, nil
}

// FileProblem classifies why a tenant file is not usable. The empty value
// (ProblemNone) means usable.
//
// ⛔ The non-empty values are an API CONTRACT: GET /api/v1/tenants returns
// them verbatim in TenantSummary.config_error. Keep them stable, lowercase
// snake_case, and add a value rather than renaming one.
type FileProblem string

const (
	// ProblemNone: the file is a readable regular file whose bytes parse as
	// YAML.
	ProblemNone FileProblem = ""

	// ProblemUnreadable: stat or read failed — a dangling symlink, a
	// permission error, an I/O error, or the entry disappearing between the
	// listing and the read.
	ProblemUnreadable FileProblem = "unreadable"

	// ProblemNotRegularFile: the path (after following symlinks) exists but
	// is not a regular file — typically a symlink to a directory. Checked
	// BEFORE reading, which also keeps a FIFO from blocking the reader
	// forever.
	ProblemNotRegularFile FileProblem = "not_regular_file"

	// ProblemMalformedYAML: the bytes do not parse as YAML at all. This is a
	// SYNTAX judgement only — confd deliberately knows nothing about the
	// threshold-exporter schema, so a well-formed document of the wrong shape
	// is usable here and left to the caller to judge.
	ProblemMalformedYAML FileProblem = "malformed_yaml"
)

// ReadTenantFile reads the tenant file name in dir, following symlinks, and
// classifies it. On ProblemNone data holds the file's bytes; otherwise data
// is nil and problem says why.
//
// An EMPTY file (or one holding only comments/whitespace) is usable: it is
// valid YAML (an empty document), and the list handler has always listed it
// as a row with no metadata. Treating it as broken here would change a
// behaviour no caller asked to change.
//
// name is expected to come from ListTenantFiles; only its base name is used,
// so a caller cannot reach outside dir through it.
func ReadTenantFile(dir, name string) (data []byte, problem FileProblem) {
	path := filepath.Join(dir, filepath.Base(name))
	fi, err := os.Stat(path)
	if err != nil {
		return nil, ProblemUnreadable
	}
	if !fi.Mode().IsRegular() {
		return nil, ProblemNotRegularFile
	}
	data, err = os.ReadFile(path)
	if err != nil {
		return nil, ProblemUnreadable
	}
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return nil, ProblemMalformedYAML
	}
	return data, ProblemNone
}
