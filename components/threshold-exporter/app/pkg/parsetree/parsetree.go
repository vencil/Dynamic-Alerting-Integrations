// Package parsetree reads one conf.d file the way ADR-036 §2 defines the
// "parse result": yaml.v3's decoded structure as plain data, or a fixed
// reason the file cannot be read (#2897). It is the YAML layer of
// `da-guard parse-tree`; which files a reader selects is the caller's
// question (pkg/config's walker), not this package's.
//
// ⛔ NO SECOND YAML SEMANTICS. Every value is what the vendored yaml.v3
// makes of the node (Node.ShortTag for the type, Node.Decode for the
// value). What this package adds is only the list of shapes it refuses
// (Kind*) — refusals, never a different reading — and the expansion of
// aliases and `<<` merges, done the way yaml.v3's decoder does it (explicit
// keys win, then each merge source in order, first one wins), with keys
// compared by their source text.
//
// ⛔ THE REFUSALS LOOK AT EVERY NODE, not at what yaml.v3's decode visits.
// yaml.v3 skips a merge source's key that the parent already sets
// (decode.go, mergedFields) and never decodes its value, so a `!!bool y`
// sitting there passes a whole-document decode while PyYAML — which
// builds every node — refuses the file (#2713). checkNode walks the parsed
// tree itself, so such a value is refused wherever it is written.
package parsetree

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

// Kind is the fixed code of one reason a file cannot be read. The set is
// closed: a reader may switch on it. Every Kind is listed in Kinds().
type Kind string

const (
	// KindReadFailure: the file (or the directory holding it) could not be
	// statted, read or listed. Set by the caller's walk, never by Parse.
	KindReadFailure Kind = "read_failure"
	// KindNotRegularFile: the path names a directory, a symlink to one, or
	// another non-regular file (a device, a FIFO). Set by the caller.
	KindNotRegularFile Kind = "not_regular_file"
	// KindInvalidUTF8: the bytes are not valid UTF-8 (this includes a
	// UTF-16 file, which yaml.v3 would decode).
	KindInvalidUTF8 Kind = "invalid_utf8"
	// KindBOMNotAtStart: a byte order mark (U+FEFF) anywhere but the first
	// three bytes.
	KindBOMNotAtStart Kind = "bom_not_at_start"
	// KindSyntaxError: yaml.v3 cannot parse the text into nodes (also a
	// `%YAML` directive it refuses, an unknown anchor, nesting deeper than
	// its limit).
	KindSyntaxError Kind = "syntax_error"
	// KindMultipleDocuments: more than one document (`---`). yaml.v3 reads
	// the first and ignores the rest.
	KindMultipleDocuments Kind = "multiple_documents"
	// KindTopLevelNotMapping: the document is not a mapping (and not
	// empty or null, which read as an empty mapping).
	KindTopLevelNotMapping Kind = "top_level_not_mapping"
	// KindTagNotAllowed: a tag outside the allowed set — scalars !!str
	// !!int !!float !!bool !!null !!timestamp; mappings and sequences
	// untagged or !!map / !!seq. A plain `<<` anywhere but as a mapping key
	// is outside it too (its tag is !!merge).
	KindTagNotAllowed Kind = "tag_not_allowed"
	// KindNonSpecificTag: a node written with the non-specific tag `!`
	// (`! "x"`, `! 1`, `! "<<": *x`), however yaml.v3 reads it. Its own
	// kind because what it means moves under the reader: PyYAML reads
	// `! "<<"` as a merge and `! 1` as a string; the vendored yaml.v3 keeps
	// `!` on a quoted scalar and merges through `! "<<"`, upstream yaml.v3
	// (once ADR-036 removes that patch) reads a literal key; `! 1` is an int
	// to both. Reading it any one way could add or drop policy silently,
	// and this document's meaning must not change when the patch goes.
	KindNonSpecificTag Kind = "non_specific_tag"
	// KindNullKey: a mapping key that resolves to null (`~:`, `null:`, an
	// empty key).
	KindNullKey Kind = "null_key"
	// KindNonScalarKey: a mapping key that is a mapping or a sequence.
	KindNonScalarKey Kind = "non_scalar_key"
	// KindDuplicateKey: two keys of one mapping with the same source text,
	// aliases resolved (`a` and `*x` where `&x a`; `a` and `"a"`).
	KindDuplicateKey Kind = "duplicate_key"
	// KindDecodeError: yaml.v3's decode refuses the file or one of its
	// values — a tag that does not fit its text (`!!int abc`, `!!bool
	// yes`), a merge value that is not a mapping (`<<: 1`), an alias that
	// contains itself, more alias expansion than yaml.v3 allows.
	KindDecodeError Kind = "decode_error"
)

