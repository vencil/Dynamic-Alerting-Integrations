package main

// configHashField is a document's `config_hash` (#3965): the identity of the
// walk the document was computed from (pkg/config treeIdentity — the value
// `da-guard parse-tree` reports for the same tree and the exporter serves on
// /api/v1/config/identity), so a reader can tell whether two documents read
// the same tree. null (`identity` "") when the document claims none: no walk
// ran, or an overlay made it read a tree that is not the one on disk.
func configHashField(identity string) *string {
	if identity == "" {
		return nil
	}
	return &identity
}
