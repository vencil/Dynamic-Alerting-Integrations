package oauth2proxy

import (
	"os"
	"path/filepath"
	"regexp"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestLibraryVersionIsTheChartImage: the library these tests load configs
// with is the oauth2-proxy version the tenant-api chart deploys. The CI step
// beside this module runs the chart's image, by digest, on the same files.
// adr-check: ADR-027/9.9b
func TestLibraryVersionIsTheChartImage(t *testing.T) {
	t.Parallel()
	// A test binary's build info lists no dependencies, so go.mod is read.
	mod, err := os.ReadFile(filepath.Join(repoRoot(t), "tests", "oauth2-proxy", "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?m)^(?:require\s+|\t)github\.com/oauth2-proxy/oauth2-proxy/v7 (v\S+)$`).FindSubmatch(mod)
	if m == nil {
		t.Fatal("go.mod: no direct require of github.com/oauth2-proxy/oauth2-proxy/v7")
	}
	lib := string(m[1])
	// Both `replace x => y` and an entry inside a `replace ( ... )` block.
	if regexp.MustCompile(`(?m)^(replace[ \t]+|[ \t]+)github\.com/oauth2-proxy/oauth2-proxy/v7\b[^\n]*=>`).Match(mod) {
		t.Fatal("go.mod replaces oauth2-proxy: the tests would not run the released code")
	}
	data, err := os.ReadFile(filepath.Join(repoRoot(t), "helm", "tenant-api", "values.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var values struct {
		OAuth2Proxy struct {
			Image struct {
				Tag    string `yaml:"tag"`
				Digest string `yaml:"digest"`
			} `yaml:"image"`
		} `yaml:"oauth2Proxy"`
	}
	if err := yaml.Unmarshal(data, &values); err != nil {
		t.Fatal(err)
	}
	img := values.OAuth2Proxy.Image
	if lib == "" || lib != img.Tag {
		t.Errorf("library %q, chart image tag %q: bump go.mod with the chart (or the chart with go.mod)", lib, img.Tag)
	}
	if img.Digest == "" {
		t.Error("the chart's oauth2-proxy image has no digest; CI's --config-test would not run the deployed image")
	}
}
