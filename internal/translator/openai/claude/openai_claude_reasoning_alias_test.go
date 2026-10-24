package claude

import (
	"context"
	"strings"
	"testing"

	"github.com/tidwall/gjson"
)

func TestReasoningAliasesStreamBeforeCompletion(t *testing.T) {
	for _, fields := range []string{
		`"reasoning":"step"`,
		`"reasoning_content":"step"`,
		`"reasoning_content":null,"reasoning":"step"`,
		`"reasoning_content":"","reasoning":"step"`,
		`"reasoning_content":"step","reasoning":"duplicate"`,
	} {
		t.Run(fields, func(t *testing.T) {
			var state any
			chunks := ConvertOpenAIResponseToClaude(context.Background(), "m", []byte(streamReq), nil,
				[]byte(`data: {"id":"c1","model":"m","choices":[{"delta":{`+fields+`},"finish_reason":null}]}`), &state)
			var thinking strings.Builder
			for _, chunk := range chunks {
				for _, line := range strings.Split(string(chunk), "\n") {
					if strings.HasPrefix(line, "data: ") {
						payload := strings.TrimPrefix(line, "data: ")
						if gjson.Get(payload, "delta.type").String() == "thinking_delta" {
							thinking.WriteString(gjson.Get(payload, "delta.thinking").String())
						}
					}
				}
			}
			if thinking.String() != "step" {
				t.Fatalf("thinking must arrive before finish/DONE; got %q", thinking.String())
			}
		})
	}
}

func TestReasoningAliasNonStream(t *testing.T) {
	raw := []byte(`{"id":"c1","model":"m","choices":[{"message":{"role":"assistant","reasoning":"step","content":"answer"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2}}`)
	got := ConvertOpenAIResponseToClaudeNonStream(context.Background(), "m", nil, nil, raw, nil)
	var thinking string
	for _, block := range gjson.GetBytes(got, "content").Array() {
		if block.Get("type").String() == "thinking" {
			thinking += block.Get("thinking").String()
		}
	}
	if thinking != "step" {
		t.Fatalf("nonstream reasoning lost: got %q", thinking)
	}
}
