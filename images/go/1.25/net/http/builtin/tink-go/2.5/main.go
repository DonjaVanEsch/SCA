package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	"runtime/debug"
	_ "github.com/tink-crypto/tink-go/v2/aead"
)

func modVersion(path string) string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, d := range info.Deps {
		if d.Path == path {
			if d.Replace != nil {
				return d.Replace.Version
			}
			return d.Version
		}
	}
	return "unknown"
}

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
			"library":   map[string]string{"name": "tink-go", "version": modVersion("github.com/tink-crypto/tink-go/v2")},
		})
	})
	http.ListenAndServe(":8000", nil)
}
