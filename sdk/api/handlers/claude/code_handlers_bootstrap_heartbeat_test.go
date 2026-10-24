package claude

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/api/handlers"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	coreexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
)

type bootstrapHeartbeatExecutor struct {
	chunks           <-chan coreexecutor.StreamChunk
	waitBeforeResult <-chan struct{}
}

func (*bootstrapHeartbeatExecutor) Identifier() string { return "claude" }

func (*bootstrapHeartbeatExecutor) Execute(context.Context, *coreauth.Auth, coreexecutor.Request, coreexecutor.Options) (coreexecutor.Response, error) {
	return coreexecutor.Response{}, errors.New("not implemented")
}

func (e *bootstrapHeartbeatExecutor) ExecuteStream(ctx context.Context, _ *coreauth.Auth, _ coreexecutor.Request, _ coreexecutor.Options) (*coreexecutor.StreamResult, error) {
	if e.waitBeforeResult != nil {
		select {
		case <-e.waitBeforeResult:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return &coreexecutor.StreamResult{Chunks: e.chunks}, nil
}

func (*bootstrapHeartbeatExecutor) Refresh(_ context.Context, auth *coreauth.Auth) (*coreauth.Auth, error) {
	return auth, nil
}

func (*bootstrapHeartbeatExecutor) CountTokens(context.Context, *coreauth.Auth, coreexecutor.Request, coreexecutor.Options) (coreexecutor.Response, error) {
	return coreexecutor.Response{}, errors.New("not implemented")
}

func (*bootstrapHeartbeatExecutor) HttpRequest(context.Context, *coreauth.Auth, *http.Request) (*http.Response, error) {
	return nil, errors.New("not implemented")
}

func newBootstrapHeartbeatServer(t *testing.T, chunks <-chan coreexecutor.StreamChunk) (*httptest.Server, string) {
	return newBootstrapHeartbeatServerWithGate(t, chunks, nil)
}

func newBootstrapHeartbeatServerWithGate(t *testing.T, chunks <-chan coreexecutor.StreamChunk, waitBeforeResult <-chan struct{}) (*httptest.Server, string) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	manager := coreauth.NewManager(nil, nil, nil)
	manager.SetRetryConfig(0, 0, 0)
	manager.RegisterExecutor(&bootstrapHeartbeatExecutor{chunks: chunks, waitBeforeResult: waitBeforeResult})

	const model = "claude-bootstrap-heartbeat-test"
	auth := &coreauth.Auth{ID: "bootstrap-heartbeat-auth", Provider: "claude", Status: coreauth.StatusActive}
	registry.GetGlobalRegistry().RegisterClient(auth.ID, auth.Provider, []*registry.ModelInfo{{ID: model}})
	t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(auth.ID) })
	if _, err := manager.Register(context.Background(), auth); err != nil {
		t.Fatalf("Register() error = %v", err)
	}

	base := handlers.NewBaseAPIHandlers(&config.SDKConfig{
		Streaming: config.StreamingConfig{KeepAliveSeconds: 1},
	}, manager)
	handler := NewClaudeCodeAPIHandler(base)
	router := gin.New()
	router.POST("/v1/messages", handler.ClaudeMessages)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	return server, model
}

func newBootstrapHeartbeatRequest(t *testing.T, serverURL, model string) *http.Request {
	t.Helper()

	body := []byte(`{"model":"` + model + `","messages":[{"role":"user","content":"\u0070\u0072\u006f\u0062\u0065"}],"max_tokens":16,"stream":true}`)
	request, err := http.NewRequest(http.MethodPost, serverURL+"/v1/messages", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("NewRequest() error = %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	return request
}

func readSSEFrame(t *testing.T, reader *bufio.Reader) string {
	t.Helper()
	var frame strings.Builder
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("read SSE frame: %v", err)
		}
		if line == "\n" {
			return frame.String()
		}
		frame.WriteString(line)
	}
}

