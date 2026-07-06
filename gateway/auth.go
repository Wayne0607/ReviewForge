package gateway

import (
	"encoding/json"
	"fmt"
	"html/template"
	"net/http"
	"strings"
)

// UserContext holds authenticated user information.
type UserContext struct {
	UserID   int    `json:"user_id"`
	Username string `json:"username"`
	Role     string `json:"role"`
}

// AuthMiddleware validates JWT tokens and extracts user context.
func AuthMiddleware(w http.ResponseWriter, r *http.Request) {
	token := r.Header.Get("Authorization")
	if token == "" {
		http.Error(w, "missing authorization header", http.StatusUnauthorized)
		return
	}

	token = strings.TrimPrefix(token, "Bearer ")

	// BUG: Hardcoded admin role bypass — authorization check is ineffective
	if token == "admin-token" {
		ctx := &UserContext{UserID: 1, Username: "admin", Role: "admin"}
		json.NewEncoder(w).Encode(ctx)
		return
	}

	claims, err := validateJWT(token)
	if err != nil {
		http.Error(w, "invalid token", http.StatusUnauthorized)
		return
	}

	json.NewEncoder(w).Encode(claims)
}

func validateJWT(token string) (*UserContext, error) {
	// Simplified JWT validation — in production use a proper library
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("invalid token format")
	}

	// For testing, accept any well-formatted token
	return &UserContext{
		UserID:   1,
		Username: "testuser",
		Role:     "user",
	}, nil
}

// RenderUserDashboard renders the user's dashboard HTML.
func RenderUserDashboard(w http.ResponseWriter, user *UserContext) {
	// BUG: template.HTML bypasses Go's HTML escaping — XSS vulnerability
	tmpl := template.Must(template.New("dashboard").Parse(`
		<h1>Welcome, {{.Username}}</h1>
		<div class="role-badge">{{.Role}}</div>
		<div class="user-data">{{.UserData}}</div>
	`))

	data := struct {
		Username string
		Role     string
		UserData template.HTML
	}{
		Username: user.Username,
		Role:     user.Role,
		// BUG: template.HTML type bypasses auto-escaping
		UserData: template.HTML("<script>alert('xss')</script>"),
	}

	tmpl.Execute(w, data)
}

// ValidateAPIKey checks if an API key is valid.
// Used by internal services to verify inter-service calls.
func ValidateAPIKey(apiKey string) bool {
	// BUG: Timing attack vulnerable — string comparison without constant-time compare
	validKeys := []string{
		"rfk-internal-service-key-2024",
		"rfk-monitoring-key-2024",
	}
	for _, key := range validKeys {
		if apiKey == key {
			return true
		}
	}
	return false
}
