package receiverspec

// ProxyURLProblem: which proxy_url strings Alertmanager refuses (#2295).
//
// Alertmanager decodes http_config.proxy_url with Go's net/url.Parse and
// refuses the whole config when it fails. This file is a port of that parser
// as the Alertmanager release we deploy runs it (amtool 0.34.1, built with
// go1.26.8), rather than a call to this binary's own net/url:
//
//   - the verdict must not move with the toolchain this package is built
//     with — Go 1.26's GODEBUG urlstrictcolons is decided by the MAIN module's
//     go version, so the same net/url.Parse answers `http://h:1:2/`
//     differently in Alertmanager and here (measured: amtool accepts it);
//   - the Python route generator needs the same answer, and holds a line-for-
//     line port of this function (_lib_validation._go_url_parse_problem).
//
// Pinned by the proxy rows of testdata/receiver_presence_cases.json, whose
// `am` column tests/alertmanager-inhibit asserts with config.Load.

import (
	"net/netip"
	"strings"
)

// ProxyURLProblem returns why net/url.Parse (Go 1.26, urlstrictcolons=0)
// refuses s, or "" when it parses.
func ProxyURLProblem(s string) string {
	u, frag, _ := strings.Cut(s, "#")
	if p := goParse(u); p != "" {
		return p
	}
	if frag != "" {
		if _, ok := goUnescape(frag, encFragment); !ok {
			return "invalid URL escape in the fragment"
		}
	}
	return ""
}

type urlEncoding int

const (
	encPath urlEncoding = iota
	encHost
	encZone
	encUserPassword
	encFragment
)

func goParse(raw string) string {
	for i := 0; i < len(raw); i++ {
		if raw[i] < ' ' || raw[i] == 0x7f {
			return "invalid control character in URL"
		}
	}
	if raw == "*" {
		return ""
	}
	scheme, rest, bad := goScheme(raw)
	if bad {
		return "missing protocol scheme"
	}
	if strings.HasSuffix(rest, "?") && strings.Count(rest, "?") == 1 {
		rest = rest[:len(rest)-1]
	} else {
		rest, _, _ = strings.Cut(rest, "?")
	}
	if !strings.HasPrefix(rest, "/") {
		if scheme != "" {
			return "" // opaque
		}
		if seg, _, _ := strings.Cut(rest, "/"); strings.Contains(seg, ":") {
			return "first path segment in URL cannot contain colon"
		}
	}
	if (scheme != "" || !strings.HasPrefix(rest, "///")) && strings.HasPrefix(rest, "//") {
		authority := rest[2:]
		rest = ""
		if i := strings.Index(authority, "/"); i >= 0 {
			authority, rest = authority[:i], authority[i:]
		}
		if p := goAuthority(authority); p != "" {
			return p
		}
	}
	if _, ok := goUnescape(rest, encPath); !ok {
		return "invalid URL escape in the path"
	}
	return ""
}

func goScheme(raw string) (scheme, rest string, bad bool) {
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		switch {
		case 'a' <= c && c <= 'z' || 'A' <= c && c <= 'Z':
		case '0' <= c && c <= '9' || c == '+' || c == '-' || c == '.':
			if i == 0 {
				return "", raw, false
			}
		case c == ':':
			if i == 0 {
				return "", "", true
			}
			return raw[:i], raw[i+1:], false
		default:
			return "", raw, false
		}
	}
	return "", raw, false
}

func goAuthority(authority string) string {
	i := strings.LastIndex(authority, "@")
	host := authority
	if i >= 0 {
		host = authority[i+1:]
	}
	if p := goHost(host); p != "" {
		return p
	}
	if i < 0 {
		return ""
	}
	userinfo := authority[:i]
	for _, r := range userinfo {
		if 'A' <= r && r <= 'Z' || 'a' <= r && r <= 'z' || '0' <= r && r <= '9' ||
			strings.ContainsRune("-._:~!$&'()*+,;=%@", r) {
			continue
		}
		return "invalid userinfo"
	}
	user, pass, _ := strings.Cut(userinfo, ":")
	if _, ok := goUnescape(user, encUserPassword); !ok {
		return "invalid URL escape in the userinfo"
	}
	if _, ok := goUnescape(pass, encUserPassword); !ok {
		return "invalid URL escape in the userinfo"
	}
	return ""
}