func TestClaudeStreamingSendsHeartbeatWhileWaitingForFirstProviderChunk(t *testing.T) {
	chunks := make(chan coreexecutor.StreamChunk)
	server, model := newBootstrapHeartbeatServer(t, chunks)
	request := newBootstrapHeartbeatRequest(t, server.URL, model)
	client := &http.Client{Timeout: 2500 * time.Millisecond}

	response, err := client.Do(request)
	if err != nil {
		t.Fatalf("waiting for bootstrap heartbeat: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.StatusCode, http.StatusOK)
	}
	line, err := bufio.NewReader(response.Body).ReadString('\n')
	if err != nil {
		t.Fatalf("read bootstrap heartbeat: %v", err)
	}
	if line != ": keep-alive\n" {
		t.Fatalf("first stream line = %q, want heartbeat", line)
	}
}

func TestClaudeStreamingPreservesThinkingAndToolJSONDeltasAcrossHeartbeatBoundaries(t *testing.T) {
	releaseExecution := make(chan struct{})
	released := false
	defer func() {
		if !released {
			close(releaseExecution)
		}
	}()
	chunks := make(chan coreexecutor.StreamChunk, 1)
	server, model := newBootstrapHeartbeatServerWithGate(t, chunks, releaseExecution)
	request := newBootstrapHeartbeatRequest(t, server.URL, model)
	client := &http.Client{Timeout: 7 * time.Second}

	response, err := client.Do(request)
	if err != nil {
		t.Fatalf("waiting for bootstrap heartbeat: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.StatusCode, http.StatusOK)
	}
	reader := bufio.NewReader(response.Body)
	if frame := readSSEFrame(t, reader); frame != ": keep-alive\n" {
		t.Fatalf("first SSE frame = %q, want bootstrap heartbeat", frame)
	}

	close(releaseExecution)
	released = true
	events := []struct {
		payload       string
		wantDeltaType string
		wantValue     string
	}{
		{
			payload:       "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"step \"}}\n\n",
			wantDeltaType: "thinking_delta",
			wantValue:     "step ",
		},
		{
			payload:       "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"two\"}}\n\n",
			wantDeltaType: "thinking_delta",
			wantValue:     "two",
		},
		{
			payload:       "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"path\\\":\"}}\n\n",
			wantDeltaType: "input_json_delta",
			wantValue:     "{\"path\":",
		},
		{
			payload:       "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"\\\"/tmp\\\"}\"}}\n\n",
			wantDeltaType: "input_json_delta",
			wantValue:     "\"/tmp\"}",
		},
	}

	var thinking, toolJSON strings.Builder
	for index, event := range events {
		chunks <- coreexecutor.StreamChunk{Payload: []byte(event.payload)}
		frame := readSSEFrame(t, reader)
		lines := strings.Split(strings.TrimSuffix(frame, "\n"), "\n")
		if len(lines) != 2 || lines[0] != "event: content_block_delta" || !strings.HasPrefix(lines[1], "data: ") {
			t.Fatalf("event %d frame = %q, want one complete content_block_delta SSE frame", index, frame)
		}
		var envelope struct {
			Delta struct {
				Type        string `json:"type"`
				Thinking    string `json:"thinking"`
				PartialJSON string `json:"partial_json"`
			} `json:"delta"`
		}
		if err := json.Unmarshal([]byte(strings.TrimPrefix(lines[1], "data: ")), &envelope); err != nil {
			t.Fatalf("event %d contains invalid JSON: %v; frame=%q", index, err, frame)
		}
		if envelope.Delta.Type != event.wantDeltaType {
			t.Fatalf("event %d delta type = %q, want %q", index, envelope.Delta.Type, event.wantDeltaType)
		}
		value := envelope.Delta.Thinking
		if event.wantDeltaType == "input_json_delta" {
			value = envelope.Delta.PartialJSON
			toolJSON.WriteString(value)
		} else {
			thinking.WriteString(value)
		}
		if value != event.wantValue {
			t.Fatalf("event %d delta value = %q, want %q", index, value, event.wantValue)
		}
		if index < len(events)-1 {
			if frameHeartbeat := readSSEFrame(t, reader); frameHeartbeat != ": keep-alive\n" {
				t.Fatalf("frame after event %d = %q, want heartbeat boundary", index, frameHeartbeat)
			}
		}
	}
	close(chunks)

	if thinking.String() != "step two" {
		t.Fatalf("combined thinking delta = %q, want %q", thinking.String(), "step two")
	}
	if toolJSON.String() != `{"path":"/tmp"}` || !json.Valid([]byte(toolJSON.String())) {
		t.Fatalf("combined tool JSON = %q, want valid object", toolJSON.String())
	}
}

func TestClaudeStreamingReportsDelayedBootstrapErrorAfterHeartbeat(t *testing.T) {
	chunks := make(chan coreexecutor.StreamChunk, 1)
	go func() {
		time.Sleep(1200 * time.Millisecond)
		chunks <- coreexecutor.StreamChunk{Err: &coreauth.Error{
			HTTPStatus: http.StatusBadGateway,
			Message:    "delayed upstream failure",
		}}
		close(chunks)
	}()
	server, model := newBootstrapHeartbeatServer(t, chunks)
	request := newBootstrapHeartbeatRequest(t, server.URL, model)
	client := &http.Client{Timeout: 3 * time.Second}

	response, err := client.Do(request)
	if err != nil {
		t.Fatalf("waiting for bootstrap heartbeat: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want heartbeat-committed %d", response.StatusCode, http.StatusOK)
	}
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("read delayed bootstrap error stream: %v", err)
	}
	text := string(payload)
	if !strings.Contains(text, ": keep-alive\n\n") || !strings.Contains(text, "event: error\n") {
		t.Fatalf("stream = %q, want heartbeat followed by SSE error", text)
	}
}

func TestClaudeStreamingPreservesImmediateBootstrapErrorStatus(t *testing.T) {
	chunks := make(chan coreexecutor.StreamChunk, 1)
	chunks <- coreexecutor.StreamChunk{Err: &coreauth.Error{
		HTTPStatus: http.StatusBadGateway,
		Message:    "immediate upstream failure",
	}}
	close(chunks)
	server, model := newBootstrapHeartbeatServer(t, chunks)
	request := newBootstrapHeartbeatRequest(t, server.URL, model)
	client := &http.Client{Timeout: 2 * time.Second}

	response, err := client.Do(request)
	if err != nil {
		t.Fatalf("waiting for immediate bootstrap error: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d before heartbeat", response.StatusCode, http.StatusBadGateway)
	}
}
