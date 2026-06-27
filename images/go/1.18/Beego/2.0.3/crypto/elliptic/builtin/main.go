package main

import (
	"runtime"
	"runtime/debug"
	beego "github.com/beego/beego/v2/server/web"
	_ "crypto/elliptic"
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

type MainController struct {
	beego.Controller
}

func (c *MainController) Get() {
	c.Data["json"] = map[string]string{"message": "Hello World"}
	c.ServeJSON()
}

type VersionController struct {
	beego.Controller
}

func (c *VersionController) Get() {
	c.Data["json"] = map[string]interface{}{
		"language":  map[string]string{"name": "Go", "version": runtime.Version()},
		"framework": map[string]string{"name": "Beego", "version": modVersion("github.com/beego/beego/v2")},
		"library":   map[string]string{"name": "crypto/elliptic", "version": "built-in"},
	}
	c.ServeJSON()
}

func main() {
	beego.Router("/", &MainController{})
	beego.Router("/version", &VersionController{})
	beego.Run(":8000")
}