// Kinds is every Kind, in the order the checks run (the first that applies
// is the one reported). It is a CLOSED LIST (ADR-036 §2, owner option B):
// the shapes whose meaning is ambiguous between YAML readers. Anything else
// is read as yaml.v3 reads it, even where today's route generator refuses
// the file only because PyYAML does — tests/shared/parse_tree_corpus.json
// pins both directions row by row.
//
// ⛔ A FRESH COPY EACH CALL. The list is read by more than one module
// (da-guard prints it, tenant-api judges with this package, #3206), and a
// package-level slice is one any importer can append to or overwrite — a
// "closed" list that is not. Mutating the returned slice changes nothing
// here.
func Kinds() []Kind {
	return append([]Kind(nil), kinds[:]...)
}

var kinds = [...]Kind{
	KindReadFailure, KindNotRegularFile, KindInvalidUTF8, KindBOMNotAtStart,
	KindSyntaxError, KindMultipleDocuments, KindNonSpecificTag, KindTagNotAllowed, KindNonScalarKey,
	KindNullKey, KindDuplicateKey, KindDecodeError, KindTopLevelNotMapping,
}

// Unreadable is why a file cannot be read.
type Unreadable struct {
	Kind    Kind
	Message string
}

func (u *Unreadable) Error() string { return string(u.Kind) + ": " + u.Message }

func fail(k Kind, format string, args ...any) *Unreadable {
	return &Unreadable{Kind: k, Message: cutMessage(fmt.Sprintf(format, args...))}
}

// MaxMessageBytes bounds Unreadable.Message. A message quotes what it refuses
// (a repeated key, a tag, yaml.v3's own error), and a quoted key can be as
// long as the file — measured, a 1 MiB duplicate key made a 1,048,644-byte
// message, which every reader then logged, kept and wrote into JSON. Cut
// here, where every message is made, so each interface (exporter log,
// tenant-api, da-guard's reports, the parse-tree JSON) carries the same text.
const MaxMessageBytes = 512

// messageTailBytes is how much of a cut message's end is kept: what a message
// says last (a repeated key's "first at line M"). The head — the line number
// and what was refused — gets the rest of MaxMessageBytes.
const messageTailBytes = 120

// cutMessage is msg itself when it fits MaxMessageBytes, else its first and
// last bytes around "… (N bytes omitted) …", at most MaxMessageBytes in all,
// cut at rune boundaries so a valid UTF-8 msg stays valid.
func cutMessage(msg string) string {
	if len(msg) <= MaxMessageBytes {
		return msg
	}
	// The marker's longest form for this msg: N is below len(msg).
	marker := len(fmt.Sprintf("… (%d bytes omitted) …", len(msg)))
	head := MaxMessageBytes - messageTailBytes - marker
	for head > 0 && !utf8.RuneStart(msg[head]) {
		head--
	}
	tail := len(msg) - messageTailBytes
	for tail < len(msg) && !utf8.RuneStart(msg[tail]) {
		tail++
	}
	return fmt.Sprintf("%s… (%d bytes omitted) …%s", msg[:head], tail-head, msg[tail:])
}

// Scalar is one scalar of the output: its source text, the tag yaml.v3
// gives it, and the value that tag means. Value is a JSON true / false /
// null for !!bool / !!null, the decimal text for !!int and !!float (a JSON
// string, so no reader loses precision), the string itself for !!str, and
// absent for !!timestamp and for an infinite or NaN !!float.
type Scalar struct {
	Text  string          `json:"text"`
	Tag   string          `json:"tag"`
	Value json.RawMessage `json:"value,omitempty"`
}

// Content is a readable file's decoded top-level mapping. Its values are
// map[string]any (a mapping, keyed by each key's source text), []any (a
// sequence) and Scalar.
type Content = map[string]any

var bom = []byte("\uFEFF")

const mergeTag = "!!merge"

var allowedScalarTags = map[string]bool{
	"!!str": true, "!!int": true, "!!float": true, "!!bool": true, "!!null": true, "!!timestamp": true,
}

