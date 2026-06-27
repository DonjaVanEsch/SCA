package main

import (
	"net/http"
	"runtime"
	"runtime/debug"
	"github.com/labstack/echo/v5"
	_ "github.com/google/tink/go/core/registry"
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
	e := echo.New()
	e.GET("/", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]string{"message": "Hello World"})
	})
	e.GET("/version", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]interface{}{
			"language":  map[string]string{"name": "Go", "version": runtime.Version()},
			"framework": map[string]string{"name": "Echo", "version": modVersion("github.com/labstack/echo/v5")},
			"library":   map[string]string{"name": "tink-go", "version": modVersion("github.com/google/tink/go")},
		})
	})
	e.Start(":8000")
}
