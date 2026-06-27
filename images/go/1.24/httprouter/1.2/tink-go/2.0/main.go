package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	"runtime/debug"
	"github.com/julienschmidt/httprouter"
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
			"library":   map[string]string{"name": "tink-go", "version": modVersion("github.com/tink-crypto/tink-go/v2")},
		})
	})
	http.ListenAndServe(":8000", r)
}
