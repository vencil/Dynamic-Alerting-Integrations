package main

// The checks edit / emit run on --input before decoding it: Go's
// encoding/json silently turns invalid UTF-8 and an unpaired surrogate escape
// into U+FFFD (two different keys could then become one), keeps the last of
// two members with the same name, and matches a struct field's name in any
// case. Each of those is a caller error here, so what is written is exactly
// what was sent.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"unicode/utf8"
)

// checkInputText refuses input that is not valid UTF-8, holds a \u escape
// of a surrogate that does not form a valid pair, or gives one member twice
// in any object.
func checkInputText(raw []byte) error {
	if !utf8.Valid(raw) {
		return errors.New("not valid UTF-8")
	}
	if err := checkSurrogates(raw); err != nil {
		return err
	}
	return checkDuplicateMembers(raw)
}

func hex4(b []byte, at int) (rune, bool) {
	if at+4 > len(b) {
		return 0, false
	}
	var r rune
	for _, c := range b[at : at+4] {
		r <<= 4
		switch {
		case c >= '0' && c <= '9':
			r |= rune(c - '0')
		case c >= 'a' && c <= 'f':
			r |= rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			r |= rune(c-'A') + 10
		default:
			return 0, false
		}
	}
	return r, true
}

// checkSurrogates walks the JSON strings' escapes: a high surrogate
// (\uD800-\uDBFF) must be followed at once by a low one (\uDC00-\uDFFF); a
// low one alone is refused.
func checkSurrogates(raw []byte) error {
	in := false
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if !in {
			in = c == '"'
			continue
		}
		switch c {
		case '"':
			in = false
		case '\\':
			if i+1 < len(raw) && raw[i+1] == 'u' {
				r, ok := hex4(raw, i+2)
				switch {
				case ok && r >= 0xD800 && r <= 0xDBFF:
					if i+7 < len(raw) && raw[i+6] == '\\' && raw[i+7] == 'u' {
						if r2, ok2 := hex4(raw, i+8); ok2 && r2 >= 0xDC00 && r2 <= 0xDFFF {
							i += 11
							continue
						}
					}
					return fmt.Errorf("an unpaired surrogate escape \\u%04X at byte %d", r, i)
				case ok && r >= 0xDC00 && r <= 0xDFFF:
					return fmt.Errorf("an unpaired surrogate escape \\u%04X at byte %d", r, i)
				}
				i += 5
				continue
			}
			i++ // the escaped character
		}
	}
	return nil
}

// checkDuplicateMembers refuses an object that names one member twice.
func checkDuplicateMembers(raw []byte) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var walk func() error
	walk = func() error {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		d, ok := tok.(json.Delim)
		if !ok {
			return nil
		}
		switch d {
		case '{':
			seen := map[string]bool{}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return err
				}
				k, _ := kt.(string)
				if seen[k] {
					return fmt.Errorf("the member %q is given twice in one object", k)
				}
				seen[k] = true
				if err := walk(); err != nil {
					return err
				}
			}
		case '[':
			for dec.More() {
				if err := walk(); err != nil {
					return err
				}
			}
		}
		_, err = dec.Token() // the closing delimiter
		return err
	}
	if err := walk(); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}

// checkMemberNames refuses a member whose name is not spelled exactly as
// the input names it (encoding/json would match "OPS" to "ops").
func checkMemberNames(cmd string, raw []byte) error {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return err
	}
	want := map[string]bool{"ops": true}
	if cmd == emitCmd {
		want = map[string]bool{"content": true}
	}
	for k := range top {
		if !want[k] {
			return fmt.Errorf("unknown member %q", k)
		}
	}
	if cmd != editCmd {
		return nil
	}
	var ops []json.RawMessage
	if err := json.Unmarshal(top["ops"], &ops); err != nil {
		return nil // decodeStrict names the shape error
	}
	for i, o := range ops {
		var m map[string]json.RawMessage
		if err := json.Unmarshal(o, &m); err != nil {
			return nil
		}
		for k := range m {
			if k != "op" && k != "path" && k != "value" {
				return fmt.Errorf("op %d: unknown member %q", i, k)
			}
		}
	}
	return nil
}
