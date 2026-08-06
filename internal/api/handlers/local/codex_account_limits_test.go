package local

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/runtime/executor/helps"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
)

type fakeAuthManager struct {
	auths                        []*auth.Auth
	response                     *http.Response
	requestErr                   error
	httpErr                      error
	selectedAuth                 *auth.Auth
	requestMethod                string
	requestURL                   string
	requestHeaders               http.Header
	newRequestRedirectsDisabled  bool
	httpRequestRedirectsDisabled bool
	newRequestHasDeadline        bool
	httpRequestHasDeadline       bool
	requireDeadline              bool
}

func (f *fakeAuthManager) List() []*auth.Auth {
	return f.auths
}

func (f *fakeAuthManager) NewHttpRequest(
	ctx context.Context,
	selected *auth.Auth,
	method string,
	targetURL string,
	body []byte,
	headers http.Header,
) (*http.Request, error) {
	f.selectedAuth = selected
	f.requestMethod = method
	f.requestURL = targetURL
	f.requestHeaders = headers.Clone()
	f.newRequestRedirectsDisabled = helps.RedirectsDisabled(ctx)
	_, f.newRequestHasDeadline = ctx.Deadline()
	if f.requestErr != nil {
		return nil, f.requestErr
	}
	return http.NewRequestWithContext(ctx, method, targetURL, nil)
}

func (f *fakeAuthManager) HttpRequest(
	ctx context.Context,
	_ *auth.Auth,
	_ *http.Request,
) (*http.Response, error) {
	f.httpRequestRedirectsDisabled = helps.RedirectsDisabled(ctx)
	_, f.httpRequestHasDeadline = ctx.Deadline()
	if f.httpErr != nil {
		return nil, f.httpErr
	}
	if f.requireDeadline {
		if !f.httpRequestHasDeadline {
			return nil, errors.New("synthetic missing deadline")
		}
		return nil, context.DeadlineExceeded
	}
	return f.response, nil
}

func activeCodexAuth(id string, accountID string) *auth.Auth {
	return &auth.Auth{
		ID:       id,
		Provider: "codex",
		Status:   auth.StatusActive,
		Attributes: map[string]string{
			auth.AttributeAuthKind: auth.AuthKindOAuth,
		},
		Metadata: map[string]any{"account_id": accountID},
	}
}

func jsonResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func serveAccountLimits(manager authManager) *httptest.ResponseRecorder {
	handler := &Handler{authManager: manager}
	router := gin.New()
	router.GET("/limits", handler.GetCodexAccountLimits)
	req := httptest.NewRequest(http.MethodGet, "/limits", nil)
	rr := httptest.NewRecorder()
	router.ServeHTTP(rr, req)
	return rr
}

func decodeLimitsResponse(t *testing.T, rr *httptest.ResponseRecorder) accountLimitsResponse {
	t.Helper()
	var body accountLimitsResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return body
}

