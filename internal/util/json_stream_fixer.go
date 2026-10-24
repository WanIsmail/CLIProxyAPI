package util

import "strings"

// JSONStreamFixer performs the same quote repair as FixJSON, without waiting
// for a complete tool argument. Keep the two implementations aligned; the
// boundary tests compare streaming output with the existing whole-input path.
// Its zero value is ready for one stream. Finish must be called at stream end.
type JSONStreamFixer struct {
	inDouble bool
	inSingle bool
	escaped  bool
}

// Write returns the repaired fragment. UTF-8 bytes are preserved even when a
// caller splits a code point across fragments; only ASCII syntax is examined.
func (f *JSONStreamFixer) Write(input string) string {
	var out strings.Builder
	for i := range len(input) {
		c := input[i]
		if f.inDouble {
			out.WriteByte(c)
			if f.escaped {
				f.escaped = false
			} else if c == '\\' {
				f.escaped = true
			} else if c == '"' {
				f.inDouble = false
			}
			continue
		}
		if f.inSingle {
			if f.escaped {
				f.escaped = false
				if c != '\'' {
					out.WriteByte('\\')
				}
				out.WriteByte(c)
			} else if c == '\\' {
				f.escaped = true
			} else if c == '\'' {
				out.WriteByte('"')
				f.inSingle = false
			} else {
				if c == '"' {
					out.WriteByte('\\')
				}
				out.WriteByte(c)
			}
			continue
		}
		switch c {
		case '"':
			f.inDouble = true
			out.WriteByte(c)
		case '\'':
			f.inSingle = true
			out.WriteByte('"')
		default:
			out.WriteByte(c)
		}
	}
	return out.String()
}

// Finish preserves FixJSON's best-effort closure of an unfinished single
// quoted string, including its handling of a dangling backslash.
func (f *JSONStreamFixer) Finish() string {
	if f.inSingle {
		f.inSingle = false
		f.escaped = false
		return `"`
	}
	return ""
}
