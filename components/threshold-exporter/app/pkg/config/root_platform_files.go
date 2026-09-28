package config

// RootPlatformFile is one conf.d ROOT platform file: its name (a root-level
// scan key, so no directory part) and its bytes.
type RootPlatformFile struct {
	Name string
	Data []byte
}

// RootPlatformFiles returns the conf.d ROOT's platform files — every
// `_`-prefixed config file there except a defaults carrier the chain did not
// select — in name order, with their bytes (#2280, for pkg/routingpolicy).
//
// ⛔ Read by the ONE walker (scanRootPlatform: same hidden / extension / stat
// / read rules as every plane), never by a second directory lister —
// confd_walker_population_test pins walkDirTree as the only listing site
// (#1911). An entry the walker kept without bytes (it could not be read) is
// left out, as the walker leaves it out of every map. The error is the
// walker's: the root could not be walked.
func RootPlatformFiles(configDir string) ([]RootPlatformFile, error) {
	scan, err := scanRootPlatform(configDir)
	if err != nil {
		return nil, err
	}
	var out []RootPlatformFile
	for _, k := range rootPlatformKeys(scan) {
		f := scan.Files[k]
		if f == nil || f.Data == nil {
			continue
		}
		out = append(out, RootPlatformFile{Name: k, Data: f.Data})
	}
	return out, nil
}