func TestCodexAccountLimitsReturnsNormalizedCanonicalWindows(t *testing.T) {
	gin.SetMode(gin.TestMode)

	selected := activeCodexAuth("codex-test", "account-example")
	manager := &fakeAuthManager{
		auths: []*auth.Auth{selected},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 12.5,
					"reset_at": 1786000000
				},
				"secondary_window": {
					"limit_window_seconds": 604800,
					"used_percent": 27,
					"reset_at": 1786500000
				}
			}
		}`),
	}
	rr := serveAccountLimits(manager)

	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if body.SchemaVersion != 1 {
		t.Fatalf("schema_version = %d, want 1", body.SchemaVersion)
	}
	if len(body.Windows) != 2 {
		t.Fatalf("windows = %#v, want 2 canonical windows", body.Windows)
	}
	if got := body.Windows[0]; got.WindowMinutes != 300 || got.UsedPercent != 12.5 || got.ResetsAt != 1786000000 {
		t.Fatalf("five-hour window = %#v, want normalized canonical window", got)
	}
	if got := body.Windows[1]; got.WindowMinutes != 10080 || got.UsedPercent != 27 || got.ResetsAt != 1786500000 {
		t.Fatalf("seven-day window = %#v, want normalized canonical window", got)
	}
	if manager.selectedAuth != selected {
		t.Fatal("request did not use the selected Codex account")
	}
	if manager.requestMethod != http.MethodGet {
		t.Fatalf("request method = %q, want GET", manager.requestMethod)
	}
	if manager.requestURL != "https://chatgpt.com/backend-api/wham/usage" {
		t.Fatalf("request URL = %q, want official account usage resource", manager.requestURL)
	}
	if got := manager.requestHeaders.Get("Chatgpt-Account-Id"); got != "account-example" {
		t.Fatalf("account scope header = %q, want synthetic account ID", got)
	}
	if got := manager.requestHeaders.Get("Accept"); got != "application/json" {
		t.Fatalf("Accept = %q, want application/json", got)
	}
	if got := manager.requestHeaders.Get("User-Agent"); got != "codex_cli_rs" {
		t.Fatalf("User-Agent = %q, want stable Codex client identity", got)
	}
	if got := manager.requestHeaders.Get("Originator"); got != "codex_cli_rs" {
		t.Fatalf("Originator = %q, want stable Codex client identity", got)
	}
}

func TestCodexAccountLimitsResponseContainsOnlyNormalizedQuota(t *testing.T) {
	gin.SetMode(gin.TestMode)

	sensitiveMarker := "synthetic-private-marker"
	selected := activeCodexAuth("private-auth-index", "account-example")
	selected.FileName = "/example/private-auth.json"
	selected.Label = sensitiveMarker
	selected.Metadata["access_token"] = sensitiveMarker
	manager := &fakeAuthManager{
		auths: []*auth.Auth{selected},
		response: jsonResponse(http.StatusOK, `{
			"account_id": "account-example",
			"email": "person@example.com",
			"private_marker": "`+sensitiveMarker+`",
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 12.5,
					"reset_at": 1786000000
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	for _, forbidden := range []string{
		sensitiveMarker,
		"private-auth-index",
		"private-auth.json",
		"account-example",
		"person@example.com",
		"access_token",
	} {
		if strings.Contains(rr.Body.String(), forbidden) {
			t.Fatalf("body = %q, leaked forbidden marker %q", rr.Body.String(), forbidden)
		}
	}
	var body map[string]json.RawMessage
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if len(body) != 2 || body["schema_version"] == nil || body["windows"] == nil {
		t.Fatalf("response keys = %#v, want only schema_version and windows", body)
	}
}

func TestCodexAccountLimitsDisablesUpstreamRedirects(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 12.5
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	if !manager.newRequestRedirectsDisabled || !manager.httpRequestRedirectsDisabled {
		t.Fatalf(
			"redirect guard = new:%t execute:%t, want both enabled",
			manager.newRequestRedirectsDisabled,
			manager.httpRequestRedirectsDisabled,
		)
	}
}

func TestCodexAccountLimitsOrdersWindowsByCanonicalDuration(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 604800,
					"used_percent": 27,
					"reset_at": 1786500000
				},
				"secondary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 12.5,
					"reset_at": 1786000000
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if len(body.Windows) != 2 {
		t.Fatalf("windows = %#v, want 2 canonical windows", body.Windows)
	}
	if body.Windows[0].WindowMinutes != 300 || body.Windows[1].WindowMinutes != 10080 {
		t.Fatalf("window order = %#v, want 300 then 10080 minutes", body.Windows)
	}
}

func TestCodexAccountLimitsDiscardsInvalidResetOnly(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 42.5,
					"reset_at": 99999999999
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if len(body.Windows) != 1 || body.Windows[0].WindowMinutes != 300 || body.Windows[0].UsedPercent != 42.5 {
		t.Fatalf("windows = %#v, want valid utilization without reset", body.Windows)
	}
	if strings.Contains(rr.Body.String(), `"resets_at"`) {
		t.Fatalf("body = %s, want invalid reset omitted", rr.Body.String())
	}
}

func TestCodexAccountLimitsDiscardsMalformedResetOnly(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 42.5,
					"reset_at": "not-a-timestamp"
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if len(body.Windows) != 1 || body.Windows[0].WindowMinutes != 300 || body.Windows[0].UsedPercent != 42.5 {
		t.Fatalf("windows = %#v, want valid utilization without malformed reset", body.Windows)
	}
	if strings.Contains(rr.Body.String(), `"resets_at"`) {
		t.Fatalf("body = %s, want malformed reset omitted", rr.Body.String())
	}
}

func TestCodexAccountLimitsDiscardsOutOfRangeUtilization(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 101,
					"reset_at": 1786000000
				},
				"secondary_window": {
					"limit_window_seconds": 604800,
					"used_percent": 27,
					"reset_at": 1786500000
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if len(body.Windows) != 1 || body.Windows[0].WindowMinutes != 10080 {
		t.Fatalf("windows = %#v, want only valid seven-day utilization", body.Windows)
	}
}

func TestCodexAccountLimitsDiscardsMalformedUtilizationWindow(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": "not-a-percentage"
				},
				"secondary_window": {
					"limit_window_seconds": 604800,
					"used_percent": 27
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if len(body.Windows) != 1 || body.Windows[0].WindowMinutes != 10080 {
		t.Fatalf("windows = %#v, want only independently valid seven-day window", body.Windows)
	}
}

