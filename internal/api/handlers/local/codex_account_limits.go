package local

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/runtime/executor/helps"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	log "github.com/sirupsen/logrus"
)

const (
	codexAccountUnavailable         = "codex_account_unavailable"
	codexAccountAmbiguous           = "codex_account_ambiguous"
	codexAccountUsageUnavailable    = "codex_account_usage_unavailable"
	codexAccountUsageTimeout        = "codex_account_usage_timeout"
	codexAccountMalformed           = "codex_account_usage_malformed"
	codexAccountResponseTooLarge    = "codex_account_usage_too_large"
	codexAccountUsageURL            = "https://chatgpt.com/backend-api/wham/usage"
	codexAccountClientIdentity      = "codex_cli_rs"
	maxCodexUsageResponseBodySize   = 64 * 1024
	codexAccountUsageRequestTimeout = 4 * time.Second
)

type authManager interface {
	List() []*auth.Auth
	NewHttpRequest(
		context.Context,
		*auth.Auth,
		string,
		string,
		[]byte,
		http.Header,
	) (*http.Request, error)
	HttpRequest(
		context.Context,
		*auth.Auth,
		*http.Request,
	) (*http.Response, error)
}

type accountLimitsResponse struct {
	SchemaVersion int                  `json:"schema_version"`
	Windows       []accountLimitWindow `json:"windows"`
}

type accountLimitWindow struct {
	WindowMinutes int     `json:"window_minutes"`
	UsedPercent   float64 `json:"used_percent"`
	ResetsAt      int64   `json:"resets_at,omitempty"`
}

type upstreamUsageResponse struct {
	RateLimit *struct {
		PrimaryWindow   upstreamLimitWindow `json:"primary_window"`
		SecondaryWindow upstreamLimitWindow `json:"secondary_window"`
	} `json:"rate_limit"`
}

type upstreamLimitWindow struct {
	LimitWindowSeconds json.RawMessage `json:"limit_window_seconds"`
	UsedPercent        json.RawMessage `json:"used_percent"`
	ResetsAt           json.RawMessage `json:"reset_at"`
}

// Handler serves credential-isolated local account metadata.
type Handler struct {
	authManager authManager
}

// NewHandler creates a local account handler backed by the runtime auth manager.
func NewHandler(manager *auth.Manager) *Handler {
	return &Handler{authManager: manager}
}

func isLoopbackAddress(rawAddress string) bool {
	host, _, err := net.SplitHostPort(strings.TrimSpace(rawAddress))
	if err != nil {
		return false
	}
	if zone := strings.LastIndexByte(host, '%'); zone >= 0 {
		host = host[:zone]
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func isValidHostPort(port string) bool {
	if port == "" {
		return false
	}
	for _, digit := range port {
		if digit < '0' || digit > '9' {
			return false
		}
	}
	value, err := strconv.ParseUint(port, 10, 16)
	return err == nil && value > 0
}

func normalizeLocalHost(rawHost string) (string, bool) {
	host := strings.TrimSpace(rawHost)
	if host == "" {
		return "", false
	}

	if strings.HasPrefix(host, "[") {
		if strings.HasSuffix(host, "]") {
			bracketedHost := strings.TrimSuffix(strings.TrimPrefix(host, "["), "]")
			return bracketedHost, bracketedHost == "::1"
		}
		parsedHost, port, err := net.SplitHostPort(host)
		if err != nil || parsedHost != "::1" || !isValidHostPort(port) {
			return "", false
		}
		return parsedHost, true
	}
	if strings.Count(host, ":") == 1 {
		parsedHost, port, err := net.SplitHostPort(host)
		if err != nil || !isValidHostPort(port) {
			return "", false
		}
		return parsedHost, true
	}
	return host, true
}

func isLoopbackHost(rawHost string) bool {
	host, ok := normalizeLocalHost(rawHost)
	return ok && (strings.EqualFold(host, "localhost") ||
		host == "127.0.0.1" || host == "::1")
}

// LocalOnly rejects browser-originated and nonlocal-host requests in addition
// to enforcing a direct loopback peer.
func LocalOnly() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !isLoopbackAddress(c.Request.RemoteAddr) ||
			!isLoopbackHost(c.Request.Host) ||
			strings.TrimSpace(c.GetHeader("Origin")) != "" {
			c.AbortWithStatus(http.StatusNotFound)
			return
		}
		c.Next()
	}
}

