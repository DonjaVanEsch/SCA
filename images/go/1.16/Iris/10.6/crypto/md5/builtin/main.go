package main

import (
	"runtime"
	"runtime/debug"
	"github.com/kataras/iris"
	_ "crypto/md5"
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
	app := iris.New()
	app.Get("/", func(ctx iris.Context) {
		ctx.JSON(iris.Map{"message": "Hello World"})
	})
	app.Get("/version", func(ctx iris.Context) {
		ctx.JSON(iris.Map{
			"language":  iris.Map{"name": "Go", "version": runtime.Version()},
			"framework": iris.Map{"name": "Iris", "version": modVersion("github.com/kataras/iris")},
			"library":   iris.Map{"name": "crypto/md5", "version": "built-in"},
		})
	})
	app.Run(iris.Addr(":8000"))
}