// Parse reads one file's bytes. Exactly one of the results is non-nil.
func Parse(data []byte) (Content, *Unreadable) {
	return parse(data, false)
}

// Judge is Parse's verdict alone — nil exactly when Parse reads data, else
// the same Kind — without building the content: the readers that only need
// to know whether a file can be read (pkg/config's decode entries, ADR-036
// step 6) pay for the checks and the content's byte bound, not for the
// content. TestJudgeIsParsesVerdict holds the two together over the corpora.
func Judge(data []byte) *Unreadable {
	_, bad := parse(data, true)
	return bad
}

// parse is Parse; verdictOnly is Judge (the returned Content is then nil).
func parse(data []byte, verdictOnly bool) (Content, *Unreadable) {
	if !utf8.Valid(data) {
		return nil, fail(KindInvalidUTF8, "not valid UTF-8 at byte %d", firstInvalidUTF8(data))
	}
	start := 0
	if bytes.HasPrefix(data, bom) {
		start = len(bom)
	}
	if i := bytes.Index(data[start:], bom); i >= 0 {
		return nil, fail(KindBOMNotAtStart, "byte order mark (U+FEFF) at byte %d; only the first bytes of a file may hold one", start+i)
	}

	doc, bad := parseDocument(data)
	if bad != nil {
		return nil, bad
	}
	if doc == nil {
		return Content{}, nil // empty, or comments only
	}
	return judge(data, doc, verdictOnly)
}

// parseDocument parses the one document of data (nil, nil: there is none).
func parseDocument(data []byte) (*yaml.Node, *Unreadable) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var doc yaml.Node
	if err := dec.Decode(&doc); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil
		}
		return nil, fail(KindSyntaxError, "%s", oneLine(err))
	}
	var next yaml.Node
	switch err := dec.Decode(&next); {
	case err == nil:
		return nil, fail(KindMultipleDocuments, "a second document starts at line %d; only one document per file is read", next.Line)
	case !errors.Is(err, io.EOF):
		return nil, fail(KindSyntaxError, "after the first document: %s", oneLine(err))
	}
	if len(doc.Content) == 0 {
		return nil, nil
	}
	return &doc, nil
}

// judge applies the refusals to a parsed document and expands it.
func judge(data []byte, doc *yaml.Node, verdictOnly bool) (Content, *Unreadable) {
	root := doc.Content[0]

	c := &checker{values: map[*yaml.Node]json.RawMessage{}}
	// A `!` tag token is a `!` byte in the text: without one no node can
	// carry it, and the source map (a rune copy of every line, and every
	// node's position) is not built.
	if bytes.IndexByte(data, '!') >= 0 {
		c.src = newSource(data)
		c.src.index(root)
	}
	if bad := c.checkNode(root, false); bad != nil {
		return nil, bad
	}
	if bad := c.checkAliasTargets(root); bad != nil {
		return nil, bad
	}
	// The whole-document decode: what yaml.v3 refuses only when it decodes
	// the tree as a whole — an alias that contains itself, more alias
	// expansion than its limit allows. Skipped on a tree with no alias,
	// where neither can happen and every node's own decode has run (for
	// Parse and Judge alike: one path).
	if hasAlias(root) {
		var whole any
		if err := decode(doc, &whole); err != nil {
			return nil, fail(KindDecodeError, "%s", oneLine(err))
		}
	}

	switch {
	case root.Kind == yaml.MappingNode:
	case root.Kind == yaml.ScalarNode && root.ShortTag() == "!!null":
		return Content{}, nil // `---` alone, `~`: nothing in the file
	default:
		return nil, fail(KindTopLevelNotMapping, "the top level is a %s, not a mapping", kindName(root))
	}
	conv := &converter{values: c.values, active: map[*yaml.Node]bool{}, bytes: 2, verdictOnly: verdictOnly} // the content's {}
	out := Content{}
	if err := conv.fill(out, root); err != nil {
		return nil, err
	}
	if verdictOnly {
		return nil, nil
	}
	return out, nil
}

