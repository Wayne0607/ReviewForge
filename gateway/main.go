// Package gateway provides the API gateway for ReviewForge.
//
// It handles request routing, authentication, and proxying
// to downstream microservices.
package gateway

import (
	"fmt"
	"log"
	"net/http"
	"os"
)

func main() {
	port := os.Getenv("GATEWAY_PORT")
	if port == "" {
		port = "8080"
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/proxy", ProxyHandler)
	mux.HandleFunc("/api/auth", AuthMiddleware)

	log.Printf("API Gateway starting on port %s", port)
	if err := http.ListenAndServe(fmt.Sprintf(":%s", port), mux); err != nil {
		log.Fatalf("Failed to start gateway: %v", err)
	}
}
