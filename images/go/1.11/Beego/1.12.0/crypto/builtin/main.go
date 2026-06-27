package main

import (
	"runtime"
	beego "github.com/beego/beego"
	_ "crypto/sha256"
)

func modVersion(_ string) string { return "unknown" }

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
		"framework": map[string]string{"name": "Beego", "version": modVersion("github.com/beego/beego")},
		"library":   map[string]string{"name": "crypto", "version": "built-in"},
	}
	c.ServeJSON()
}

func main() {
	beego.Router("/", &MainController{})
	beego.Router("/version", &VersionController{})
	beego.Run(":8000")
}