// hasAlias reports whether the tree under n holds an alias node.
func hasAlias(n *yaml.Node) bool {
	if n.Kind == yaml.AliasNode {
		return true
	}
	for _, child := range n.Content {
		if hasAlias(child) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- checks

type checker struct {
	src    *source                        // nil: the text holds no `!`
	values map[*yaml.Node]json.RawMessage // every decoded scalar but a plain string (plainString), by node
}

// plainString reports whether n is a scalar written with no tag that yaml.v3
// resolves to !!str: its decode is its text and cannot fail, so the checker
// does not decode it and its value is the text (plainStringValue).
func plainString(n *yaml.Node) bool {
	return n.Kind == yaml.ScalarNode && n.Style&yaml.TaggedStyle == 0 && n.ShortTag() == "!!str"
}

// plainStringValue is a plain string's Scalar.Value: its text as
// json.Marshal writes it — what scalarValue gives a !!str.
func plainStringValue(text string) json.RawMessage {
	raw, _ := json.Marshal(text) // a string always marshals
	return raw
}

// marshalLen is len(json.Marshal(s)) for a valid UTF-8 s, counted without
// building it: jsonStringLen, plus json.Marshal's HTML escape of `<`, `>`
// and `&` (`\u003c`: 6 bytes for 1). TestMarshalLenIsJSONMarshal holds it.
func marshalLen(s string) int {
	return jsonStringLen(s) + 5*(strings.Count(s, "<")+strings.Count(s, ">")+strings.Count(s, "&"))
}

// checkNode refuses the first shape the package does not read, in document
// order, visiting every node once (an alias is not followed: its target is
// visited where it is written). isMergeKey marks the plain `<<` that yaml.v3
// takes as a merge key; it is the one place the !!merge tag is read.
func (c *checker) checkNode(n *yaml.Node, isMergeKey bool) *Unreadable {
	if n.Kind == yaml.AliasNode {
		if n.Alias == nil {
			return fail(KindSyntaxError, "line %d: an alias with no anchor", n.Line)
		}
		return nil
	}
	if bad := c.checkTag(n, isMergeKey); bad != nil {
		return bad
	}
	switch n.Kind {
	case yaml.ScalarNode:
		if isMergeKey {
			return nil
		}
		if plainString(n) {
			// Its decode is its text, always: not decoded, not stored; the
			// converter takes the value from the text (plainStringValue).
			return nil
		}
		raw, err := scalarValue(n)
		if err != nil {
			return fail(KindDecodeError, "line %d: %s", n.Line, oneLine(err))
		}
		c.values[n] = raw
	case yaml.SequenceNode:
		for _, child := range n.Content {
			if bad := c.checkNode(child, false); bad != nil {
				return bad
			}
		}
	case yaml.MappingNode:
		seen := make(map[string]int, len(n.Content)/2)
		for i := 0; i+1 < len(n.Content); i += 2 {
			k, v := n.Content[i], n.Content[i+1]
			merge := isMerge(k)
			if bad := c.checkNode(k, merge); bad != nil {
				return bad
			}
			kr := deref(k)
			if kr.Kind != yaml.ScalarNode {
				return fail(KindNonScalarKey, "line %d: a mapping key is a %s; only scalar keys are read", k.Line, kindName(kr))
			}
			if !merge && kr.ShortTag() == "!!null" {
				return fail(KindNullKey, "line %d: the key %q is null; write the key as a quoted string if a key is meant", k.Line, kr.Value)
			}
			if first, dup := seen[kr.Value]; dup {
				return fail(KindDuplicateKey, "line %d: the key %q is written twice in one mapping (first at line %d)", k.Line, kr.Value, first)
			}
			seen[kr.Value] = k.Line
			if bad := c.checkNode(v, false); bad != nil {
				return bad
			}
			if merge {
				if bad := checkMergeValue(v); bad != nil {
					return bad
				}
			}
		}
	}
	return nil
}

// checkTag refuses the non-specific tag and a tag outside the allowed set. A
// node written with any other tag carries TaggedStyle and that tag.
//
// ⛔ `!` IS FOUND IN THE SOURCE TEXT, NEVER IN Node.Tag. yaml.v3 drops `!`
// when it builds the node (parser.node: the tag becomes the resolved or the
// default one); only the vendored patch keeps it, and only on a quoted or
// block scalar. Reading Node.Tag == "!" would make this refusal vanish
// with the patch (ADR-036 §11), so the token is read at the node's position
// for every node, and TestNonSpecificTagWithoutThePatch pins that a tree
// rewritten the way upstream yaml.v3 builds it is still refused.
func (c *checker) checkTag(n *yaml.Node, isMergeKey bool) *Unreadable {
	tag := n.Tag
	explicit := n.Style&yaml.TaggedStyle != 0
	if !explicit && c.src != nil && c.src.nonSpecificTag(n) {
		return fail(KindNonSpecificTag, "line %d: the non-specific tag `!` is not read (its meaning differs between YAML readers); remove it, or write the value quoted", n.Line)
	}
	if !explicit {
		tag = n.ShortTag()
	}
	switch n.Kind {
	case yaml.ScalarNode:
		if isMergeKey && !explicit {
			return nil
		}
		if !allowedScalarTags[tag] {
			if tag == mergeTag && !explicit {
				return fail(KindTagNotAllowed, "line %d: a plain `<<` is a merge key; anywhere but as a mapping key write it quoted (\"<<\")", n.Line)
			}
			return fail(KindTagNotAllowed, "line %d: the tag %s is not read on a scalar (allowed: !!str !!int !!float !!bool !!null !!timestamp)", n.Line, tag)
		}
	case yaml.MappingNode:
		if tag != "!!map" {
			return fail(KindTagNotAllowed, "line %d: the tag %s is not read on a mapping (allowed: none, or !!map)", n.Line, tag)
		}
	case yaml.SequenceNode:
		if tag != "!!seq" {
			return fail(KindTagNotAllowed, "line %d: the tag %s is not read on a sequence (allowed: none, or !!seq)", n.Line, tag)
		}
	}
	return nil
}

// checkAliasTargets refuses an alias to a plain `<<`: the target is a merge
// key where it is written, and anywhere an alias repeats it, it is a value or
// a key whose tag is !!merge (PyYAML merges through such a key; yaml.v3 does
// not).
func (c *checker) checkAliasTargets(n *yaml.Node) *Unreadable {
	if n.Kind == yaml.AliasNode {
		if t := n.Alias; t.Kind == yaml.ScalarNode && t.Style&yaml.TaggedStyle == 0 && t.ShortTag() == mergeTag {
			return fail(KindTagNotAllowed, "line %d: the alias *%s repeats a plain `<<` (a merge key)", n.Line, n.Value)
		}
		return nil
	}
	for _, child := range n.Content {
		if bad := c.checkAliasTargets(child); bad != nil {
			return bad
		}
	}
	return nil
}

// checkMergeValue is yaml.v3's own rule for a merge value (decoder.merge):
// a mapping, an alias to a mapping, or a sequence of those. Checked here for
// every merge key, because yaml.v3 checks only the merges its decode reaches.
func checkMergeValue(v *yaml.Node) *Unreadable {
	switch v.Kind {
	case yaml.MappingNode:
		return nil
	case yaml.AliasNode:
		if v.Alias.Kind == yaml.MappingNode {
			return nil
		}
	case yaml.SequenceNode:
		for _, item := range v.Content {
			if deref(item).Kind != yaml.MappingNode {
				return fail(KindDecodeError, "line %d: a merge (`<<`) sequence item is a %s; it must be a mapping or an alias to one", item.Line, kindName(deref(item)))
			}
		}
		return nil
	}
	return fail(KindDecodeError, "line %d: a merge (`<<`) value is a %s; it must be a mapping, an alias to one, or a sequence of those", v.Line, kindName(deref(v)))
}

// isMerge is yaml.v3's merge-key test (decode.go isMerge) for the nodes this
// package reads: the `!` alternatives it also accepts are refused by
// checkTag, and an explicit `!!merge` tag too.
func isMerge(n *yaml.Node) bool {
	return n.Kind == yaml.ScalarNode && n.Value == "<<" && n.Tag == mergeTag && n.Style&yaml.TaggedStyle == 0
}

func deref(n *yaml.Node) *yaml.Node {
	if n.Kind == yaml.AliasNode && n.Alias != nil {
		return n.Alias
	}
	return n
}

func kindName(n *yaml.Node) string {
	switch n.Kind {
	case yaml.MappingNode:
		return "mapping"
	case yaml.SequenceNode:
		return "sequence"
	case yaml.ScalarNode:
		return "scalar (" + n.ShortTag() + ")"
	case yaml.AliasNode:
		return "alias"
	}
	return "node"
}

// scalarValue decodes one scalar with yaml.v3 and renders its value per
// Scalar's contract (nil: no value).
func scalarValue(n *yaml.Node) (json.RawMessage, error) {
	var v any
	if err := decode(n, &v); err != nil {
		return nil, err
	}
	tag := n.Tag
	if n.Style&yaml.TaggedStyle == 0 {
		tag = n.ShortTag()
	}
	if tag == "!!timestamp" {
		return nil, nil
	}
	var s string
	switch x := v.(type) {
	case nil:
		return json.RawMessage("null"), nil
	case bool:
		return json.RawMessage(strconv.FormatBool(x)), nil
	case string:
		return json.Marshal(x)
	case int:
		s = strconv.FormatInt(int64(x), 10)
	case int64:
		s = strconv.FormatInt(x, 10)
	case uint64:
		s = strconv.FormatUint(x, 10)
	case uint:
		s = strconv.FormatUint(uint64(x), 10)
	case float64:
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return nil, nil
		}
		s = strconv.FormatFloat(x, 'f', -1, 64)
	case time.Time:
		return nil, nil
	default:
		return nil, fmt.Errorf("the tag %s decodes to %T, which has no value form", tag, v)
	}
	return json.Marshal(s)
}

// decode is Node.Decode that turns a panic into an error. yaml.v3 re-panics
// any runtime error raised inside its decoder (handleErr): measured, a
// collection key in a mapping that also merges (`? [a]` beside `<<:`)
// panics with "hash of unhashable type". The checks above refuse every shape
// known to do that before any decode runs; this keeps a shape not yet known
// a decode_error of its own file rather than a crash of the whole run.
func decode(n *yaml.Node, v any) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("yaml.v3 decode failed: %v", r)
		}
	}()
	return n.Decode(v)
}

