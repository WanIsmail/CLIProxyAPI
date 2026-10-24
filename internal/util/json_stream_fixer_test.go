package util

import (
	"strings"
	"testing"
)

func TestJSONStreamFixerMatchesWholeInputAcrossBoundaries(t *testing.T) {
	inputs := []string{
		`{"content":"valid JSON with apostrophe ' and emoji 🐉"}`,
		`{'content': 'He said "hi" and it\'s fine', 'path': 'C:\\temp\\x'}`,
		`{'escapes':'\n\r\t\b\f\/\"\u0041\u12zz\q'}`,
		`{"quote":"escaped \" double","backslash":"\\"}`,
		`{'unfinished': 'value`, `{'unfinished': 'backslash\`,
		`{"unfinished":"value`, "", `{}`, `null`,
	}
	for _, input := range inputs {
		want := FixJSON(input)
		for split := 0; split <= len(input); split++ {
			var fixer JSONStreamFixer
			got := fixer.Write(input[:split]) + fixer.Write(input[split:]) + fixer.Finish()
			if got != want {
				t.Fatalf("split=%d input bytes=%d: streamed repair differs from whole input", split, len(input))
			}
			if fixer.Finish() != "" {
				t.Fatal("repeated finish duplicated output")
			}
		}
		var fixer JSONStreamFixer
		var got strings.Builder
		for i := range len(input) {
			got.WriteString(fixer.Write(input[i : i+1]))
		}
		got.WriteString(fixer.Finish())
		if got.String() != want {
			t.Fatal("bytewise repair differs from whole input")
		}
	}
}

func TestJSONStreamFixerEmitsProgressBeforeFinish(t *testing.T) {
	var fixer JSONStreamFixer
	if got := fixer.Write(`{"content":"first`); got != `{"content":"first` {
		t.Fatal("valid partial JSON was buffered or changed")
	}
	if got := fixer.Write(` last"}`); got != ` last"}` {
		t.Fatal("later fragment was buffered or duplicated")
	}
	if fixer.Finish() != "" {
		t.Fatal("valid JSON requires no final repair")
	}
}
