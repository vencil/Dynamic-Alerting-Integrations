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
// It also says where that reading is not the text the file was written with
// (ADR-036 §7 step 11, #3206; aswritten.go): a key `010:` the conversion
// reads as "8", a string it reads as other text. A caller that takes names
// from the JSON reads that report; it does not read the YAML a second time.
//
// Usage:
//
//	da-crdecode <file>
//	da-crdecode -              (reads standard input)
//	da-crdecode --any-shape <file|->
//
// Two readings (#3426):
//
//   - The default is a Kubernetes manifest, as kubectl reads one. After the
//     conversion kubectl decodes each document into an object
//     (k8s.io/apimachinery's unstructured.UnstructuredJSONScheme), which
//     refuses a document that is not a mapping ("cannot unmarshal array into
//     Go value of type unstructured.detector") and one with no kind ("Object
//     'Kind' is missing"). Here each such document is refused too: a
//     document that is no object — an array, a string, a number — or one
//     whose `kind` is missing, not a string, or empty. The refusal names the
//     line the document starts on only when the file holds several documents
//     (a one-document file gets no line number); a refused list item is
//     named `items[i] (line N)`, the line left out when it cannot be told.
//     A list (a document with a top-level `items`: `kind: List`, the output
//     of `kubectl get … -o yaml`) is replaced by its items, in order, as kubectl
//     applies them; each item must be an object with a non-empty string
//     `kind` and `apiVersion`, and a list inside a list is refused, as kubectl
//     refuses it. A list with content of its own (a `data`, a `spec` beside
//     `items`), which kubectl discards without a word, is refused (manifest.go). So a caller can rely on every entry of
//     `documents` being an object with a non-empty string `kind`, none of
//     them a list.
//   - `--any-shape` reads YAML that is not a manifest with the same
//     conversion: a ConfigMap data value (a rule file, an Alertmanager
//     config), a plain rule file, a Helm values file. Nothing is checked about
//     the shape of a document; everything else (the split, the conversion,
//     the as-written comparison) is the same.
//
// Output (stdout, on exit 0): one JSON object,
//
//	{"documents": [<each document, as JSON>, ...],
//	 "differs_from_text": [[<difference>, ...], ...],
//	 "reading": "manifest" | "any-shape"}
//
// `reading` names which of the two readings below produced the output, so a
// caller that relies on the manifest reading's guarantee can tell that it
// was given (an older da-crdecode prints no `reading` and checks no shape).
//
// A file may hold several documents (ADR-036 §12, #3206). They are split the
// way `kubectl apply -f` splits a file (k8s.io/apimachinery's YAMLReader,
// read by cli-runtime's StreamVisitor; splitDocuments): at every line that
// starts with `---`, which may be followed only by spaces or a comment —
// anything else after it (`--- a: 1`, `---x`) is refused, as kubectl refuses
// it. Each piece is converted on its own, and a piece kubectl skips is
// skipped: one that converts to nothing or to null (only `---`, blank lines,
// comments, or an explicit `null`). So `documents` holds one entry per
// object kubectl would apply, in file order — none for an empty file, a list's
// items in its place — and `differs_from_text` holds one list per entry of
// `documents` (an item's differences, with paths from the item).
// Each document's objects carry their keys in the order the file writes them;
// aswritten.go describes the entries of `differs_from_text`.
//
// Not reproduced: kubectl first tries to read a stream as JSON (a file that
// starts with `{`) and reads several concatenated JSON objects that way; here
// every piece goes through the YAML conversion, which reads one JSON object
// the same way and refuses several.
//
// Exit codes:
//
//	0  decoded; JSON on stdout
//	2  caller error: bad arguments, the file cannot be read, a `---` line
//	   carries content, the conversion refuses a document (the reason is on
//	   stderr, with the line the document starts on when the file holds
//	   several), a piece between two
//	   `---` lines holds more than one YAML document, a document cannot be
//	   read as written to compare with the conversion, or — without
//	   `--any-shape` — a document or a list item is no Kubernetes object (not
//	   a mapping; no string `kind`, or an empty one; an item without a string
//	   `apiVersion`), a list holds a list, or a list's own text is not read as
//	   written (its items' differences could not be told apart)
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
	Documents       []json.RawMessage `json:"documents"`
	DiffersFromText [][]difference    `json:"differs_from_text"`
	Reading         string            `json:"reading"`
}

