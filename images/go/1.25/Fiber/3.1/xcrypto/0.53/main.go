package main

import (
	"encoding/json"
	"runtime"
	"runtime/debug"
	"github.com/gofiber/fiber/v3"
	_ "golang.org/x/crypto/sha3"
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

func jsonSend(c *fiber.Ctx, v interface{}) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	c.Set("Content-Type", "application/json")
	return c.Send(b)
}

func main() {
	app := fiber.New()
	app.Get("/", func(c *fiber.Ctx) error {
		return jsonSend(c, map[string]interface{}{"message": "Hello World"})
	})
	app.Get("/version", func(c *fiber.Ctx) error {
		return jsonSend(c, map[string]interface{}{
			"language":  map[string]interface{}{"name": "Go", "version": runtime.Version()},
			"framework": map[string]interface{}{"name": "Fiber", "version": modVersion("github.com/gofiber/fiber/v3")},
			"library":   map[string]interface{}{"name": "xcrypto", "version": modVersion("golang.org/x/crypto")},
		})
	})
	app.Listen(":8000")
}
