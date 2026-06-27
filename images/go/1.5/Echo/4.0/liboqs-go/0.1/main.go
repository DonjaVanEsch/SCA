package main

import (
	"net/http"
	"runtime"
	"github.com/labstack/echo/v4"
	_ "github.com/open-quantum-safe/liboqs-go/oqs"
)

func modVersion(_ string) string { return "unknown" }

func main() {
	e := echo.New()
	e.GET("/", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]string{"message": "Hello World"})
	})
	e.GET("/version", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]interface{}{
			"language":  map[string]string{"name": "Go", "version": runtime.Version()},
			"framework": map[string]string{"name": "Echo", "version": modVersion("github.com/labstack/echo/v4")},
			"library":   map[string]string{"name": "liboqs-go", "version": modVersion("github.com/open-quantum-safe/liboqs-go")},
		})
	})
	e.Start(":8000")
}