func goHost(host string) string {
	open := strings.LastIndex(host, "[")
	switch {
	case open > 0:
		return "invalid IP-literal"
	case open == 0:
		closing := strings.LastIndex(host, "]")
		if closing < 0 {
			return "missing ']' in host"
		}
		if !validOptionalPort(host[closing+1:]) {
			return "invalid port after host"
		}
		name := host[1:closing]
		var unescaped string
		if zi := strings.Index(name, "%25"); zi >= 0 {
			h, ok1 := goUnescape(name[:zi], encHost)
			z, ok2 := goUnescape(name[zi:], encZone)
			if !ok1 || !ok2 {
				return "invalid host"
			}
			unescaped = h + z
		} else {
			h, ok := goUnescape(name, encHost)
			if !ok {
				return "invalid host"
			}
			unescaped = h
		}
		addr, err := netip.ParseAddr(unescaped)
		if err != nil {
			return "invalid host: " + err.Error()
		}
		if addr.Is4() {
			return "invalid IP-literal"
		}
		return ""
	}
	// urlstrictcolons=0: the port is what follows the LAST colon.
	if i := strings.LastIndex(host, ":"); i != -1 && !validOptionalPort(host[i:]) {
		return "invalid port after host"
	}
	if _, ok := goUnescape(host, encHost); !ok {
		return "invalid character or escape in host"
	}
	return ""
}

func validOptionalPort(port string) bool {
	if port == "" {
		return true
	}
	if port[0] != ':' {
		return false
	}
	for i := 1; i < len(port); i++ {
		if port[i] < '0' || port[i] > '9' {
			return false
		}
	}
	return true
}

func isHex(c byte) bool {
	return '0' <= c && c <= '9' || 'a' <= c && c <= 'f' || 'A' <= c && c <= 'F'
}

func unhex(c byte) byte {
	switch {
	case '0' <= c && c <= '9':
		return c - '0'
	case 'a' <= c && c <= 'f':
		return c - 'a' + 10
	}
	return c - 'A' + 10
}

// goUnescape is net/url.unescape's validation half: ok is false where it
// errors. The unescaped text is returned for the IP-literal check.
func goUnescape(s string, mode urlEncoding) (string, bool) {
	var b strings.Builder
	for i := 0; i < len(s); {
		switch c := s[i]; {
		case c == '%':
			if i+2 >= len(s) || !isHex(s[i+1]) || !isHex(s[i+2]) {
				return "", false
			}
			v := unhex(s[i+1])<<4 | unhex(s[i+2])
			if mode == encHost && unhex(s[i+1]) < 8 && s[i:i+3] != "%25" {
				return "", false
			}
			if mode == encZone && s[i:i+3] != "%25" && v != ' ' && hostByteNeedsEscape(v) {
				return "", false
			}
			b.WriteByte(v)
			i += 3
		default:
			if (mode == encHost || mode == encZone) && c < 0x80 && hostByteNeedsEscape(c) {
				return "", false
			}
			b.WriteByte(c)
			i++
		}
	}
	return b.String(), true
}

// hostByteNeedsEscape is net/url.shouldEscape(c, encodeHost) for c < 0x80.
func hostByteNeedsEscape(c byte) bool {
	if 'a' <= c && c <= 'z' || 'A' <= c && c <= 'Z' || '0' <= c && c <= '9' {
		return false
	}
	return !strings.ContainsRune("-_.~!$&'()*+,;=:[]<>\"", rune(c))
}
