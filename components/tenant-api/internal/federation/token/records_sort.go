package token

import "sort"

// sortRecordsByIssuedAt sorts records in place, oldest first (ascending
// IssuedAt). Shared by the JSON-file and ConfigMap stores so their
// list / listAll / flush paths all return the same stable oldest-first
// ordering.
func sortRecordsByIssuedAt(records []Record) {
	sort.Slice(records, func(i, j int) bool { return records[i].IssuedAt.Before(records[j].IssuedAt) })
}
