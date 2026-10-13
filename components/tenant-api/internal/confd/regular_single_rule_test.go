package confd

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #2939: "is this conf.d entry a regular file, and can it be opened without
// blocking" has ONE answer, pkg/config's (IsSpecialFile / OpenConfFile /
// ReadConfFile), which the exporter's walk and every tenant-api read share.
// This package used to carry its own copy (an O_NONBLOCK open plus its own
// IsRegular test on the fd); a second copy is how the walker and parse-tree
// came to disagree on a FIFO. Pinned structurally: no production file here
// may name the mode test or the open flag the rule is made of.
func TestConfd_NoSecondRegularFileRule(t *testing.T) {
	t.Parallel()
	forbidden := map[string]bool{"IsRegular": true, "O_NONBLOCK": true}
	paths, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	parsed := 0
	for _, p := range paths {
		if strings.HasSuffix(p, "_test.go") {
			continue
		}
		src, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		f, err := parser.ParseFile(fset, p, src, 0)
		if err != nil {
			t.Fatalf("%s: %v (cannot check it)", p, err)
		}
		parsed++
		ast.Inspect(f, func(n ast.Node) bool {
			if sel, ok := n.(*ast.SelectorExpr); ok && forbidden[sel.Sel.Name] {
				t.Errorf("%s: %s is a second regular-file rule; use pkg/config's IsSpecialFile / OpenConfFile / ReadConfFile",
					fset.Position(sel.Pos()), sel.Sel.Name)
			}
			return true
		})
	}
	if parsed == 0 {
		t.Fatal("no production file parsed: the check measured nothing")
	}
}
