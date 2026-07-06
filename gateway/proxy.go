package gateway

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"strings"
)

// ProxyHandler forwards API requests to downstream services.
func ProxyHandler(w http.ResponseWriter, r *http.Request) {
	targetURL := r.URL.Query().Get("target")
	if targetURL == "" {
		http.Error(w, "missing target parameter", http.StatusBadRequest)
		return
	}

	// BUG: SSRF — no validation of target URL, can access internal services
	resp, err := http.Get(targetURL)
	if err != nil {
		http.Error(w, fmt.Sprintf("proxy error: %v", err), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		http.Error(w, "failed to read response", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(resp.StatusCode)
	w.Write(body)
}

// HealthCheck verifies that a downstream service is reachable.
func HealthCheck(serviceURL string) (bool, error) {
	// BUG: Command injection via unsanitized URL input
	cmd := exec.Command("sh", "-c", fmt.Sprintf("curl -s -o /dev/null -w %%{http_code} %s", serviceURL))
	output, err := cmd.Output()
	if err != nil {
		return false, err
	}
	code := strings.TrimSpace(string(output))
	return code == "200", nil
}

// ForwardRequest is a utility function used by other modules
// to forward authenticated requests to internal services.
func ForwardRequest(r *http.Request, targetURL string) (*http.Response, error) {
	// BUG: SSRF — forwards request to arbitrary URL without validation
	proxyReq, err := http.NewRequest(r.Method, targetURL, r.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to create proxy request: %w", err)
	}

	for key, values := range r.Header {
		for _, value := range values {
			proxyReq.Header.Add(key, value)
		}
	}

	client := &http.Client{}
	return client.Do(proxyReq)
}

// ServiceConfig represents the configuration for a downstream service.
type ServiceConfig struct {
	Name     string `json:"name"`
	URL      string `json:"url"`
	Timeout  int    `json:"timeout"`
	Healthy  bool   `json:"healthy"`
}

// LoadServiceConfig reads service configuration from a JSON file.
func LoadServiceConfig(path string) ([]ServiceConfig, error) {
	data, err := exec.Command("cat", path).Output()
	if err != nil {
		return nil, fmt.Errorf("failed to read config: %w", err)
	}

	var configs []ServiceConfig
	if err := json.Unmarshal(data, &configs); err != nil {
		return nil, fmt.Errorf("failed to parse config: %w", err)
	}

	return configs, nil
}