func TestCodexAccountLimitsDiscardsMalformedDurationWindow(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": "not-a-duration",
					"used_percent": 12.5
				},
				"secondary_window": {
					"limit_window_seconds": 604800,
					"used_percent": 27
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	body := decodeLimitsResponse(t, rr)
	if len(body.Windows) != 1 || body.Windows[0].WindowMinutes != 10080 {
		t.Fatalf("windows = %#v, want only independently valid seven-day window", body.Windows)
	}
}

func TestCodexAccountLimitsRejectsAllMalformedCanonicalWindows(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": "not-a-percentage"
				},
				"secondary_window": {
					"limit_window_seconds": 604800,
					"used_percent": null
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusBadGateway, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_usage_malformed"}` {
		t.Fatalf("body = %q, want sanitized malformed-data error", rr.Body.String())
	}
}

func TestCodexAccountLimitsRejectsDuplicateCanonicalDuration(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 12.5,
					"reset_at": 1786000000
				},
				"secondary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 25,
					"reset_at": 1786100000
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusBadGateway, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_usage_malformed"}` {
		t.Fatalf("body = %q, want sanitized malformed-data error", rr.Body.String())
	}
}

func TestCodexAccountLimitsRejectsDuplicateDurationWithInvalidUtilization(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 101
				},
				"secondary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 25
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusBadGateway, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_usage_malformed"}` {
		t.Fatalf("body = %q, want sanitized duplicate-duration error", rr.Body.String())
	}
}

func TestCodexAccountLimitsRejectsMalformedUsageData(t *testing.T) {
	gin.SetMode(gin.TestMode)

	for _, test := range []struct {
		name string
		body string
	}{
		{name: "invalid JSON", body: `{"rate_limit":`},
		{name: "missing quota structure", body: `{}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			manager := &fakeAuthManager{
				auths:    []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
				response: jsonResponse(http.StatusOK, test.body),
			}

			rr := serveAccountLimits(manager)
			if rr.Code != http.StatusBadGateway {
				t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusBadGateway, rr.Body.String())
			}
			if rr.Body.String() != `{"error":"codex_account_usage_malformed"}` {
				t.Fatalf("body = %q, want sanitized malformed-data error", rr.Body.String())
			}
		})
	}
}

func TestCodexAccountLimitsRejectsOversizedUsageResponse(t *testing.T) {
	gin.SetMode(gin.TestMode)

	body := `{"padding":"` + strings.Repeat("x", 70*1024) + `","rate_limit":{"primary_window":{"limit_window_seconds":18000,"used_percent":12.5}}}`
	manager := &fakeAuthManager{
		auths:    []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, body),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusBadGateway, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_usage_too_large"}` {
		t.Fatalf("body = %q, want sanitized size error", rr.Body.String())
	}
}

func TestCodexAccountLimitsProvidesAnUpstreamDeadline(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths:           []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		requireDeadline: true,
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusGatewayTimeout {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusGatewayTimeout, rr.Body.String())
	}
	if !manager.newRequestHasDeadline || !manager.httpRequestHasDeadline {
		t.Fatalf(
			"deadline = new:%t execute:%t, want finite deadline on both operations",
			manager.newRequestHasDeadline,
			manager.httpRequestHasDeadline,
		)
	}
	if rr.Body.String() != `{"error":"codex_account_usage_timeout"}` {
		t.Fatalf("body = %q, want sanitized timeout error", rr.Body.String())
	}
}

func TestCodexAccountLimitsReturnsSanitizedTimeout(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths:   []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		httpErr: context.DeadlineExceeded,
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusGatewayTimeout {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusGatewayTimeout, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_usage_timeout"}` {
		t.Fatalf("body = %q, want sanitized timeout error", rr.Body.String())
	}
}

func TestCodexAccountLimitsSanitizesUpstreamFailure(t *testing.T) {
	gin.SetMode(gin.TestMode)

	sensitiveMarker := "synthetic-secret-marker"
	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(
			http.StatusUnauthorized,
			`{"error":"`+sensitiveMarker+`","account_id":"account-example"}`,
		),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusBadGateway, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_usage_unavailable"}` {
		t.Fatalf("body = %q, want sanitized upstream error", rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), sensitiveMarker) || strings.Contains(rr.Body.String(), "account-example") {
		t.Fatalf("body = %q, leaked upstream or account metadata", rr.Body.String())
	}
}

