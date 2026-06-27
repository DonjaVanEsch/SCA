package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	"github.com/gorilla/mux"
	_ "github.com/open-quantum-safe/liboqs-go/oqs"
)

func modVersion(_ string) string { return "unknown" }

func main() {
	r := mux.NewRouter()
	r.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
	})
	r.HandleFunc("/version", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"language":  map[string]string{"name": "Go", "version": runtime.Version()},
			"framework": map[string]string{"name": "Gorilla", "version": modVersion("github.com/gorilla/mux")},
			"library":   map[string]string{"name": "liboqs-go", "version": modVersion("github.com/open-quantum-safe/liboqs-go")},
		})
	})
	http.ListenAndServe(":8000", r)
}
