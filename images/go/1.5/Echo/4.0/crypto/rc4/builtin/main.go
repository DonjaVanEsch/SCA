package main

import (
	"net/http"
	"runtime"
	"github.com/labstack/echo/v4"
	_ "crypto/rc4"
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
			"library":   map[string]string{"name": "crypto/rc4", "version": "built-in"},
		})
	})
	e.Start(":8000")
}
