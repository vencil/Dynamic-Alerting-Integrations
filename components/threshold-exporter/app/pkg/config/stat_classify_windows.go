//go:build windows

package config

// wrongPathErrnos: see StatErrIsWrongPath (stat_classify.go).
var wrongPathErrnos = windowsWrongPathErrnos
