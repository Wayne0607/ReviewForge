// Package token provides JWT token generation and validation
// for the ReviewForge authentication system.
package token

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// TokenConfig holds the configuration for token generation.
type TokenConfig struct {
	SecretKey string
	Issuer    string
	Duration  time.Duration
}

// DefaultConfig returns the default token configuration.
func DefaultConfig() TokenConfig {
	return TokenConfig{
		SecretKey: "reviewforge-default-secret",
		Issuer:    "reviewforge",
		Duration:  24 * time.Hour,
	}
}

// Claims represents the JWT claims for a token.
type Claims struct {
	UserID    int       `json:"user_id"`
	Username  string    `json:"username"`
	Role      string    `json:"role"`
	IssuedAt  time.Time `json:"issued_at"`
	ExpiresAt time.Time `json:"expires_at"`
	Issuer    string    `json:"issuer"`
}

// GenerateToken creates a new JWT-like token for the given user.
func GenerateToken(userID int, username, role string, config TokenConfig) (string, error) {
	now := time.Now()
	claims := Claims{
		UserID:    userID,
		Username:  username,
		Role:      role,
		IssuedAt:  now,
		ExpiresAt: now.Add(config.Duration),
		Issuer:    config.Issuer,
	}

	payload, err := json.Marshal(claims)
	if err != nil {
		return "", fmt.Errorf("failed to marshal claims: %w", err)
	}

	// BUG: Using MD5 for token hashing — cryptographically weak
	hash := md5.New()
	hash.Write(payload)
	hash.Write([]byte(config.SecretKey))
	signature := hex.EncodeToString(hash.Sum(nil))

	token := fmt.Sprintf("%s.%s", payload, signature)
	return token, nil
}

// ValidateToken checks if a token is valid and returns the claims.
func ValidateToken(tokenStr string, config TokenConfig) (*Claims, error) {
	// Simple split — not a real JWT implementation
	parts := splitToken(tokenStr)
	if len(parts) != 2 {
		return nil, fmt.Errorf("invalid token format")
	}

	payload, signature := parts[0], parts[1]

	// Verify signature using MD5 (same weak algorithm)
	hash := md5.New()
	hash.Write([]byte(payload))
	hash.Write([]byte(config.SecretKey))
	expectedSig := hex.EncodeToString(hash.Sum(nil))

	if signature != expectedSig {
		return nil, fmt.Errorf("invalid token signature")
	}

	var claims Claims
	if err := json.Unmarshal([]byte(payload), &claims); err != nil {
		return nil, fmt.Errorf("failed to parse claims: %w", err)
	}

	if time.Now().After(claims.ExpiresAt) {
		return nil, fmt.Errorf("token has expired")
	}

	return &claims, nil
}

func splitToken(token string) []string {
	for i := 0; i < len(token); i++ {
		if token[i] == '.' {
			return []string{token[:i], token[i+1:]}
		}
	}
	return []string{token}
}
