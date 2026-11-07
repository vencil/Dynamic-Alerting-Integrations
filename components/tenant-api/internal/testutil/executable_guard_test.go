package testutil

// The guard for WriteExecutable (executable.go): no test file in this module,
// nor this test-helper package, writes a file with an execute bit through
// os.WriteFile or os.OpenFile. Production code is out of scope: it does not
// run a file it has just written.
// go/ast, not grep: a call that spans lines or a mode in a comment is not
// mistaken either way. A mode that is not a constant cannot be judged, so it
// fails too.

import (
	"go/ast"
	"go/constant"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExecutableWritesGoThroughWriteExecutable(t *testing.T) {
	t.Parallel()
	root := moduleRoot(t)
	fset := token.NewFileSet()
	var violations []string
	writes := 0
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if name := d.Name(); name == "testdata" || name == "vendor" || strings.HasPrefix(name, ".") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		rel = filepath.ToSlash(rel)
		if !strings.HasSuffix(rel, "_test.go") && !strings.HasPrefix(rel, "internal/testutil/") {
			return nil
		}
		f, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			return perr
		}
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			pkg, ok := sel.X.(*ast.Ident)
			if !ok || pkg.Name != "os" || len(call.Args) != 3 ||
				(sel.Sel.Name != "WriteFile" && sel.Sel.Name != "OpenFile") {
				return true
			}
			if rel == "internal/testutil/executable.go" {
				return true // the helper itself, which holds the lock
			}
			writes++
			pos := fset.Position(call.Pos())
			pos.Filename = rel
			mode, known := constMode(call.Args[2])
			switch {
			case !known:
				violations = append(violations, pos.String()+": os."+sel.Sel.Name+
					" with a mode that is not a constant: write it as a literal, or use testutil.WriteExecutable")
			case mode&0o111 != 0:
				violations = append(violations, pos.String()+": os."+sel.Sel.Name+
					" writes an executable: use testutil.WriteExecutable (ETXTBSY, golang/go#22315)")
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatalf("walk module: %v", err)
	}
	// Vacuity guard: the walk found the module's file writes.
	if writes < 50 {
		t.Fatalf("found only %d os.WriteFile/os.OpenFile calls: the walk is not reading the module", writes)
	}
	for _, v := range violations {
		t.Error(v)
	}
}

// constMode evaluates a mode argument built from integer literals (0o755,
// 0o600|0o100); anything else is unknown.
func constMode(e ast.Expr) (int64, bool) {
	tv, err := types.Eval(token.NewFileSet(), nil, token.NoPos, types.ExprString(e))
	if err != nil || tv.Value == nil {
		return 0, false
	}
	v, ok := constant.Int64Val(constant.ToInt(tv.Value))
	return v, ok
}

func moduleRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found above the testutil package directory")
		}
		dir = parent
	}
}
