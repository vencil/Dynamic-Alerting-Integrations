// da-crdecode decodes a custom-resource YAML file the way a Kubernetes client
// does before sending it to the API server, and prints the result as JSON.
//
// It exists for `da_assembler.py --render-cr` (#2476): that tool used to read
// the CR with PyYAML (YAML 1.1), so a name such as `0o17` or `y` was a string
// to it while Kubernetes reads a number or a boolean. Instead of imitating
// go-yaml in Python, the Python side asks this binary, which runs the same
// conversion kubectl runs: sigs.k8s.io/yaml.YAMLToJSON.
//
// What is reproduced is that conversion only — not the API server's own
// checks (the CRD's OpenAPI schema, name / namespace validation). The Python
// caller makes its own checks on the decoded JSON.
//
// Usage:
//
//	da-crdecode <file>
//
// Output (stdout, on exit 0): one JSON object,
//
//	{"documents": [<the document, as JSON>]}
//
// `documents` is a list so that a later version can return every document of
// a multi-document file (#2529) without changing the shape; today a file with
// more than one document is refused (exit 2), as the PyYAML reader refused it.
//
// Exit codes:
//
//	0  decoded; JSON on stdout
//	2  caller error: bad arguments, the file cannot be read, the conversion
//	   refuses it (the reason is on stderr), or it holds more than one
//	   document
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	yamlv2 "go.yaml.in/yaml/v2"
	"sigs.k8s.io/yaml"
)

// programName prefixes every stderr line.
const programName = "da-crdecode"

const (
	exitOK        = 0
	exitCallerErr = 2
)

// output is the stdout contract.
type output struct {
	Documents []json.RawMessage `json:"documents"`
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) != 1 || args[0] == "" || args[0] == "-h" || args[0] == "--help" {
		fmt.Fprintf(stderr, "usage: %s <file>\n", programName)
		fmt.Fprintf(stderr, "Decodes a YAML custom resource as Kubernetes clients do "+
			"(sigs.k8s.io/yaml.YAMLToJSON) and prints {\"documents\": [...]} as JSON.\n")
		return exitCallerErr
	}
	path := args[0]
	data, err := os.ReadFile(path)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	doc, err := decode(data)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %s: %v\n", programName, path, err)
		return exitCallerErr
	}
	out, err := json.Marshal(output{Documents: []json.RawMessage{doc}})
	if err != nil {
		fmt.Fprintf(stderr, "%s: %s: %v\n", programName, path, err)
		return exitCallerErr
	}
	out = append(out, '\n')
	if _, err := stdout.Write(out); err != nil {
		fmt.Fprintf(stderr, "%s: writing stdout: %v\n", programName, err)
		return exitCallerErr
	}
	return exitOK
}

// errMultiDoc is returned for a file holding more than one YAML document.
var errMultiDoc = errors.New("the file holds more than one YAML document; only one is read")

// decode converts *data* with sigs.k8s.io/yaml.YAMLToJSON and refuses a file
// with more than one document, or one whose conversion is not fixed
// (keyCollision).
//
// YAMLToJSON itself reads only the FIRST document and ignores the rest, so the
// documents are counted separately, with the go-yaml v2 decoder YAMLToJSON is
// built on. Converting first means a broken first document is reported with
// YAMLToJSON's own error.
func decode(data []byte) (json.RawMessage, error) {
	doc, err := yaml.YAMLToJSON(data)
	if err != nil {
		return nil, err
	}
	n, err := countDocuments(data)
	if err != nil {
		return nil, err
	}
	if n > 1 {
		return nil, errMultiDoc
	}
	if err := keyCollision(data); err != nil {
		return nil, err
	}
	return doc, nil
}

// countDocuments counts the YAML documents in *data*. An empty stream has
// none; an explicit `---` with nothing after it is a document of its own.
func countDocuments(data []byte) (int, error) {
	dec := yamlv2.NewDecoder(bytes.NewReader(data))
	n := 0
	for {
		var v interface{}
		err := dec.Decode(&v)
		if errors.Is(err, io.EOF) {
			return n, nil
		}
		if err != nil {
			return n, err
		}
		n++
	}
}
