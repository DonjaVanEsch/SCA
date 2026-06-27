package main

import (
	"runtime"
	"runtime/debug"
	"github.com/gofiber/fiber/v2"
	_ "crypto/des"
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
	app := fiber.New()
	app.Get("/", func(c *fiber.Ctx) error {
		return c.JSON(fiber.Map{"message": "Hello World"})
	})
	app.Get("/version", func(c *fiber.Ctx) error {
		return c.JSON(fiber.Map{
			"language":  fiber.Map{"name": "Go", "version": runtime.Version()},
			"framework": fiber.Map{"name": "Fiber", "version": modVersion("github.com/gofiber/fiber/v2")},
			"library":   fiber.Map{"name": "crypto/des", "version": "built-in"},
		})
	})
	app.Listen(":8000")
}