// GetCodexAccountLimits returns a normalized Codex account limits snapshot.
func (h *Handler) GetCodexAccountLimits(c *gin.Context) {
	selected, ambiguous := h.selectCodexAccount()
	if ambiguous {
		c.JSON(http.StatusConflict, gin.H{"error": codexAccountAmbiguous})
		return
	}
	if selected == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": codexAccountUnavailable})
		return
	}

	accountID, _ := selected.Metadata["account_id"].(string)
	accountID = strings.TrimSpace(accountID)
	if accountID == "" {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": codexAccountUnavailable})
		return
	}

	headers := make(http.Header)
	headers.Set("Accept", "application/json")
	headers.Set("Chatgpt-Account-Id", accountID)
	headers.Set("Originator", codexAccountClientIdentity)
	headers.Set("User-Agent", codexAccountClientIdentity)
	requestContext, cancel := context.WithTimeout(
		c.Request.Context(),
		codexAccountUsageRequestTimeout,
	)
	defer cancel()
	outboundContext := helps.WithRedirectsDisabled(requestContext)
	request, err := h.authManager.NewHttpRequest(
		outboundContext,
		selected,
		http.MethodGet,
		codexAccountUsageURL,
		nil,
		headers,
	)
	if err != nil {
		writeAccountUsageFailure(c, err)
		return
	}

	response, err := h.authManager.HttpRequest(
		outboundContext,
		selected,
		request,
	)
	if err != nil {
		writeAccountUsageFailure(c, err)
		return
	}
	if response == nil || response.Body == nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountUsageUnavailable})
		return
	}
	defer closeAccountUsageBody(response.Body)
	if response.StatusCode != http.StatusOK {
		c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountUsageUnavailable})
		return
	}

	body, err := io.ReadAll(io.LimitReader(
		response.Body,
		maxCodexUsageResponseBodySize+1,
	))
	if err != nil {
		writeAccountUsageFailure(c, err)
		return
	}
	if len(body) > maxCodexUsageResponseBodySize {
		c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountResponseTooLarge})
		return
	}
	var upstream upstreamUsageResponse
	if err := json.Unmarshal(body, &upstream); err != nil || upstream.RateLimit == nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountMalformed})
		return
	}

	windows := make([]accountLimitWindow, 0, 2)
	seenDurations := make(map[int]struct{}, 2)
	canonicalWindows := 0
	for _, window := range []upstreamLimitWindow{
		upstream.RateLimit.PrimaryWindow,
		upstream.RateLimit.SecondaryWindow,
	} {
		minutes, canonical := canonicalWindowMinutes(window.LimitWindowSeconds)
		if canonical {
			canonicalWindows++
			if _, duplicate := seenDurations[minutes]; duplicate {
				c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountMalformed})
				return
			}
			seenDurations[minutes] = struct{}{}
		}
		if normalized, ok := normalizeAccountLimitWindow(window); ok {
			windows = append(windows, normalized)
		}
	}
	if canonicalWindows > 0 && len(windows) == 0 {
		c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountMalformed})
		return
	}
	sort.Slice(windows, func(i, j int) bool {
		return windows[i].WindowMinutes < windows[j].WindowMinutes
	})
	c.JSON(http.StatusOK, accountLimitsResponse{
		SchemaVersion: 1,
		Windows:       windows,
	})
}

func closeAccountUsageBody(body io.Closer) {
	if err := body.Close(); err != nil {
		log.Warn("codex account usage response body close failed")
	}
}

func writeAccountUsageFailure(c *gin.Context, err error) {
	if errors.Is(err, context.DeadlineExceeded) {
		c.JSON(http.StatusGatewayTimeout, gin.H{"error": codexAccountUsageTimeout})
		return
	}
	c.JSON(http.StatusBadGateway, gin.H{"error": codexAccountUsageUnavailable})
}

func (h *Handler) selectCodexAccount() (*auth.Auth, bool) {
	if h == nil || h.authManager == nil {
		return nil, false
	}
	var selected *auth.Auth
	for _, candidate := range h.authManager.List() {
		if candidate == nil || candidate.Disabled || !eligibleForCodexUsage(candidate) {
			continue
		}
		if !strings.EqualFold(strings.TrimSpace(candidate.Provider), "codex") ||
			candidate.AuthKind() != auth.AuthKindOAuth {
			continue
		}
		if selected != nil {
			return nil, true
		}
		selected = candidate
	}
	return selected, false
}

func eligibleForCodexUsage(candidate *auth.Auth) bool {
	if candidate.Status == auth.StatusActive {
		return true
	}
	return candidate.Status == auth.StatusError &&
		candidate.Quota.Exceeded &&
		strings.EqualFold(strings.TrimSpace(candidate.Quota.Reason), "quota")
}

func canonicalWindowMinutes(raw json.RawMessage) (int, bool) {
	var seconds int64
	if len(raw) == 0 || json.Unmarshal(raw, &seconds) != nil {
		return 0, false
	}
	switch seconds {
	case 18_000:
		return 300, true
	case 604_800:
		return 10_080, true
	default:
		return 0, false
	}
}

func parseUsedPercent(raw json.RawMessage) (float64, bool) {
	if len(raw) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return 0, false
	}
	var usedPercent float64
	if err := json.Unmarshal(raw, &usedPercent); err != nil ||
		math.IsNaN(usedPercent) || math.IsInf(usedPercent, 0) ||
		usedPercent < 0 || usedPercent > 100 {
		return 0, false
	}
	return usedPercent, true
}

func parseResetTimestamp(raw json.RawMessage) int64 {
	if len(raw) == 0 {
		return 0
	}
	var resetsAt int64
	if err := json.Unmarshal(raw, &resetsAt); err != nil ||
		resetsAt < 1_000_000_000 || resetsAt > 9_999_999_999 {
		return 0
	}
	return resetsAt
}

func normalizeAccountLimitWindow(window upstreamLimitWindow) (accountLimitWindow, bool) {
	usedPercent, validUtilization := parseUsedPercent(window.UsedPercent)
	if !validUtilization {
		return accountLimitWindow{}, false
	}
	minutes, canonical := canonicalWindowMinutes(window.LimitWindowSeconds)
	if !canonical {
		return accountLimitWindow{}, false
	}
	resetsAt := parseResetTimestamp(window.ResetsAt)
	return accountLimitWindow{
		WindowMinutes: minutes,
		UsedPercent:   usedPercent,
		ResetsAt:      resetsAt,
	}, true
}