func TestCodexAccountLimitsReturnsAuthoritativeEmptyWithoutCanonicalWindows(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{activeCodexAuth("codex-test", "account-example")},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 3600,
					"used_percent": 10
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	if rr.Body.String() != `{"schema_version":1,"windows":[]}` {
		t.Fatalf("body = %q, want authoritative empty windows", rr.Body.String())
	}
}

func TestCodexAccountLimitsIgnoresIneligibleAccounts(t *testing.T) {
	gin.SetMode(gin.TestMode)

	selected := activeCodexAuth("selected", "selected-account-example")
	disabled := activeCodexAuth("disabled", "disabled-account-example")
	disabled.Disabled = true
	apiKey := activeCodexAuth("api-key", "api-key-account-example")
	apiKey.Attributes[auth.AttributeAuthKind] = auth.AuthKindAPIKey
	nonCodex := activeCodexAuth("claude", "claude-account-example")
	nonCodex.Provider = "claude"
	inactive := activeCodexAuth("inactive", "inactive-account-example")
	inactive.Status = auth.StatusError
	manager := &fakeAuthManager{
		auths: []*auth.Auth{nil, disabled, apiKey, nonCodex, inactive, selected},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"primary_window": {
					"limit_window_seconds": 18000,
					"used_percent": 12.5
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	if manager.selectedAuth != selected {
		t.Fatalf("selected auth = %#v, want only active Codex OAuth account", manager.selectedAuth)
	}
}

func TestCodexAccountLimitsUsesAccountDuringQuotaCooldown(t *testing.T) {
	gin.SetMode(gin.TestMode)

	selected := activeCodexAuth("quota-cooldown", "account-example")
	selected.Status = auth.StatusError
	selected.Unavailable = true
	selected.Quota = auth.QuotaState{
		Exceeded: true,
		Reason:   "quota",
	}
	manager := &fakeAuthManager{
		auths: []*auth.Auth{selected},
		response: jsonResponse(http.StatusOK, `{
			"rate_limit": {
				"secondary_window": {
					"limit_window_seconds": 604800,
					"used_percent": 100
				}
			}
		}`),
	}

	rr := serveAccountLimits(manager)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusOK, rr.Body.String())
	}
	if manager.selectedAuth != selected {
		t.Fatal("quota-cooldown Codex account was not used for the limits request")
	}
}

func TestCodexAccountLimitsRejectsAmbiguousActiveAccounts(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := &fakeAuthManager{
		auths: []*auth.Auth{
			activeCodexAuth("codex-one", "account-one-example"),
			activeCodexAuth("codex-two", "account-two-example"),
		},
	}
	rr := serveAccountLimits(manager)

	if rr.Code != http.StatusConflict {
		t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusConflict, rr.Body.String())
	}
	if rr.Body.String() != `{"error":"codex_account_ambiguous"}` {
		t.Fatalf("body = %q, want sanitized ambiguity error", rr.Body.String())
	}
	if manager.requestMethod != "" {
		t.Fatal("ambiguous account selection performed upstream network work")
	}
}

func TestLocalOnlyUsesDirectPeerAddress(t *testing.T) {
	gin.SetMode(gin.TestMode)

	tests := []struct {
		name       string
		remoteAddr string
		headers    http.Header
		wantStatus int
	}{
		{
			name:       "IPv4 loopback",
			remoteAddr: "127.0.0.1:1234",
			wantStatus: http.StatusNoContent,
		},
		{
			name:       "IPv6 loopback",
			remoteAddr: "[::1]:1234",
			wantStatus: http.StatusNoContent,
		},
		{
			name:       "remote peer",
			remoteAddr: "192.0.2.10:1234",
			wantStatus: http.StatusNotFound,
		},
		{
			name:       "malformed peer",
			remoteAddr: "not-an-address",
			wantStatus: http.StatusNotFound,
		},
		{
			name:       "forwarded loopback cannot spoof remote peer",
			remoteAddr: "192.0.2.10:1234",
			headers: http.Header{
				"X-Forwarded-For": []string{"127.0.0.1"},
				"X-Real-Ip":       []string{"127.0.0.1"},
			},
			wantStatus: http.StatusNotFound,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			router := gin.New()
			router.GET("/limits", LocalOnly(), func(c *gin.Context) {
				c.Status(http.StatusNoContent)
			})

			req := httptest.NewRequest(http.MethodGet, "/limits", nil)
			req.RemoteAddr = test.remoteAddr
			req.Host = "127.0.0.1:8317"
			for name, values := range test.headers {
				for _, value := range values {
					req.Header.Add(name, value)
				}
			}
			rr := httptest.NewRecorder()
			router.ServeHTTP(rr, req)

			if rr.Code != test.wantStatus {
				t.Fatalf("status = %d, want %d", rr.Code, test.wantStatus)
			}
		})
	}
}