// The values of output.Reading.
const (
	readingManifest = "manifest"
	readingAnyShape = "any-shape"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// stdinName is how standard input is named in messages.
const stdinName = "<stdin>"

// anyShapeFlag reads YAML that is not a manifest: no document shape is checked.
const anyShapeFlag = "--any-shape"

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	anyShape := false
	if len(args) > 0 && args[0] == anyShapeFlag {
		anyShape = true
		args = args[1:]
	}
	if len(args) != 1 || args[0] == "" || args[0] == "-h" || args[0] == "--help" {
		fmt.Fprintf(stderr, "usage: %s [%s] <file|->\n", programName, anyShapeFlag)
		fmt.Fprintf(stderr, "Decodes a YAML file as Kubernetes clients do "+
			"(sigs.k8s.io/yaml.YAMLToJSON, one document after another as `kubectl apply -f` "+
			"splits them) and prints {\"documents\": [...], \"differs_from_text\": [...]} as JSON. "+
			"`-` reads standard input. Every document must be a Kubernetes object (a mapping "+
			"with a non-empty string `kind`; a list is replaced by its items) unless %s is given, which reads YAML "+
			"that is not a manifest (a ConfigMap data value, a rule file, Helm values).\n", anyShapeFlag)
		return exitCallerErr
	}
	name := args[0]
	var data []byte
	var err error
	if name == "-" {
		name = stdinName
		data, err = io.ReadAll(stdin)
	} else {
		data, err = os.ReadFile(name)
	}
	if err != nil {
		fmt.Fprintf(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	pieces, err := splitDocuments(data)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %s: %v\n", programName, name, err)
		return exitCallerErr
	}
	result := output{Documents: []json.RawMessage{}, DiffersFromText: [][]difference{}, Reading: readingManifest}
	if anyShape {
		result.Reading = readingAnyShape
	}
	for _, p := range pieces {
		source := p.atFileLines()
		doc, differs, skip, err := decodeDocument(source)
		docs, docDiffers := []json.RawMessage{doc}, [][]difference{differs}
		if err == nil && !skip && !anyShape {
			docs, docDiffers, err = manifestDocuments(source, doc, differs)
		}
		if err != nil {
			if len(pieces) > 1 {
				fmt.Fprintf(stderr, "%s: %s: the document starting at line %d: %v\n", programName, name, p.line, err)
			} else {
				fmt.Fprintf(stderr, "%s: %s: %v\n", programName, name, err)
			}
			return exitCallerErr
		}
		if skip {
			continue
		}
		result.Documents = append(result.Documents, docs...)
		result.DiffersFromText = append(result.DiffersFromText, docDiffers...)
	}
	out, err := json.Marshal(result)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %s: %v\n", programName, name, err)
		return exitCallerErr
	}
	out = append(out, '\n')
	if _, err := stdout.Write(out); err != nil {
		fmt.Fprintf(stderr, "%s: writing stdout: %v\n", programName, err)
		return exitCallerErr
	}
	return exitOK
}

// piece is the text between two `---` lines and the line it starts on.
type piece struct {
	data []byte
	line int
}

// atFileLines is the piece behind one newline for each line of the file
// before it, so that a line number the YAML parsers report is the file's.
// Blank lines before a document change nothing it holds.
func (p piece) atFileLines() []byte {
	if p.line <= 1 {
		return p.data
	}
	out := make([]byte, 0, p.line-1+len(p.data))
	out = append(out, bytes.Repeat([]byte("\n"), p.line-1)...)
	return append(out, p.data...)
}

// docSeparator starts a line that ends one document (YAMLReader's separator).
const docSeparator = "---"

// splitDocuments splits *data* as k8s.io/apimachinery's YAMLReader.Read does
// (pkg/util/yaml/decoder.go, v0.36.1), which `kubectl apply -f` reads a file
// through: line by line, a line starting with `---` ends the piece before it,
// and only spaces or a comment may follow the `---` on that line. A `---`
// line with nothing before it in the piece ends nothing, so leading and
// repeated separators give no piece; the text after the last separator is the
// last piece.
func splitDocuments(data []byte) ([]piece, error) {
	var pieces []piece
	var buf bytes.Buffer
	start := 1
	for n, line := range bytes.SplitAfter(data, []byte("\n")) {
		if len(line) == 0 {
			continue // the empty tail SplitAfter leaves after a final newline
		}
		if bytes.HasPrefix(line, []byte(docSeparator)) {
			trimmed := bytes.TrimSpace(line[len(docSeparator):])
			if len(trimmed) > 0 && trimmed[0] != '#' {
				return nil, fmt.Errorf("line %d: invalid YAML document separator: %s "+
					"(kubectl refuses anything but a comment after `---`)", n+1, trimmed)
			}
			if buf.Len() != 0 {
				pieces = append(pieces, piece{data: append([]byte(nil), buf.Bytes()...), line: start})
				buf.Reset()
			}
			start = n + 2
			continue
		}
		buf.Write(line)
	}
	if buf.Len() != 0 {
		pieces = append(pieces, piece{data: buf.Bytes(), line: start})
	}
	return pieces, nil
}

// decodeDocument converts one piece. skip is true for a piece kubectl skips:
// one whose conversion is empty or null (cli-runtime's StreamVisitor.Visit).
func decodeDocument(data []byte) (json.RawMessage, []difference, bool, error) {
	doc, err := decode(data)
	if err != nil {
		return nil, nil, false, err
	}
	if t := bytes.TrimSpace(doc); len(t) == 0 || bytes.Equal(t, []byte("null")) {
		return nil, nil, true, nil
	}
	doc, differs, err := compareAsWritten(data, doc)
	if err != nil {
		return nil, nil, false, err
	}
	return doc, differs, false, nil
}

// jsonShape names the JSON type of *v*, as encoding/json's errors do.
func jsonShape(v json.RawMessage) string {
	t := bytes.TrimSpace(v)
	if len(t) == 0 {
		return "empty"
	}
	switch t[0] {
	case '{':
		return "object"
	case '[':
		return "array"
	case '"':
		return "string"
	case 't', 'f':
		return "bool"
	case 'n':
		return "null"
	default:
		return "number"
	}
}

// errMultiDoc is returned for a piece holding more than one YAML document,
// which YAMLToJSON would read only the first of. Pieces are split at `---`
// lines first, so no input is known to reach it; it stays so that such a
// piece is refused rather than cut short.
var errMultiDoc = errors.New("it holds more than one YAML document; " +
	"only `---` lines separate documents")

// decode converts *data* with sigs.k8s.io/yaml.YAMLToJSON and refuses a piece
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
// none.
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