// ------------------------------------------------------------- expansion

type converter struct {
	values map[*yaml.Node]json.RawMessage
	active map[*yaml.Node]bool // alias targets being expanded (a cycle guard)
	bytes  int                 // JSON bytes of the content emitted so far (MaxContentBytes)
	// verdictOnly (Judge): the same walk, the same charges and refusals, but
	// no value is built — a scalar, a sequence and a mapping's values are nil;
	// a mapping's keys are still kept, for the merge's "already set" test.
	verdictOnly bool
}

// MaxContentBytes bounds the JSON one file's content encodes to — the
// content object as `da-guard parse-tree` writes it under "files", counted
// while it is built, so expansion stops at the bound rather than after it.
//
// A bound on bytes, not on values or aliases: yaml.v3's own alias limit (kept)
// is a ratio of decoded nodes, so a small file passes it and still expands
// far — measured, 300 aliases to one 100,000-character string (a 100,914-byte
// file) came to 60 MB of JSON, and 1,000 aliases to a list of 100 short
// strings (a 10 KB file) past 8 MiB. A count of values misses the first
// shape and a count of aliases the second; bytes catch both.
//
// 8 MiB is far above any real conf.d file. Measured on this repository's
// conf.d trees: the largest content is
// rule-packs/recipes/examples/conf.d/shop-a.yaml at 4,572 bytes of JSON (the largest YAML file,
// components/threshold-exporter/config/conf.d/_defaults.yaml, 8,525 bytes,
// mostly comments, encodes to 2,513) — some 1,800 times under the bound. A
// file with no alias at all reaches it only past ~3.6 MB of YAML (40,000
// keys of 80-character values, 3,640,000 bytes, encode to 8,320,001;
// TestContentByteBound). Over the bound the file is a decode_error rather
// than megabytes every reader must load.
const MaxContentBytes = 8 << 20

