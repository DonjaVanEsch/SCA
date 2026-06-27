package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	"runtime/debug"
	"github.com/go-chi/chi/v5"
	_ "crypto/sha1"
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
	r := chi.NewRouter()
	r.Get("/", func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
	})
	r.Get("/version", func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"language":  map[string]string{"name": "Go", "version": runtime.Version()},
			"framework": map[string]string{"name": "Chi", "version": modVersion("github.com/go-chi/chi/v5")},
			"library":   map[string]string{"name": "crypto/sha1", "version": "built-in"},
		})
	})
	http.ListenAndServe(":8000", r)
}
