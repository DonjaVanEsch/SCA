package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	_ "crypto/sha1"
)

func modVersion(_ string) string { return "unknown" }

func main() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
	})
	http.HandleFunc("/version", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"language":  map[string]string{"name": "Go", "version": runtime.Version()},
			"framework": map[string]string{"name": "net/http", "version": "built-in"},
			"library":   map[string]string{"name": "crypto/sha1", "version": "built-in"},
		})
	})
	http.ListenAndServe(":8000", nil)
}