// charge adds n bytes of emitted JSON, failing once over MaxContentBytes.
func (c *converter) charge(n int, at *yaml.Node) *Unreadable {
	c.bytes += n
	if c.bytes > MaxContentBytes {
		return fail(KindDecodeError, "line %d: the content expands to more than %d bytes of JSON "+
			"(aliases repeat what they point at); write the file smaller, or split it", at.Line, MaxContentBytes)
	}
	return nil
}

// jsonStringLen is the length encoding/json (SetEscapeHTML(false)) writes
// for s, quotes included. s is valid UTF-8.
func jsonStringLen(s string) int {
	n := 2
	for _, r := range s {
		switch {
		case r == '"' || r == '\\' || r == '\n' || r == '\r' || r == '\t' || r == '\b' || r == '\f':
			n += 2
		case r < 0x20 || r == '\u2028' || r == '\u2029':
			n += 6
		default:
			n += utf8.RuneLen(r)
		}
	}
	return n
}

// scalarLen is the JSON length of Scalar{text, tag, raw}:
// {"text":…,"tag":…[,"value":…]}.
func scalarLen(text, tag string, raw json.RawMessage) int {
	if raw == nil {
		return scalarLenN(text, tag, -1)
	}
	return scalarLenN(text, tag, len(raw))
}

