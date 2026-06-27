package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	"github.com/julienschmidt/httprouter"
	_ "crypto/md5"
)

func modVersion(_ string) string { return "unknown" }

func main() {
	r := httprouter.New()
	r.GET("/", func(w http.ResponseWriter, req *http.Request, _ httprouter.Params) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
	})
	r.GET("/version", func(w http.ResponseWriter, req *http.Request, _ httprouter.Params) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"language":  map[string]string{"name": "Go", "version": runtime.Version()},
			"framework": map[string]string{"name": "httprouter", "version": modVersion("github.com/julienschmidt/httprouter")},
			"library":   map[string]string{"name": "crypto/md5", "version": "built-in"},
		})
	})
	http.ListenAndServe(":8000", r)
}
