//go:build !windows

package config

import "syscall"

// wrongPathErrnos: see StatErrIsWrongPath (stat_classify.go).
var wrongPathErrnos = []syscall.Errno{syscall.ENOTDIR, syscall.ELOOP}