// scalarLenN is the one count of a Scalar's JSON length, its value valueLen
// bytes long (-1: no value field).
func scalarLenN(text, tag string, valueLen int) int {
	n := len(`{"text":`) + jsonStringLen(text) + len(`,"tag":`) + jsonStringLen(tag) + len(`}`)
	if valueLen >= 0 {
		n += len(`,"value":`) + valueLen
	}
	return n
}

func (c *converter) node(n *yaml.Node) (any, *Unreadable) {
	if n.Kind == yaml.AliasNode {
		t := n.Alias
		if c.active[t] {
			return nil, fail(KindDecodeError, "line %d: the alias *%s contains itself", n.Line, n.Value)
		}
		c.active[t] = true
		defer delete(c.active, t)
		return c.node(t)
	}
	switch n.Kind {
	case yaml.ScalarNode:
		tag := n.Tag
		if n.Style&yaml.TaggedStyle == 0 {
			tag = n.ShortTag()
		}
		s := Scalar{Text: n.Value, Tag: tag, Value: c.values[n]}
		if !utf8.ValidString(s.Text) {
			return nil, fail(KindInvalidUTF8, "line %d: a scalar is not valid UTF-8", n.Line)
		}
		size := 0
		if plainString(n) {
			// The value the checker did not store: the text, as JSON. Judge
			// counts it; Parse also writes it.
			size = scalarLenN(s.Text, s.Tag, marshalLen(s.Text))
			if !c.verdictOnly {
				s.Value = plainStringValue(s.Text)
			}
		} else {
			size = scalarLen(s.Text, s.Tag, s.Value)
		}
		if bad := c.charge(size, n); bad != nil {
			return nil, bad
		}
		if c.verdictOnly {
			return nil, nil
		}
		return s, nil
	case yaml.SequenceNode:
		// [a,b,…]: the brackets and one comma per item after the first.
		sep := len(n.Content) - 1
		if sep < 0 {
			sep = 0
		}
		if bad := c.charge(2+sep, n); bad != nil {
			return nil, bad
		}
		if c.verdictOnly {
			for _, child := range n.Content {
				if _, bad := c.node(child); bad != nil {
					return nil, bad
				}
			}
			return nil, nil
		}
		out := make([]any, 0, len(n.Content))
		for _, child := range n.Content {
			v, bad := c.node(child)
			if bad != nil {
				return nil, bad
			}
			out = append(out, v)
		}
		return out, nil
	case yaml.MappingNode:
		if bad := c.charge(2, n); bad != nil { // {}
			return nil, bad
		}
		out := map[string]any{}
		if bad := c.fill(out, n); bad != nil {
			return nil, bad
		}
		if c.verdictOnly {
			return nil, nil
		}
		return out, nil
	}
	return nil, fail(KindDecodeError, "line %d: a node of an unknown kind", n.Line)
}

// fill adds mapping n to out the way yaml.v3's decoder does (decoder.mapping
// and decoder.merge): n's own keys, then its merge value — each source in
// order, depth first — where a key already in out is never replaced. Keys
// are compared by their source text.
func (c *converter) fill(out map[string]any, n *yaml.Node) *Unreadable {
	var merge *yaml.Node
	for i := 0; i+1 < len(n.Content); i += 2 {
		k := n.Content[i]
		if isMerge(k) {
			merge = n.Content[i+1]
			continue
		}
		text := deref(k).Value
		if !utf8.ValidString(text) {
			return fail(KindInvalidUTF8, "line %d: a key is not valid UTF-8", k.Line)
		}
		if _, set := out[text]; set {
			continue
		}
		// "key": — and a comma before every entry but the first.
		entry := jsonStringLen(text) + 1
		if len(out) > 0 {
			entry++
		}
		if bad := c.charge(entry, k); bad != nil {
			return bad
		}
		v, bad := c.node(n.Content[i+1])
		if bad != nil {
			return bad
		}
		out[text] = v
	}
	if merge == nil {
		return nil
	}
	sources := []*yaml.Node{merge}
	if merge.Kind == yaml.SequenceNode {
		sources = merge.Content
	}
	for _, src := range sources {
		t := src
		if src.Kind == yaml.AliasNode {
			t = src.Alias
			if c.active[t] {
				return fail(KindDecodeError, "line %d: the alias *%s contains itself", src.Line, src.Value)
			}
			c.active[t] = true
		}
		bad := c.fill(out, t)
		if src.Kind == yaml.AliasNode {
			delete(c.active, t)
		}
		if bad != nil {
			return bad
		}
	}
	return nil
}

