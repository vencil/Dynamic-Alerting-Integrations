//go:build !unix

package confd

import "os"

// openNoBlock: non-unix targets have no FIFO special files in the conf.d
// sense, so a plain read-only open cannot block there. tenant-api ships as a
// linux image; this exists so `go build`/`go test` still work natively on
// other development hosts.
func openNoBlock(path string) (*os.File, error) {
	return os.Open(path)
}
