package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/home"
)

func TestCodexAccountLimitsRouteAvailableWithoutManagementSecret(t *testing.T) {
	server := newTestServer(t)

	for _, host := range []string{
		"localhost",
		"localhost:8317",
		"127.0.0.1",
		"127.0.0.1:8317",
		"::1",
		"[::1]",
		"[::1]:8317",
	} {
		t.Run(host, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/v0/local/codex/account-limits", nil)
			req.RemoteAddr = "127.0.0.1:4321"
			req.Host = host
			rr := httptest.NewRecorder()
			server.engine.ServeHTTP(rr, req)

			if rr.Code != http.StatusServiceUnavailable {
				t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusServiceUnavailable, rr.Body.String())
			}
			if !strings.Contains(rr.Body.String(), `"error":"codex_account_unavailable"`) {
				t.Fatalf("body = %q, want sanitized unavailable error", rr.Body.String())
			}
			if origin := rr.Header().Get("Access-Control-Allow-Origin"); origin != "" {
				t.Fatalf("Access-Control-Allow-Origin = %q, want no browser access", origin)
			}
		})
	}
}

func TestCodexAccountLimitsRouteRejectsBrowserAndNonlocalHost(t *testing.T) {
	server := newTestServer(t)

	for _, test := range []struct {
		name   string
		method string
		host   string
		origin string
	}{
		{
			name:   "cross-origin browser",
			method: http.MethodGet,
			host:   "127.0.0.1:8317",
			origin: "https://attacker.example",
		},
		{
			name:   "nonlocal host",
			method: http.MethodGet,
			host:   "attacker.example:8317",
		},
		{
			name:   "bracketed localhost host",
			method: http.MethodGet,
			host:   "[localhost]",
		},
		{
			name:   "bracketed localhost host with port",
			method: http.MethodGet,
			host:   "[localhost]:8317",
		},
		{
			name:   "bracketed IPv4 host",
			method: http.MethodGet,
			host:   "[127.0.0.1]",
		},
		{
			name:   "bracketed IPv4 host with port",
			method: http.MethodGet,
			host:   "[127.0.0.1]:8317",
		},
		{
			name:   "signed host port",
			method: http.MethodGet,
			host:   "localhost:+8317",
		},
		{
			name:   "out-of-range host port",
			method: http.MethodGet,
			host:   "localhost:65536",
		},
		{
			name:   "out-of-range IPv6 host port",
			method: http.MethodGet,
			host:   "[::1]:65536",
		},
		{
			name:   "alternate IPv4 loopback host",
			method: http.MethodGet,
			host:   "127.0.0.2:8317",
		},
		{
			name:   "mapped IPv4 loopback host",
			method: http.MethodGet,
			host:   "[::ffff:127.0.0.1]:8317",
		},
		{
			name:   "zone-qualified IPv6 host",
			method: http.MethodGet,
			host:   "[::1%lo]:8317",
		},
		{
			name:   "browser preflight",
			method: http.MethodOptions,
			host:   "127.0.0.1:8317",
			origin: "https://attacker.example",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(test.method, "/v0/local/codex/account-limits", nil)
			req.RemoteAddr = "127.0.0.1:4321"
			req.Host = test.host
			if test.origin != "" {
				req.Header.Set("Origin", test.origin)
			}
			rr := httptest.NewRecorder()
			server.engine.ServeHTTP(rr, req)

			if rr.Code != http.StatusNotFound {
				t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusNotFound, rr.Body.String())
			}
			if origin := rr.Header().Get("Access-Control-Allow-Origin"); origin != "" {
				t.Fatalf("Access-Control-Allow-Origin = %q, want no browser access", origin)
			}
		})
	}
}

func TestCodexAccountLimitsTrailingSlashRejectsBeforeRedirect(t *testing.T) {
	server := newTestServer(t)

	for _, test := range []struct {
		name       string
		remoteAddr string
		host       string
		origin     string
	}{
		{
			name:       "nonlocal host",
			remoteAddr: "127.0.0.1:4321",
			host:       "attacker.example:8317",
		},
		{
			name:       "browser origin",
			remoteAddr: "127.0.0.1:4321",
			host:       "127.0.0.1:8317",
			origin:     "https://attacker.example",
		},
		{
			name:       "remote peer",
			remoteAddr: "192.0.2.10:4321",
			host:       "127.0.0.1:8317",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/v0/local/codex/account-limits/", nil)
			req.RemoteAddr = test.remoteAddr
			req.Host = test.host
			if test.origin != "" {
				req.Header.Set("Origin", test.origin)
			}
			rr := httptest.NewRecorder()
			server.engine.ServeHTTP(rr, req)

			if rr.Code != http.StatusNotFound {
				t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusNotFound, rr.Body.String())
			}
			if location := rr.Header().Get("Location"); location != "" {
				t.Fatalf("Location = %q, want no route disclosure", location)
			}
			if origin := rr.Header().Get("Access-Control-Allow-Origin"); origin != "" {
				t.Fatalf("Access-Control-Allow-Origin = %q, want no browser access", origin)
			}
		})
	}
}

func TestCodexAccountLimitsRouteBoundaryPrecedesHomeHeartbeat(t *testing.T) {
	previousHome := home.Current()
	home.ClearCurrent()
	t.Cleanup(func() { home.SetCurrent(previousHome) })

	server := newTestServer(t)
	server.cfg.Home.Enabled = true

	for _, test := range []struct {
		name       string
		method     string
		remoteAddr string
		host       string
		origin     string
	}{
		{
			name:       "nonlocal host",
			method:     http.MethodGet,
			remoteAddr: "127.0.0.1:4321",
			host:       "attacker.example:8317",
		},
		{
			name:       "browser preflight",
			method:     http.MethodOptions,
			remoteAddr: "127.0.0.1:4321",
			host:       "127.0.0.1:8317",
			origin:     "https://attacker.example",
		},
		{
			name:       "remote peer",
			method:     http.MethodGet,
			remoteAddr: "192.0.2.10:4321",
			host:       "127.0.0.1:8317",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(test.method, "/v0/local/codex/account-limits", nil)
			req.RemoteAddr = test.remoteAddr
			req.Host = test.host
			if test.origin != "" {
				req.Header.Set("Origin", test.origin)
			}
			rr := httptest.NewRecorder()
			server.engine.ServeHTTP(rr, req)

			if rr.Code != http.StatusNotFound {
				t.Fatalf("status = %d, want %d; body=%s", rr.Code, http.StatusNotFound, rr.Body.String())
			}
			if origin := rr.Header().Get("Access-Control-Allow-Origin"); origin != "" {
				t.Fatalf("Access-Control-Allow-Origin = %q, want no browser access", origin)
			}
		})
	}
}
