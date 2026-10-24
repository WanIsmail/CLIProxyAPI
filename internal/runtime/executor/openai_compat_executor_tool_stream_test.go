package executor

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
)

// Hold upstream completion until the downstream client receives the first
// argument fragment. A proxy that buffers arguments until finish deadlocks
// this handshake and can hide active generation from the client's watchdog.
func TestOpenAICompatExecutorToolArgumentsArriveBeforeFinish(t *testing.T) {
	gate := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(gate) }) }
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: "+`{"id":"fixture","model":"dragon-qwen3.8-flash-next-nvfp4","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_fixture","type":"function","function":{"name":"Write","arguments":"{\"content\":\"first"}}]},"finish_reason":null}]}`+"\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-gate:
		case <-r.Context().Done():
			return
		}
		_, _ = io.WriteString(w, "data: "+`{"id":"fixture","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":" last\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":8}}`+"\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	defer release()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	executor := NewOpenAICompatExecutor("openai-compatibility", &config.Config{})
	auth := &cliproxyauth.Auth{Attributes: map[string]string{"base_url": server.URL + "/v1", "api_key": "fixture"}}
	body := []byte(`{"model":"dragon-qwen3.8-flash-next-nvfp4","messages":[{"role":"user","content":"fixture"}],"tools":[{"name":"Write","input_schema":{"type":"object","properties":{"content":{"type":"string"}}}}],"stream":true,"max_tokens":32}`)
	result, err := executor.ExecuteStream(ctx, auth, cliproxyexecutor.Request{Model: "dragon-qwen3.8-flash-next-nvfp4", Payload: body},
		cliproxyexecutor.Options{SourceFormat: sdktranslator.FormatClaude, OriginalRequest: body, Stream: true})
	if err != nil {
		t.Fatal(err)
	}
	sawInput, sawStop := false, false
	for chunk := range result.Chunks {
		if chunk.Err != nil {
			t.Fatalf("stream failed before tool argument progress: %v (input=%t)", chunk.Err, sawInput)
		}
		if strings.Contains(string(chunk.Payload), `"type":"input_json_delta"`) {
			sawInput = true
			release()
		}
		if strings.Contains(string(chunk.Payload), "event: message_stop") {
			sawStop = true
		}
	}
	if !sawInput || !sawStop {
		t.Fatalf("argument before finish=%t; terminal event=%t", sawInput, sawStop)
	}
}
