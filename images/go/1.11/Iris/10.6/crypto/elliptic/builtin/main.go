package main

import (
	"runtime"
	"github.com/kataras/iris"
	_ "crypto/elliptic"
)

func modVersion(_ string) string { return "unknown" }

func main() {
	app := iris.New()
	app.Get("/", func(ctx iris.Context) {
		ctx.JSON(iris.Map{"message": "Hello World"})
	})
	app.Get("/version", func(ctx iris.Context) {
		ctx.JSON(iris.Map{
			"language":  iris.Map{"name": "Go", "version": runtime.Version()},
			"framework": iris.Map{"name": "Iris", "version": modVersion("github.com/kataras/iris")},
			"library":   iris.Map{"name": "crypto/elliptic", "version": "built-in"},
		})
	})
	app.Run(iris.Addr(":8000"))
}
