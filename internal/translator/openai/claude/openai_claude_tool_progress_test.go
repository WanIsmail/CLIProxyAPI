package claude

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/tidwall/gjson"
)

func TestToolArgumentsStreamAfterDelayedIdentity(t *testing.T) {
	var state any
	feed := func(call map[string]any, finish any) string {
		chunk, _ := json.Marshal(map[string]any{"id": "fixture", "choices": []any{map[string]any{
			"index": 0, "delta": map[string]any{"tool_calls": []any{call}}, "finish_reason": finish,
		}}})
		var result strings.Builder
		for _, b := range ConvertOpenAIResponseToClaude(context.Background(), "", []byte(streamReq), nil, append([]byte("data: "), chunk...), &state) {
			result.Write(b)
		}
		return result.String()
	}
	first := feed(map[string]any{"index": 0, "function": map[string]any{"name": "Write", "arguments": `{"content":"first`}}, nil)
	if strings.Contains(first, "input_json_delta") {
		t.Fatal("arguments emitted before identity was available")
	}
	second := feed(map[string]any{"index": 0, "id": "call_fixture"}, nil)
	if !strings.Contains(second, "input_json_delta") || !strings.Contains(second, "content_block_start") {
		t.Fatal("buffered prefix did not flush when identity became available")
	}
	third := feed(map[string]any{"index": 0, "function": map[string]any{"arguments": ` last"}`}}, nil)
	if !strings.Contains(third, "input_json_delta") {
		t.Fatal("later argument fragment did not stream before completion")
	}
	last := feed(map[string]any{"index": 0}, "tool_calls")
	if strings.Contains(last, "input_json_delta") {
		t.Fatal("completion duplicated already-streamed arguments")
	}
}

func TestStreamingToolArgumentsPreserveParallelAndRepair(t *testing.T) {
	events := runStream(t, streamReq,
		`{"id":"fixture","choices":[{"delta":{"tool_calls":[{"index":0,"id":"first","function":{"name":"Write","arguments":"{'content': 'first\\"}},{"index":1,"id":"second","function":{"name":"Write","arguments":"{\"content\":\"other\"}"}}]}}]}`,
		`{"id":"fixture","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"' last'}"}}]}}]}`,
		`{"id":"fixture","choices":[{"delta":{},"finish_reason":"tool_calls"}]}`,
	)
	assertSequentialContentBlocks(t, events)
	args := map[int64]string{}
	for _, e := range events {
		if e.Type == "content_block_delta" && gjson.Get(e.Payload, "delta.type").String() == "input_json_delta" {
			args[gjson.Get(e.Payload, "index").Int()] += gjson.Get(e.Payload, "delta.partial_json").String()
		}
	}
	if args[0] != `{"content": "first' last"}` || args[1] != `{"content":"other"}` {
		t.Fatal("parallel tool arguments were duplicated, reordered or incorrectly repaired")
	}
	if countByType(events, "content_block_stop") != 2 || countByType(events, "message_stop") != 1 {
		t.Fatal("tool blocks did not close exactly once")
	}
}
