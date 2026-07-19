package token

import "sort"

// sortRecordsByIssuedAt sorts records in place, oldest first (ascending
// IssuedAt). Shared by the JSON-file and ConfigMap stores so their
// list / listAll / flush paths order identically. sort.Slice is not stable
// and equal IssuedAt values have no tie-breaker, so the relative order of
// equal-timestamp records is unspecified.
func sortRecordsByIssuedAt(records []Record) {
	sort.Slice(records, func(i, j int) bool { return records[i].IssuedAt.Before(records[j].IssuedAt) })
}