// ---------------------------------------------------------------- source

// source maps a node's line and column (yaml.v3 counts both in characters,
// from 1, after a leading byte order mark) back to the text, to find a `!`
// that yaml.v3 parses and then drops.
type source struct {
	lines     [][]rune
	positions []pos
}

func newSource(data []byte) *source {
	text := strings.TrimPrefix(string(data), "\uFEFF")
	var lines [][]rune
	cur := []rune{}
	rs := []rune(text)
	for i := 0; i < len(rs); i++ {
		switch r := rs[i]; r {
		case '\r':
			if i+1 < len(rs) && rs[i+1] == '\n' {
				i++
			}
			lines, cur = append(lines, cur), []rune{}
		case '\n', '\u0085', ' ', ' ':
			lines, cur = append(lines, cur), []rune{}
		default:
			cur = append(cur, r)
		}
	}
	return &source{lines: append(lines, cur)}
}

// nonSpecificTag reports whether node n was written with a tag token that
// yaml.v3 did not keep — the non-specific `!` (also `!<!>`, or a %TAG handle
// that expands to `!`). A node's position is where its properties (anchor,
// tag) start; they end at its content: for a mapping or a sequence with
// entries, no later than its first entry's position, and otherwise before
// the next node of the document (an empty scalar's position is right after
// the indicator before it, so the next key's own tag is never its).
func (s *source) nonSpecificTag(n *yaml.Node) bool {
	if n.Line < 1 {
		return false
	}
	var end pos
	if (n.Kind == yaml.MappingNode || n.Kind == yaml.SequenceNode) && len(n.Content) > 0 {
		end = pos{n.Content[0].Line, n.Content[0].Column}
	} else {
		end = s.after(pos{n.Line, n.Column})
	}
	line, col := n.Line-1, n.Column-1
	for line < len(s.lines) {
		if end.line > 0 && !(pos{line + 1, col + 1}).before(end) {
			return false
		}
		l := s.lines[line]
		if col >= len(l) {
			line, col = line+1, 0
			continue
		}
		switch l[col] {
		case ' ', '\t':
			col++
		case '#':
			line, col = line+1, 0
		case '&':
			for col < len(l) && !isSeparator(l[col]) {
				col++
			}
		case '!':
			return true
		default:
			return false
		}
	}
	return false
}

// pos is a node position as yaml.v3 counts it (line and column from 1).
type pos struct{ line, col int }

func (p pos) before(q pos) bool { return p.line < q.line || p.line == q.line && p.col < q.col }

// after is the first node position of the document strictly after p (the
// zero pos when there is none).
func (s *source) after(p pos) pos {
	i := sort.Search(len(s.positions), func(i int) bool { return p.before(s.positions[i]) })
	if i == len(s.positions) {
		return pos{}
	}
	return s.positions[i]
}

// index records every node position of the tree under n, sorted.
func (s *source) index(n *yaml.Node) {
	var walk func(*yaml.Node)
	walk = func(n *yaml.Node) {
		if n.Line > 0 {
			s.positions = append(s.positions, pos{n.Line, n.Column})
		}
		for _, c := range n.Content {
			walk(c)
		}
	}
	walk(n)
	sort.Slice(s.positions, func(i, j int) bool { return s.positions[i].before(s.positions[j]) })
}

func isSeparator(r rune) bool {
	return r == ' ' || r == '\t' || r == ',' || r == '[' || r == ']' || r == '{' || r == '}'
}

func oneLine(err error) string { return strings.Join(strings.Fields(err.Error()), " ") }

func firstInvalidUTF8(b []byte) int {
	for i := 0; i < len(b); {
		r, size := utf8.DecodeRune(b[i:])
		if r == utf8.RuneError && size <= 1 {
			return i
		}
		i += size
	}
	return len(b)
}
